@echo off
echo.
echo  Installing dependencies...
pip install -r requirements.txt
echo.
echo  Starting DropBeam...
echo.
python server.py
pause
