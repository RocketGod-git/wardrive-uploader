@echo off
REM Build a standalone WardriveUploader.exe (no Python needed to run the result).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :err
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :err

echo Building exe...
REM Note: no --clean. Under OneDrive/synced folders --clean's rmtree can fail
REM with "Access is denied" while a file is being synced. Delete build\ yourself
REM if you want a from-scratch build.
if exist "dist\WardriveUploader.exe" del /q "dist\WardriveUploader.exe"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm wdgwars_uploader.spec || goto :err

echo.
echo Done. The executable is at:  dist\WardriveUploader.exe
goto :eof

:err
echo.
echo BUILD FAILED.
exit /b 1
