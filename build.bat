@echo off
pip install --upgrade pyinstaller -r requirements.txt >nul
pyinstaller --onefile --name site-cloner --collect-all playwright clone.py
echo.
echo Done: dist\site-cloner.exe
echo NOTE: on any machine running the exe, run once: python -m playwright install chromium
pause
