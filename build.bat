@echo off
python -m pip install --upgrade pyinstaller -r requirements.txt
python -m PyInstaller --onefile --name site-cloner --collect-all playwright clone.py
echo.
echo Done: dist\site-cloner.exe
echo NOTE: on any machine running the exe, run once: python -m playwright install chromium
pause
