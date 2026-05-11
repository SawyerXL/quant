#!/bin/bash
set -e
cd /root/quant
git pull
source .venv/bin/activate
pip install -r requirements.txt -q
python -m pytest tests/ -q
echo 'deploy ok'
