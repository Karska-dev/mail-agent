#!/bin/bash
# Double-click to build "Mail Agent.app" on this Mac (Apple Silicon). Takes a few minutes the first time.
set -e
cd "$(dirname "$0")/../.."
echo "Building Mail Agent.app in: $(pwd)/dist"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and try again."
  read -n 1 -s -r -p "Press any key to close."; exit 1
fi
python3 -m venv .build-venv
.build-venv/bin/python -m pip install -q --upgrade pip pyinstaller
.build-venv/bin/python -m PyInstaller --noconfirm --clean packaging/mac/MailAgent.spec
echo
echo "Done: dist/Mail Agent.app — opening the folder."
open dist
read -n 1 -s -r -p "Press any key to close this window."
