@echo off
REM Build pricecheck.exe.
REM
REM PyInstaller is the only third-party dependency in the project, and it is a
REM BUILD dependency only - nothing that runs needs it, and the scanner still
REM has none. Install it with:  python -m pip install pyinstaller
REM
REM --onefile      one .exe, nothing to install
REM --windowed     no console window behind the app
REM --clean        do not reuse a stale build cache after a code change
REM
REM Every failure below pauses. Double-clicked, this window used to close
REM faster than the message could be read, so a build that never ran looked
REM exactly like one that did - which is how a fortnight-old exe gets copied
REM over itself and the "new" tool opens with the old layout.
REM
REM PyInstaller writes dist\pricecheck.exe, and this then copies it up beside
REM wowcraft.sqlite3 - which is where you actually launch it from, and where
REM it needs to be to find the database at all. That copy used to be a line of
REM advice at the end instead of a step, so a rebuild appeared to do nothing:
REM the build succeeded every time and the exe being double-clicked was a
REM different, older file.

setlocal
cd /d "%~dp0"

REM Pick the interpreter that can BUILD, not merely the first one that runs.
REM PyInstaller is installed per interpreter and this machine has several, so
REM "first Python wins" picked one without it and stopped - while the Python
REM that built the previous exe sat further down the list, untried. Same
REM reasoning as pricecheck.cmd testing `import tkinter` rather than testing
REM that a file exists: run the candidate and ask it what it can do.
REM
REM WOWCRAFT_PYTHON still wins outright if it is set and runs at all. An
REM explicit override that gets silently overruled is worse than one that
REM fails with a message naming itself.
set "PY="
set "PYANY="
call :override "%WOWCRAFT_PYTHON%"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
call :try "py"
call :try "python"

REM Nothing had PyInstaller, so fall back to any working Python purely so the
REM message below can name one and print a command that will fix it.
if not defined PY if defined PYANY set "PY=%PYANY%"

if not defined PY (
    echo No usable Python found. Install python.org Python, or set
    echo WOWCRAFT_PYTHON to a working python.exe.
    pause
    exit /b 9
)

REM Reached only when no candidate could import PyInstaller at all.
"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo.
    echo PyInstaller is not installed for any Python found, including %PY%.
    echo Install it with:
    echo     "%PY%" -m pip install pyinstaller
    echo.
    echo If that stops on a permissions error - Program Files needs an
    echo elevated prompt - this does the same without one:
    echo     "%PY%" -m pip install --user pyinstaller
    echo.
    echo Or point WOWCRAFT_PYTHON at a python.exe that already has it.
    echo.
    echo Nothing was built. pricecheck.exe is untouched, and dist\pricecheck.exe
    echo is the PREVIOUS build - copying that anywhere changes nothing.
    pause
    exit /b 9
)

echo Building with %PY% ...
"%PY%" -m PyInstaller --onefile --windowed --clean ^
    --name pricecheck ^
    --distpath dist --workpath build --specpath build ^
    pricecheck.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED - the PyInstaller output above says why.
    pause
    exit /b 1
)

echo.

REM Windows will not overwrite a running executable, and the whole point of
REM this tool is that you leave it open. Say so plainly rather than failing
REM with "access is denied" and leaving a stale exe behind a fresh build.
copy /y "dist\pricecheck.exe" "pricecheck.exe" >nul
if errorlevel 1 (
    echo Built dist\pricecheck.exe, but could not replace pricecheck.exe.
    echo Close the price lookup window if it is open, then run this again.
    echo Nothing is lost - the new build is in dist\.
    pause
    exit /b 1
)

REM Prove it actually rebuilt. Every check above can pass while the exe on
REM disk is still older than the source it was supposed to be built from - a
REM build run before the file was saved, a copy that quietly did not happen -
REM and that is the one failure this script exists to make impossible to miss.
REM Written without < or > so that cmd cannot mistake any of it for a
REM redirection, quotes or no quotes.
"%PY%" -c "import os,sys;a=os.path.getmtime('pricecheck.exe');b=os.path.getmtime('pricecheck.py');sys.exit(0 if max(a,b)==a else 1)"
if errorlevel 1 (
    echo.
    echo WARNING: pricecheck.exe is OLDER than pricecheck.py, so the build did
    echo not take. Close any running price lookup window and run this again.
    pause
    exit /b 1
)

for %%F in (pricecheck.exe) do echo Built pricecheck.exe  ^(%%~zF bytes, %%~tF^)
echo Run it with pricecheck.cmd, or double-click pricecheck.exe.
echo The window's title bar shows that build time, so you can tell at a glance
echo which build you are looking at.
pause
exit /b 0

:try
REM A candidate qualifies only if it can import PyInstaller. Any candidate
REM that merely starts is remembered in PYANY, so a total miss can still be
REM reported against a real interpreter instead of a guess.
if defined PY exit /b 0
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
if not defined PYANY set "PYANY=%~1"
"%~1" -c "import PyInstaller" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:override
REM WOWCRAFT_PYTHON, used if it runs at all - PyInstaller or not.
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY=%~1"
    set "PYANY=%~1"
)
exit /b 0
