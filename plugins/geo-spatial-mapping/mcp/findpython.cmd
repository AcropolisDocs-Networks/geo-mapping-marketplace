@echo off
rem 1.12.3: a Python counts only when pycheck.py passes it (3.11 or newer, its own standard library in place, venv and ensurepip present). A Python that
rem merely starts is not enough: a folder holding python.exe and its DLLs but no Lib folder starts, prints "Could not find platform independent
rem libraries" every time, and an environment built on it carries that. pycheck.py says why a Python was skipped; the reason goes to the file named
rem in PYLOG when the caller sets it (setup.cmd and launch.cmd do), and nowhere else, because stdout belongs to the MCP protocol here.
rem Finds a real Python 3 and sets PYEXE and PYARG in the caller's environment. Call it with:  call "%~dp0findpython.cmd"
rem Order: 4.0.0, the python: line of %USERPROFILE%\.geo-mapping\config.yaml when the person set one (an environment variable would not reach
rem the desktop app's processes); then the py launcher (3.13, 3.12, 3.11, then any 3), then python and python3 from PATH, skipping anything under
rem WindowsApps (the Microsoft Store stubs, which print "Python was not found" or open the Store). Silent.
set "FPHERE=%~dp0"
set "PYEXE="
set "PYARG="
set "CFGPY="
set "CFG=%USERPROFILE%\.geo-mapping\config.yaml"
if not exist "%CFG%" goto noconfig
for /f "usebackq tokens=1,* delims=:" %%A in (`findstr /b /i /c:"python:" "%CFG%"`) do set "CFGPY=%%B"
if not defined CFGPY goto noconfig
set "CFGPY=%CFGPY:"=%"
set "CFGPY=%CFGPY:'=%"
for /f "tokens=*" %%X in ("%CFGPY%") do set "CFGPY=%%X"
rem goto, not ( ) blocks: a path such as "C:\Program Files (x86)\..." would close a block early
if exist "%CFGPY%" goto cfgcheck
if defined PYLOG >>"%PYLOG%" echo config.yaml python: %CFGPY%: not found
goto noconfig
:cfgcheck
"%CFGPY%" "%FPHERE%pycheck.py" >nul 2>nul
if errorlevel 1 goto cfgbad
set "PYEXE=%CFGPY%"
exit /b 0
:cfgbad
if defined PYLOG "%CFGPY%" "%FPHERE%pycheck.py" >>"%PYLOG%" 2>nul
:noconfig
where py >nul 2>nul
if errorlevel 1 goto trypath
for %%V in (3.13 3.12 3.11 3) do call :trypy %%V
if defined PYEXE exit /b 0

:trypath
for %%N in (python python3) do call :scan %%N
exit /b 0

:trypy
if defined PYEXE exit /b 0
rem 4.0.0: a version the py launcher does not have is "not installed", told apart from one that fails its check
py -%1 -c "import sys" >nul 2>nul
if errorlevel 1 (
  if defined PYLOG if not "%1"=="3" >>"%PYLOG%" echo py -%1: not installed
  exit /b 0
)
py -%1 "%FPHERE%pycheck.py" >nul 2>nul
if not errorlevel 1 goto trypyok
if defined PYLOG py -%1 "%FPHERE%pycheck.py" >>"%PYLOG%" 2>nul
exit /b 0
:trypyok
set "PYEXE=py"
set "PYARG=-%1"
exit /b 0

:scan
if defined PYEXE exit /b 0
for /f "delims=" %%P in ('where %1 2^>nul') do call :probe "%%P"
exit /b 0

:probe
if defined PYEXE exit /b 0
echo %~1| findstr /i /c:"\WindowsApps\" >nul
if not errorlevel 1 exit /b 0
"%~1" "%FPHERE%pycheck.py" >nul 2>nul
if not errorlevel 1 goto probeok
if defined PYLOG "%~1" "%FPHERE%pycheck.py" >>"%PYLOG%" 2>nul
exit /b 0
:probeok
set "PYEXE=%~1"
exit /b 0
