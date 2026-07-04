@echo off
REM ─────────────────────────────────────────────────────────────
REM  GSE Scraper Launcher
REM  Update the two paths below before using.
REM ─────────────────────────────────────────────────────────────

REM Path to the folder containing main.py
cd /d "C:\Users\Tom\Desktop\Re\files (1)\main.py"

REM Path to your Python executable (run "where python" in CMD to find it)
"C:\Program Files\Python313\python.exe" main.py

REM Keep window open briefly so you can see any errors (optional)
timeout /t 5
