import os
import shutil
import subprocess
import time
import venv
from datetime import date, timedelta
from pathlib import Path

from autogen import ConversableAgent
from autogen.coding import LocalCommandLineCodeExecutor
from google.genai import errors

# ---------------------------------------------------------------- settings
PARAMS = {
    "symbols": ["BTC-USD", "NVDA"],   # Yahoo tickers
    "months": 4,
    "short_window": 7,
    "long_window": 30,
    "normalize": True,                # rebase to 100 and plot together
}

ALLOWED_SYMBOLS = {"BTC-USD", "ETH-USD", "NVDA", "AAPL", "MSFT", "TSLA"}

llm_config = {
    "model": "gemini-3.1-flash-lite",
    "api_type": "google",
    "api_key": os.environ["GEMINI_API_KEY"],
}


def validate(p):
    problems = []
    if not 1 <= len(p["symbols"]) <= 4:
        problems.append("use between 1 and 4 symbols")
    bad = [s for s in p["symbols"] if s not in ALLOWED_SYMBOLS]
    if bad:
        problems.append(f"symbols not allowed: {bad}")
    if not 1 <= p["months"] <= 24:
        problems.append("months must be between 1 and 24")
    if not 2 <= p["short_window"] < p["long_window"] <= 200:
        problems.append("need 2 <= short_window < long_window <= 200")
    if p["months"] * 30 < 2 * p["long_window"]:
        problems.append("not enough months for that long_window")
    if problems:
        raise SystemExit("Bad parameters:\n- " + "\n- ".join(problems))


validate(PARAMS)   # stop here, before any model is called, if settings are bad

# ----------------------------------------------------------------- sandbox
work_dir = Path("sandbox")
work_dir.mkdir(exist_ok=True)

# Separate virtual environment: pip installs land here, not in your project
venv_dir = "sandbox_venv"
venv_builder = venv.EnvBuilder(with_pip=True)
venv_builder.create(venv_dir)
venv_context = venv_builder.ensure_directories(venv_dir)

executor = LocalCommandLineCodeExecutor(
    work_dir=str(work_dir),
    virtual_env_context=venv_context,
    timeout=300,
)


def is_done(msg):
    content = msg.get("content") or ""
    return "TERMINATE" in content and "```" not in content


# ------------------------------------------------------------------ agents
code_reviewer = ConversableAgent(
    name="code_reviewer",
    llm_config=llm_config,
    human_input_mode="NEVER",
    code_execution_config=False,
    is_termination_msg=is_done,
    system_message=(
        "You write Python code, review the results of running it, and fix it.\n"
        "Rules:\n"
        "- Give ONE ```python block per reply, and nothing else that must run.\n"
        "- To install libraries, use ONE python block that runs "
        "subprocess.check_call([sys.executable, '-m', 'pip', 'install', ...]) "
        "with ONLY these names: pandas numpy matplotlib yfinance. "
        "Never install anything else.\n"
        "- Network use is allowed only for pip and for downloading price data "
        "with yfinance. Nothing else.\n"
        "- Start plotting code with: import matplotlib; matplotlib.use('Agg')\n"
        "- Save every chart as a PNG in the current folder, and print the "
        "filename after saving.\n"
        "- Use the current folder only. Never delete files.\n"
        "- Do NOT predict prices and do NOT give trading advice.\n"
        "- After each run, review the output: did it run, was the PNG saved, "
        "does the chart have a title, axis labels and a legend? "
        "If anything is missing or wrong, fix it in the next block.\n"
        "- When everything is right, reply with a one-paragraph summary "
        "ending with the word TERMINATE."
        "- NEVER write TERMINATE in a reply that contains code.\n"
        "- NEVER describe results you have not seen in execution output. "
        "Only after execution output confirms everything worked, reply "
        "with a short summary and TERMINATE.\n"
    ),
)

code_executor = ConversableAgent(
    name="code_executor",
    llm_config=False,
    human_input_mode="ALWAYS",      # you approve before anything runs
    code_execution_config={"executor": executor},
    is_termination_msg=is_done,
)

# -------------------------------------------------------------------- task
# Dates are worked out here in plain Python, because the model does not
# know today's date.
today = date.today()
start = today - timedelta(days=30 * PARAMS["months"])
end = today + timedelta(days=1)       # yfinance's end date is exclusive
symbols = ", ".join(PARAMS["symbols"])
short, long_ = PARAMS["short_window"], PARAMS["long_window"]

steps = [
    "Install the libraries in one block.",
    "Download the data. Newer yfinance versions may return multi-level "
    "columns, so flatten them. Print the row count, first and last date, and "
    "missing values for each symbol.",
    f"Compute {short}-day and {long_}-day simple moving averages for each "
    "symbol on its own dates.",
    "Chart 1, saved as price_and_sma.png: one panel per symbol showing price "
    "and both moving averages, with titles, axis labels and a legend.",
]
if PARAMS["normalize"]:
    steps.append(
        "Chart 2, saved as normalized_sma.png: keep only dates all symbols "
        f"share, rebase each to 100 on the first shared date, and plot each "
        f"{short}-day moving average on one chart. Print the final values."
    )
steps.append(
    "For each symbol, print the dates when the short average crossed the long "
    "average. Describe only. Do not interpret."
)
steps.append(
    "Write findings.md with every number printed, the last data date, the "
    "download time (UTC), and a limitations section covering different "
    "trading calendars and that the latest point may be delayed or intraday."
)

TASK = (
    f"Today's date is {today}. Download daily closing prices for {symbols} "
    f"with the yfinance library, from {start} to {end} (end is exclusive).\n"
    "Steps, one script at a time:\n"
    + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, start=1))
)

# --------------------------------------------------------------------- run
chat_results = None
for attempt in range(1, 6):
    try:
        chat_results = code_executor.initiate_chat(
            recipient=code_reviewer,
            message=TASK,
            max_turns=14,
        )
        break
    except errors.ServerError as e:
        wait = 15 * attempt
        print(f"Server busy (attempt {attempt}/5): {e}\nRetrying in {wait}s")
        time.sleep(wait)
else:
    raise SystemExit("Gave up after 5 attempts. Try again later.")

# ----------------------------------------------------------------- results
print("\nFiles produced:")
for p in sorted(work_dir.iterdir()):
    print(" -", p.name)

code_cli = shutil.which("code")   # VS Code's command line launcher
for p in sorted(work_dir.glob("*.png")):
    print("Chart:", p.resolve())
    if code_cli:
        subprocess.run([code_cli, str(p.resolve())])
    elif hasattr(os, "startfile"):
        os.startfile(p)