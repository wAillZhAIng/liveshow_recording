@echo off
setlocal
cd /d "%~dp0"
set "WHATNOT_MODE=start"
if "%~1"=="--check" set "WHATNOT_MODE=check"
if exist ".venv\Scripts\python.exe" goto use_venv
if exist ".venv" (
  echo Existing .venv has no Python executable. Repair or rename it and retry.
  goto failed
)
where py >nul 2>&1
if errorlevel 1 goto fallback_python
py -3.12 -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)" >nul 2>&1
if errorlevel 1 goto try_311
py -3.12 whatnot_launcher.py %WHATNOT_MODE%
goto finished
:try_311
py -3.11 -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)" >nul 2>&1
if errorlevel 1 goto fallback_python
py -3.11 whatnot_launcher.py %WHATNOT_MODE%
goto finished
:fallback_python
python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or 3.12 is required. Install it and retry.
  goto failed
)
python whatnot_launcher.py %WHATNOT_MODE%
goto finished
:use_venv
".venv\Scripts\python.exe" whatnot_launcher.py %WHATNOT_MODE%
:finished
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Check Python, network access, dependencies and the README.
if not "%WHATNOT_MODE%"=="check" pause
exit /b 1
