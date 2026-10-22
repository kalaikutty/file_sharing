@echo off
echo.
echo  ===============================================
echo   Building DropBeam.exe (Windows)
echo  ===============================================
echo.

echo  Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo  Building single-file .exe ...
pyinstaller --onefile --name DropBeam --console server.py

echo.
if exist dist\DropBeam.exe (
    echo  DONE. Your file is at: dist\DropBeam.exe
    echo  Copy that single file anywhere - just double click it to run.
) else (
    echo  Build failed - check the errors above.
)
echo.
pause
