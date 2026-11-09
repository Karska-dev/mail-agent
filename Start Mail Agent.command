#!/bin/bash
# Double-click to open the Mail Agent in your browser. Keep this window open while you use it.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and try again."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
python3 -m email_agent app
