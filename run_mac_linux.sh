#!/usr/bin/env bash
set -e
if [ -z "$1" ]; then
  echo "Usage: ./run_mac_linux.sh <image-directory>"
  exit 1
fi
python3 -m pip install -r "$(dirname "$0")/requirements.txt"
python3 "$(dirname "$0")/app.py" "$1"
