@echo off
cd /d "%~dp0"
python -m pip install --quiet requests cryptography
python bot.py
pause
