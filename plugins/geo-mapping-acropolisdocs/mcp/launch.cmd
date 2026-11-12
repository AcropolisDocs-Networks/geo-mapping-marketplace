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
if exist "%BASE%\setup.running" goto checked
if exist "%BASE%\setup.failed" goto checked
fc /b "%HERE%requirements.txt" "%STAMP%" >nul 2>nul
if errorlevel 1 goto startsetup
fc /b "%HERE%..\requirements.txt" "%FULLSTAMP%" >nul 2>nul
if not errorlevel 1 goto checked
:startsetup
if not exist "%BASE%" mkdir "%BASE%" >nul 2>nul
start "geo-mapping setup" "%HERE%setup.cmd" /auto all
set "STARTED=1"
>&2 echo geo-mapping-acropolisdocs: installing the map packages, the optional extras and Chromium in a separate window (once).
:checked

if not exist "%VENVPY%" goto fallback
if not exist "%STAMP%" goto fallback
fc /b "%HERE%requirements.txt" "%STAMP%" >nul 2>nul
if errorlevel 1 goto fallback
"%VENVPY%" -c "import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl" >nul 2>nul
if errorlevel 1 goto fallback
"%VENVPY%" "%HERE%ogc_server.py" %*
exit /b %errorlevel%

:fallback
call "%HERE%findpython.cmd"
if not defined PYEXE (
  >&2 echo geo-mapping-acropolisdocs: no usable Python 3 was found. Install Python from python.org, or use the py launcher.
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
