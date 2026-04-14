#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi

source venv/bin/activate
pip install --quiet -r requirements.txt
python feedly_rss.py
