#!/bin/bash
cd ~/repos/troubleshooter || exit

source venv/bin/activate
python3 main.py
deactivate
