@echo off
rem ===========================================================================
rem  zoombie-install: the ONE entry point. A GETTER, and nothing more.
rem
rem  It deliberately carries NO install logic. It only fetches the current core
rem  (scripts/install.ps1) from the repo and runs it, so a saved or installed copy
rem  can never go stale: the thing that does the work is always fetched fresh.
rem
rem  Double-click it, right-click Run it, or call it from any shell. When it was
rem  double-clicked the window is held open at the end so the output is readable.
rem
rem  Options (forwarded to the core, which forwards them to the installer):
rem      -Check            detect only; write nothing
rem      -DryRun           plan only; write nothing
rem      -Model <name>     whisper model to ensure
rem      -Root <path>      ASCII toolchain root override
rem      -Force            re-download even when present
rem      -NoPause          never hold the window open
rem  Environment equivalents (for the unattended one-liner):
rem      ZOOMBIE_REPO_SLUG  ZOOMBIE_REPO_REF  ZOOMBIE_MODEL  ZOOMBIE_ROOT
rem      ZOOMBIE_NOPAUSE
rem ===========================================================================
setlocal EnableExtensions

set "REPO_SLUG=%ZOOMBIE_REPO_SLUG%"
if "%REPO_SLUG%"=="" set "REPO_SLUG=carnivorum/zoombie"
set "REPO_REF=%ZOOMBIE_REPO_REF%"
if "%REPO_REF%"=="" set "REPO_REF=main"

set "CORE_URL=https://raw.githubusercontent.com/%REPO_SLUG%/%REPO_REF%/scripts/install.ps1"

echo ==^> zoombie-install
echo     repo: %REPO_SLUG% @ %REPO_REF%

rem --- strip -NoPause, collect the rest to forward ----------------------------
set "FWD="
set "NOPAUSE=%ZOOMBIE_NOPAUSE%"
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="-NoPause" (
    set "NOPAUSE=1"
    shift
    goto parse_args
)
set FWD=%FWD% "%~1"
shift
goto parse_args
:args_done

rem --- ASCII staging base -----------------------------------------------------
rem A Cyrillic user profile makes %TEMP% non-ASCII, and cmd.exe then processes a
rem non-ASCII path through the console code page. %PUBLIC% is the machine-level
rem ASCII fallback the toolchain already uses; %SystemDrive%\ is last. The launch
rem directory is never used, so a double-click from a Cyrillic folder is safe.
set "BASE="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "$c=@($env:TEMP,$env:PUBLIC,($env:SystemDrive+'\')); ($c | Where-Object { $_ -and ($_ -notmatch '[^\x00-\x7F]') } | Select-Object -First 1)"`) do set "BASE=%%D"
if "%BASE%"=="" set "BASE=."

set "STAGE=%BASE%\zoombie-install-%RANDOM%%RANDOM%"
mkdir "%STAGE%" 2>nul
set "CORE=%STAGE%\install.ps1"

rem --- fetch the core --------------------------------------------------------
set "GOT="
where curl.exe >nul 2>&1
if errorlevel 1 goto fetch_web
curl.exe -L --fail --retry 3 --silent --show-error -o "%CORE%" "%CORE_URL%"
if not errorlevel 1 set "GOT=1"

:fetch_web
if defined GOT goto fetch_done
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { (New-Object Net.WebClient).DownloadFile('%CORE_URL%','%CORE%'); exit 0 } catch { exit 1 }"
if not errorlevel 1 set "GOT=1"

:fetch_done
if not defined GOT (
    echo ERROR: could not fetch the installer core from:
    echo        %CORE_URL%
    echo        Check the network, or set ZOOMBIE_REPO_SLUG / ZOOMBIE_REPO_REF.
    rmdir /s /q "%STAGE%" 2>nul
    set "CODE=1"
    goto finish
)

rem --- run the core ----------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%CORE%" %FWD%
set "CODE=%ERRORLEVEL%"

rmdir /s /q "%STAGE%" 2>nul

:finish
rem Hold the window open ONLY for a double-click: no forwarded arguments, no
rem ZOOMBIE_NOPAUSE, and cmd was launched with "/c" (what Explorer does). A shell
rem run, an argument-bearing run and an automated run all return immediately, so
rem the pause can never hang a script.
if not defined NOPAUSE if "%FWD%"=="" (
    echo %cmdcmdline% | findstr /i /c:"/c" >nul && pause
)
exit /b %CODE%
