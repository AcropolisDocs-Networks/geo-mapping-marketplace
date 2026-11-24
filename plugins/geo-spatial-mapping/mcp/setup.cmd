@echo off
rem Installs the Python packages the bundled data server needs into a private virtual environment (not your system Python), once.
rem The environment lives in %LOCALAPPDATA%\AcropolisDocs\geo-mapping, so it survives plugin updates and is rebuilt only when mcp\requirements.txt changes.
rem 4.0.0: this file finds a Python (findpython.cmd, or python: in %USERPROFILE%\.geo-mapping\config.yaml) and hands over to setup_env.py, which
rem checks first (write access, the Python, certificate settings, the network), installs, and writes everything it prints to setup.log in that
rem folder. A failure ends with a cause code and a one-line fix (NO_PYTHON, NO_WRITE, PIP_CERT, PIP_TLS, NO_NETWORK, VENV_LOCKED, IMPORT, PIP).
rem Usage:  setup.cmd           the data server's packages (mcp\requirements.txt)
rem         setup.cmd all       also the full map-building set, the optional extras and Playwright's Chromium (only needed to build maps in this
rem                             computer's own Python; in Cowork maps are built in the session's Linux shell). Run it with Claude closed.
rem         setup.cmd /repair   removes the environment and its markers, then installs again
rem         setup.cmd /auto     what launch.cmd uses: the same, then waits for a key so the result can be read
setlocal EnableExtensions DisableDelayedExpansion
set "HERE=%~dp0"
set "ROOT=%HERE%.."
set "BASE=%LOCALAPPDATA%\AcropolisDocs\geo-mapping"
rem 1.6.3: another program's PYTHONHOME or PYTHONPATH (QGIS and ArcGIS set them) would point this Python at the wrong standard library
set "PYTHONHOME="
set "PYTHONPATH="
set "AUTO=0"
set "ARGS="
:args
if "%~1"=="" goto argsdone
if /i "%~1"=="/auto" (set "AUTO=1") else set "ARGS=%ARGS% %~1"
shift
goto args
:argsdone

if not exist "%BASE%" mkdir "%BASE%" >nul 2>nul
set "PYLOG=%BASE%\python-check.log"
if exist "%PYLOG%" del /q "%PYLOG%"
call "%HERE%findpython.cmd"
if not defined PYEXE goto nopython
"%PYEXE%" %PYARG% "%HERE%setup_env.py" --root "%ROOT%" --base "%BASE%" --pylog "%PYLOG%" %ARGS%
set "RC=%errorlevel%"
if "%AUTO%"=="1" pause
exit /b %RC%

:nopython
> "%BASE%\setup.log" echo NO_PYTHON: no usable Python 3.11 or newer was found.
>>"%BASE%\setup.log" echo Fix: winget install Python.Python.3.13  (the python and python3 entries under Settings, Apps, Advanced app settings, App execution aliases are Store stubs and are skipped on purpose)
if exist "%PYLOG%" (
  >>"%BASE%\setup.log" echo Pythons not used:
  type "%PYLOG%" >>"%BASE%\setup.log"
)
> "%BASE%\setup.failed" echo NO_PYTHON
>>"%BASE%\setup.failed" echo winget install Python.Python.3.13, then run setup again
if exist "%BASE%\setup.running" del /q "%BASE%\setup.running" >nul 2>nul
type "%BASE%\setup.log"
echo.
echo Log: %BASE%\setup.log
if "%AUTO%"=="1" pause
exit /b 1
