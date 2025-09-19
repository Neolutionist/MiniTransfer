#!/usr/bin/env bash
set -euo pipefail

# Always run from the folder where this script lives (handles Desktop/Bureaublad and spaces in path)
cd "$(cd "$(dirname "$0")" && pwd)"

# Show where we are (helps debugging if needed)
echo "Werkmap: $(pwd)"

# Check python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python3 niet gevonden. Installeer via https://www.python.org/ of Homebrew (brew install python)."
  exit 1
fi

# Create venv if needed
if [ ! -d ".venv" ]; then
  python3 -m venv ".venv"
fi

# Activate venv
# shellcheck source=/dev/null
source ".venv/bin/activate"

pip install --upgrade pip
pip install -r "requirements.txt"

export FLASK_APP="app.py"
python3 "app.py"
