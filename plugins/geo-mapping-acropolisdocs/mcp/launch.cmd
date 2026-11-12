@echo off
rem Starts the bundled OGC MCP server on Windows. Runs it from the private environment that setup.cmd builds; if that is not ready it uses a
rem system Python that already has the server's packages, and otherwise starts setup.cmd once in its own window. See findpython.cmd for why not "python3".
rem Stdout belongs to the MCP protocol, so every check here is silent and every message goes to stderr.
setlocal EnableExtensions DisableDelayedExpansion
set "HERE=%~dp0"
set "BASE=%LOCALAPPDATA%\AcropolisDocs\geo-mapping"
set "VENVPY=%BASE%\venv\Scripts\python.exe"
set "STAMP=%BASE%\requirements.installed"
set "FULLSTAMP=%BASE%\full.installed"
rem 1.6.3: another program's PYTHONHOME or PYTHONPATH (QGIS and ArcGIS set them) would point Python at the wrong standard library
set "PYTHONHOME="
set "PYTHONPATH="

rem 1.6.3: the first time (and when a new version changes the package list), install everything in its own window: the map-building set,
rem the optional extras and Playwright's Chromium. The server starts meanwhile from whatever Python already works. Never twice at once,
rem and not again after a failure (setup.failed says to run setup.cmd all by hand).
rem 1.12.3: launch.log (the latest start only) says what this start decided and why, so a setup window that did not open leaves a reason.
if not exist "%BASE%" mkdir "%BASE%" >nul 2>nul
>"%BASE%\launch.log" echo %date% %time% launch.cmd from %HERE%
rem a setup.running marker more than a day old belongs to a window that was closed or crashed: it must not block setup for ever
if exist "%BASE%\setup.running" forfiles /p "%BASE%" /m setup.running /d -1 >nul 2>nul && del /q "%BASE%\setup.running" >nul 2>nul
if exist "%BASE%\setup.running" goto skiprunning
if exist "%BASE%\setup.failed" goto skipfailed
fc /b "%HERE%requirements.txt" "%STAMP%" >nul 2>nul
if errorlevel 1 goto stampsetup
fc /b "%HERE%..\requirements.txt" "%FULLSTAMP%" >nul 2>nul
if errorlevel 1 goto stampsetup
if not exist "%VENVPY%" goto checked
"%VENVPY%" "%HERE%pycheck.py" >nul 2>nul
if not errorlevel 1 goto checked
rem the environment was built on a Python that is not whole: setup rebuilds it, and this start does not use it
set "BADVENV=1"
>>"%BASE%\launch.log" echo the private environment fails pycheck.py; starting setup to rebuild it
goto startsetup
:stampsetup
>>"%BASE%\launch.log" echo the package lists changed or the full install never finished; starting setup
:startsetup
start "geo-mapping setup" "%HERE%setup.cmd" /auto all
set "STARTED=1"
>>"%BASE%\launch.log" echo setup window started
>&2 echo geo-mapping-acropolisdocs: installing the map packages, the optional extras and Chromium in a separate window (once).
goto checked
:skiprunning
>>"%BASE%\launch.log" echo not starting setup: setup.running exists (a setup window is open, or it was interrupted less than a day ago)
goto checked
:skipfailed
>>"%BASE%\launch.log" echo not starting setup: setup.failed exists (run setup.cmd by hand to see why)
:checked

if defined BADVENV goto fallback
if not exist "%VENVPY%" goto fallback
if not exist "%STAMP%" goto fallback
fc /b "%HERE%requirements.txt" "%STAMP%" >nul 2>nul
if errorlevel 1 goto fallback
"%VENVPY%" -c "import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl" >nul 2>nul
if errorlevel 1 goto fallback
"%VENVPY%" "%HERE%ogc_server.py" %*
exit /b %errorlevel%

:fallback
set "PYLOG=%BASE%\launch.log"
call "%HERE%findpython.cmd"
if defined PYEXE >>"%BASE%\launch.log" echo fallback Python: %PYEXE% %PYARG%
if not defined PYEXE (
  >&2 echo geo-mapping-acropolisdocs: no usable Python 3.11 or newer was found. Install Python 3.13 from python.org, or run: winget install Python.Python.3.13. Why each Python found was skipped: "%BASE%\launch.log"
  >&2 echo The python and python3 entries under Settings, Apps, Advanced app settings, App execution aliases are Store stubs and are skipped on purpose.
  exit /b 1
)
"%PYEXE%" %PYARG% -c "import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl" >nul 2>nul
if errorlevel 1 goto needsetup
"%PYEXE%" %PYARG% "%HERE%ogc_server.py" %*
exit /b %errorlevel%

:needsetup
if exist "%BASE%\setup.running" (
  >&2 echo geo-mapping-acropolisdocs: the package install is still running in its own window, or was interrupted. If no window is open, delete "%BASE%\setup.running" and restart.
  exit /b 1
)
if exist "%BASE%\setup.failed" (
  >&2 echo geo-mapping-acropolisdocs: the package install failed earlier. Run "%HERE%setup.cmd" to see why, then restart.
  exit /b 1
)
if not defined STARTED start "geo-mapping setup" "%HERE%setup.cmd" /auto all
>&2 echo geo-mapping-acropolisdocs: first run. Installing the Python packages in a separate window; restart the plugin when it says Done.
exit /b 1
