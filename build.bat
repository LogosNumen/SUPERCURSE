@echo off
rem Builds dist\SUPERCURSE.exe, a single file you can share. Needs the .venv from the README plus: .venv\Scripts\python -m pip install pyinstaller
cd /d "%~dp0"
.venv\Scripts\python -c "import supercurse; supercurse.tray_image().save('icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64)])" || exit /b 1
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name SUPERCURSE --icon icon.ico ^
  --collect-all uiautomation --collect-submodules winrt --collect-submodules comtypes ^
  supercurse.py
