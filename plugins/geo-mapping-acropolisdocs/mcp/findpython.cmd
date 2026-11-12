@echo off
rem 1.8.0: only a Python 3.11 or newer counts (the pinned packages need it); the test is written without a greater-than sign, which cmd
rem would read as a redirect outside quotes on some paths.
rem Finds a real Python 3 and sets PYEXE and PYARG in the caller's environment. Call it with:  call "%~dp0findpython.cmd"
rem Order: the py launcher (py -3), then python and python3 from PATH, skipping anything under WindowsApps (the Microsoft Store stubs,
rem which print "Python was not found" or open the Store). Silent: stdout belongs to the MCP protocol when the launcher uses this.
set "PYEXE="
set "PYARG="
where py >nul 2>nul
if errorlevel 1 goto trypath
py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] == max(sys.version_info[:2], (3, 11)) else 1)" >nul 2>nul
if errorlevel 1 goto trypath
set "PYEXE=py"
set "PYARG=-3"
exit /b 0

:trypath
for %%N in (python python3) do call :scan %%N
exit /b 0

:scan
if defined PYEXE exit /b 0
for /f "delims=" %%P in ('where %1 2^>nul') do call :probe "%%P"
exit /b 0

:probe
if defined PYEXE exit /b 0
echo %~1| findstr /i /c:"\WindowsApps\" >nul
if not errorlevel 1 exit /b 0
"%~1" -c "import sys; sys.exit(0 if sys.version_info[:2] == max(sys.version_info[:2], (3, 11)) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYEXE=%~1"
exit /b 0
