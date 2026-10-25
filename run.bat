@echo off
rem ===========================================================
rem  Huawei Smart Life PC Console - launcher
rem  Uses the "py -3" launcher so the correct Python is picked.
rem  (Kept ASCII-only: cmd reads .bat in the OEM codepage and
rem   non-ASCII text would break the script.)
rem ===========================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=py -3"

%PY% -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo [1/2] PySide6 not found, installing dependencies...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Install failed. Run manually:  py -3 -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo [2/2] Starting...
%PY% main.py
if errorlevel 1 pause
