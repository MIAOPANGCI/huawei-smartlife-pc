@echo off
rem ===========================================================
rem  Huawei Smart Life PC Console - launcher (setup / debug)
rem
rem  Installs dependencies if missing, then starts the GUI with
rem  a windowless interpreter (pythonw) and exits immediately, so
rem  no console window keeps hanging around. Closing that old
rem  console window used to kill the app.
rem
rem  For a fully silent start with no console at all, use run.vbs.
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
rem  Launch detached so this script can exit: "start" returns at
rem  once and cmd closes its window while the GUI keeps running.
where pyw >nul 2>&1
if not errorlevel 1 (
    start "" pyw -3 "%~dp0main.py"
    exit /b 0
)

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)

rem No windowless interpreter found - fall back to a console run so
rem that any error stays readable.
%PY% main.py
if errorlevel 1 pause
