@echo off
rem Installs the Python packages the bundled OGC server needs into a private virtual environment (not your system Python), once.
rem The environment lives in %LOCALAPPDATA%\AcropolisDocs\geo-mapping, so it survives plugin updates and is rebuilt only when mcp\requirements.txt changes.
rem Only the server runs on Windows: the map skills and their PDF export (WeasyPrint) run wherever the shell runs, not here.
rem Usage:  setup.cmd        the server's packages (mcp\requirements.txt)
rem         setup.cmd all    also the full map-building set (requirements.txt), the optional extras (requirements-optional.txt, one at a
rem                          time) and Playwright's Chromium, for running the map scripts on this machine (1.6.3: what launch.cmd starts
rem                          by itself the first time, so nothing has to be installed by hand)
rem         setup.cmd /auto  what launch.cmd uses on first run: same as plain, then waits for a key so the result can be read
setlocal EnableExtensions DisableDelayedExpansion
set "HERE=%~dp0"
set "ROOT=%HERE%..\"
set "BASE=%LOCALAPPDATA%\AcropolisDocs\geo-mapping"
set "VENV=%BASE%\venv"
set "VENVPY=%VENV%\Scripts\python.exe"
set "STAMP=%BASE%\requirements.installed"
set "FULLSTAMP=%BASE%\full.installed"
rem 1.6.3: another program's PYTHONHOME or PYTHONPATH (QGIS and ArcGIS set them) would point this Python at the wrong standard library
set "PYTHONHOME="
set "PYTHONPATH="
set "AUTO=0"
set "ALL=0"
if /i "%~1"=="/auto" set "AUTO=1"
if /i "%~1"=="all" set "ALL=1"
if /i "%~2"=="all" set "ALL=1"

if not exist "%BASE%" mkdir "%BASE%"
if exist "%BASE%\setup.failed" del /q "%BASE%\setup.failed"
>"%BASE%\setup.running" echo started

rem 1.12.3: a Python is used only when pycheck.py passes it; the reasons for the ones skipped are kept in python-check.log
set "PYLOG=%BASE%\python-check.log"
if exist "%PYLOG%" del /q "%PYLOG%"
call "%HERE%findpython.cmd"
if not defined PYEXE (
  echo No usable Python 3.11 or newer was found. Install Python 3.13 from python.org ^(or run: winget install Python.Python.3.13^), then run this again.
  echo The python and python3 entries under Settings, Apps, Advanced app settings, App execution aliases are Microsoft Store stubs and are skipped on purpose.
  if exist "%PYLOG%" (
    echo.
    echo These Pythons were found and skipped:
    type "%PYLOG%"
  )
  goto failed
)
echo Using Python: %PYEXE% %PYARG%
if exist "%PYLOG%" (
  echo Skipped as not fit to build on:
  type "%PYLOG%"
)

if not exist "%VENVPY%" goto makevenv
"%VENVPY%" "%HERE%pycheck.py" >nul 2>nul
if not errorlevel 1 goto havevenv
echo The existing private environment was built on a Python that is not whole:
"%VENVPY%" "%HERE%pycheck.py"
echo Rebuilding it on %PYEXE% %PYARG%.
rmdir /s /q "%VENV%" >nul 2>nul
if not exist "%VENVPY%" goto makevenv
echo The old environment could not be removed because a program is using it: close Claude, and anything else running Python from %VENV%, then run this again.
goto failed

:makevenv
echo Creating the private environment in %VENV%
"%PYEXE%" %PYARG% -m venv "%VENV%"
if errorlevel 1 goto failed
"%VENVPY%" "%HERE%pycheck.py"
if errorlevel 1 (
  echo The new environment does not pass its own check ^(the reason is above^).
  goto failed
)

:havevenv
echo Installing packages. The first run downloads about a hundred megabytes and can take a few minutes.
"%VENVPY%" -m pip install --upgrade --disable-pip-version-check --progress-bar off pip
if errorlevel 1 goto failed
"%VENVPY%" -m pip install --disable-pip-version-check --progress-bar off -r "%HERE%requirements.txt"
if errorlevel 1 goto failed
"%VENVPY%" -c "import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl" >nul 2>nul
if errorlevel 1 (
  echo The install finished but the packages do not import. Run:  "%VENVPY%" -m pip check
  goto failed
)
copy /b /y "%HERE%requirements.txt" "%STAMP%" >nul

if "%ALL%"=="0" goto done
echo Installing the full map-building set.
rem 1.8.0: the tested versions first (constraints.txt); if one cannot be installed on this computer, the newest that fit
if not exist "%ROOT%constraints.txt" goto plainfull
"%VENVPY%" -m pip install --disable-pip-version-check --progress-bar off -r "%ROOT%requirements.txt" -c "%ROOT%constraints.txt"
if not errorlevel 1 goto fulldone
echo The tested versions could not all be installed here; installing the newest that fit instead.
:plainfull
"%VENVPY%" -m pip install --disable-pip-version-check --progress-bar off -r "%ROOT%requirements.txt"
if errorlevel 1 goto failed
:fulldone
echo Installing the optional extras and Playwright's Chromium.
"%VENVPY%" "%HERE%install_extras.py" "%ROOT%requirements-optional.txt"
copy /b /y "%ROOT%requirements.txt" "%FULLSTAMP%" >nul
"%VENVPY%" -c "import weasyprint" >nul 2>nul
if errorlevel 1 (
  echo.
  echo NOTE: WeasyPrint installed but cannot load its native libraries, so PDF export will not work from this environment.
  echo       On Windows it needs the GTK runtime, which pip cannot install. SVG and PNG output do not need it.
)

:done
del /q "%BASE%\setup.running" >nul 2>nul
echo.
echo Done. Restart the plugin so the OGC server picks up the packages.
echo The environment is at %VENV%
if "%AUTO%"=="1" pause
exit /b 0

:failed
del /q "%BASE%\setup.running" >nul 2>nul
>"%BASE%\setup.failed" echo failed
echo.
echo Setup did not finish. Fix the message above, then run this file again:  %~f0
echo If a line above says "Access is denied" or "being used by another process", a program is using the environment: close Claude, then run this again.
if "%AUTO%"=="1" pause
exit /b 1
