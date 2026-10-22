#!/bin/bash
echo ""
echo " ==============================================="
echo "  Building DropBeam.app (Mac)"
echo " ==============================================="
echo ""

echo " Installing dependencies..."
pip3 install -r requirements.txt
pip3 install pyinstaller

echo ""
echo " Building single-file .app ..."
pyinstaller --onefile --windowed --name DropBeam server.py

echo ""
if [ -d "dist/DropBeam.app" ]; then
    echo " DONE. Your app is at: dist/DropBeam.app"
    echo " Drag it to Applications or anywhere - just double click to run."
    echo ""
    echo " NOTE: first time you open it, macOS will say it's from an"
    echo " unidentified developer. Right-click the app -> Open -> Open"
    echo " (only needed once)."
else
    echo " Build failed - check the errors above."
fi
echo ""
