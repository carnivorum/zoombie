@echo off
rem ===========================================================================
rem  zoombie bootstrap: the ONLY non-Python file in the toolchain.
rem
rem  Python cannot install itself, so something has to exist before Python does.
rem  Its whole job is:
rem    1. find (or install) a Python interpreter,
rem    2. fetch the repo archive,
rem    3. hand off to the Python installer.
rem  Everything after step 3 is Python.
rem
rem  Usage:  bootstrap.cmd [-Check] [-DryRun] [-Model <name>] [-Root <path>] [-Force]
rem ===========================================================================
setlocal EnableExtensions

set "REPO_SLUG=%ZOOMBIE_REPO_SLUG%"
if "%REPO_SLUG%"=="" set "REPO_SLUG=carnivorum/zoombie"
set "REPO_REF=%ZOOMBIE_REPO_REF%"
if "%REPO_REF%"=="" set "REPO_REF=main"

set "ARCHIVE=https://codeload.github.com/%REPO_SLUG%/zip/refs/heads/%REPO_REF%"

echo ==^> zoombie bootstrap
echo     repo: %REPO_SLUG% @ %REPO_REF%

rem --- 1. locate a usable Python -------------------------------------------
set "PY="
for %%C in (python.exe py.exe) do (
    if not defined PY (
        for /f "delims=" %%P in ('where %%C 2^>nul') do (
            if not defined PY set "PY=%%P"
        )
    )
)
rem Reject the Microsoft Store alias stub: it is not a real interpreter and
rem running it opens the Store instead of executing anything.
if defined PY (
    echo %PY% | findstr /I "WindowsApps" >nul && set "PY="
)
if defined PY (
    rem `py -3` is the launcher; make sure it resolves before accepting it.
    "%PY%" -c "import sys" >nul 2>&1 || set "PY="
)

if not defined PY (
    echo ==^> no usable Python found; attempting to install one
    where winget >nul 2>&1 && (
        echo     winget install Python.Python.3.12
        winget install --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --silent
    )
    rem winget updates PATH for NEW processes; re-probe.
    for %%C in (python.exe) do (
        for /f "delims=" %%P in ('where %%C 2^>nul') do (
            if not defined PY set "PY=%%P"
        )
    )
    if defined PY (
        echo %PY% | findstr /I "WindowsApps" >nul && set "PY="
    )
)

if not defined PY (
    echo.
    echo ERROR: Python 3.10+ is required and could not be found or installed.
    echo        Install it manually and re-run this script:
    echo            winget install Python.Python.3.12
    echo        or download from https://www.python.org/downloads/windows/
    exit /b 1
)

echo     python: %PY%

rem --- 2. fetch the repo into a temp checkout ------------------------------
set "STAGE=%TEMP%\zoombie-bootstrap-%RANDOM%%RANDOM%"
mkdir "%STAGE%" 2>nul
set "ZIP=%STAGE%\repo.zip"

echo ==^> downloading %ARCHIVE%
rem curl.exe ships with Windows 10 1803+; fall back to the .NET client if absent.
where curl.exe >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "try { Invoke-WebRequest -Uri '%ARCHIVE%' -OutFile '%ZIP%' -UseBasicParsing; exit 0 } catch { exit 1 }"
) else (
    curl.exe -L --fail --retry 3 --silent --show-error -o "%ZIP%" "%ARCHIVE%"
)
if errorlevel 1 (
    echo ERROR: could not download the repository archive.
    echo        Check the network, or set ZOOMBIE_REPO_SLUG / ZOOMBIE_REPO_REF.
    rmdir /s /q "%STAGE%" 2>nul
    exit /b 1
)

rem tar.exe ships with Windows 10 1803+ and unpacks a zip, so no PowerShell
rem is needed for extraction on a modern machine.
where tar.exe >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%STAGE%' -Force"
) else (
    tar.exe -xf "%ZIP%" -C "%STAGE%"
)
if errorlevel 1 (
    echo ERROR: could not extract the repository archive.
    rmdir /s /q "%STAGE%" 2>nul
    exit /b 1
)

rem The archive extracts into a single top-level folder; find it.
set "REPO="
for /d %%D in ("%STAGE%\*") do if not defined REPO set "REPO=%%D"
if not defined REPO (
    echo ERROR: the archive contained no folder.
    rmdir /s /q "%STAGE%" 2>nul
    exit /b 1
)

rem --- 3. hand off to the Python installer ---------------------------------
echo ==^> running the installer
set "PYTHONPATH=%REPO%\scripts"
set "PYTHONUTF8=1"
"%PY%" -m zoombie.install %*
set "CODE=%ERRORLEVEL%"

rmdir /s /q "%STAGE%" 2>nul
exit /b %CODE%
