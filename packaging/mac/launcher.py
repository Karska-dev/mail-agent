"""Entry point of the double-click Mail Agent.app.

The app has no window of its own: it starts the local server and opens the page in your browser.
There's no Terminal either, so messages go to ~/.email_agent/app.log. Opening the app again
while it runs just brings the page back. It quits from the page's Quit button, or by itself
when no page has been open for 30 minutes and nothing is running.
"""
import os
import sys
from pathlib import Path


def _log_to_file() -> None:
    data = Path(os.environ.get("EMAIL_AGENT_DATA", "~/.email_agent")).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    log = data / "app.log"
    if log.exists() and log.stat().st_size > 1_000_000:   # keep it small
        log.replace(data / "app.log.1")
    stream = open(log, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = stream


def main() -> None:
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        _log_to_file()
    from email_agent.cli import main as cli_main
    args = ["app", "--quit-when-idle", "30"]
    if os.environ.get("MAIL_AGENT_NO_BROWSER"):   # used by the automatic build check
        args.append("--no-browser")
    cli_main(args)


if __name__ == "__main__":
    main()
