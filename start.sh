#!/bin/bash
echo ""
echo " Installing dependencies..."
pip3 install -r requirements.txt
echo ""
echo " Starting DropBeam..."
echo ""
python3 server.py
