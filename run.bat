@echo off
:loop
python spread_parser.py
echo Restarting in 3 seconds...
timeout /t 3
goto loop
