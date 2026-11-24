@echo off
rem Starts the bundled data server (MCP) on Windows. Runs it from the private environment that setup.cmd builds; if that is not ready it uses a
rem system Python that already has the server's packages; otherwise it starts setup.cmd once in its own window and, 4.0.0, a stand-in server
rem (standin_server.py, standard library only) that answers the MCP handshake and one tool, setup_status: the cause and the fix. The
rem connection then does not fail, so Claude does not cache a failure for 15 minutes. See findpython.cmd for why not "python3".
rem Stdout belongs to the MCP protocol, so every check here is silent and every message goes to stderr or launch.log.
rem 4.0.0 (spec B8): setup never installs into the environment while the server runs from it. The full map-building set (setup.cmd all) is
rem started here only when the server's own set is not ready either, so the server is never started from an environment a setup window is changing.
setlocal EnableExtensions DisableDelayedExpansion
set "HERE=%~dp0"
set "BASE=%LOCALAPPDATA%\AcropolisDocs\geo-mapping"
set "VENVPY=%BASE%\venv\Scripts\python.exe"
set "STAMP=%BASE%\requirements.installed"
set "FULLSTAMP=%BASE%\full.installed"
set "IMPORTS=import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl"
rem 1.6.3: another program's PYTHONHOME or PYTHONPATH (QGIS and ArcGIS set them) would point Python at the wrong standard library
set "PYTHONHOME="
set "PYTHONPATH="

if not exist "%BASE%" mkdir "%BASE%" >nul 2>nul
>"%BASE%\launch.log" echo %date% %time% launch.cmd from %HERE%
rem a setup.running marker more than a day old belongs to a window that was closed or crashed: it must not block setup for ever
if exist "%BASE%\setup.running" forfiles /p "%BASE%" /m setup.running /d -1 >nul 2>nul && del /q "%BASE%\setup.running" >nul 2>nul

rem ready: the environment exists, passes pycheck, holds the server's current package list and imports it
if not exist "%VENVPY%" goto notready
fc /b "%HERE%requirements.txt" "%STAMP%" >nul 2>nul
if errorlevel 1 goto notready
"%VENVPY%" "%HERE%pycheck.py" >nul 2>nul
if errorlevel 1 goto notready
"%VENVPY%" -c "%IMPORTS%" >nul 2>nul
if errorlevel 1 goto notready
fc /b "%HERE%..\requirements.txt" "%FULLSTAMP%" >nul 2>nul
if errorlevel 1 >>"%BASE%\launch.log" echo the full map-building set is not current; not started while the server runs from the environment (setup.cmd all, with Claude closed)
>>"%BASE%\launch.log" echo starting the data server from the private environment
"%VENVPY%" "%HERE%ogc_server.py" %*
exit /b %errorlevel%

:notready
>>"%BASE%\launch.log" echo the private environment is not ready
set "PYLOG=%BASE%\launch.log"
call "%HERE%findpython.cmd"
if defined PYEXE >>"%BASE%\launch.log" echo Python found: %PYEXE% %PYARG%
if not defined PYEXE goto nopython
rem a NO_PYTHON failure an earlier start recorded is over once a Python is found: setup may run again
if exist "%BASE%\setup.failed" findstr /b /c:"NO_PYTHON" "%BASE%\setup.failed" >nul 2>nul && del /q "%BASE%\setup.failed" >nul 2>nul
"%PYEXE%" %PYARG% -c "%IMPORTS%" >nul 2>nul
if errorlevel 1 goto standin
>>"%BASE%\launch.log" echo starting the data server from that Python, which has the packages
"%PYEXE%" %PYARG% "%HERE%ogc_server.py" %*
exit /b %errorlevel%

:standin
if exist "%BASE%\setup.running" goto skiprunning
if exist "%BASE%\setup.failed" goto skipfailed
>>"%BASE%\launch.log" echo starting setup in its own window
start "geo-spatial-mapping setup" "%HERE%setup.cmd" /auto all
>&2 echo geo-spatial-mapping: installing the data server's packages in a separate window (once). Restart Claude when it says Done.
goto runstandin
:skiprunning
>>"%BASE%\launch.log" echo not starting setup: setup.running exists (a setup window is open, or it was interrupted less than a day ago)
goto runstandin
:skipfailed
>>"%BASE%\launch.log" echo not starting setup: setup.failed exists (setup_status says why; setup.cmd /repair installs again)
:runstandin
>>"%BASE%\launch.log" echo starting the stand-in server (setup_status only)
"%PYEXE%" %PYARG% "%HERE%standin_server.py" --base "%BASE%"
exit /b %errorlevel%

:nopython
rem no Python passes pycheck; the stand-in needs only a standard library, so any py -3 will do for it
>>"%BASE%\launch.log" echo no Python 3.11 or newer passed pycheck
if not exist "%BASE%\setup.failed" (
  > "%BASE%\setup.failed" echo NO_PYTHON
  >>"%BASE%\setup.failed" echo winget install Python.Python.3.13, then restart Claude
)
where py >nul 2>nul
if errorlevel 1 goto nostandin
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto nostandin
>>"%BASE%\launch.log" echo starting the stand-in server on py -3
py -3 "%HERE%standin_server.py" --base "%BASE%"
exit /b %errorlevel%
:nostandin
>&2 echo geo-spatial-mapping: no usable Python 3.11 or newer was found. Install Python 3.13 from python.org, or run: winget install Python.Python.3.13. Why each Python found was skipped: "%BASE%\launch.log"
>&2 echo The python and python3 entries under Settings, Apps, Advanced app settings, App execution aliases are Store stubs and are skipped on purpose.
exit /b 1
