@echo off
rem ---------------------------------------------------------------------------
rem claudish gh wrapper launcher for Windows (cmd.exe / PowerShell).
rem
rem Runs gh-wrapper.py from this same directory. Install both files together in
rem a directory that comes BEFORE the real gh on PATH.
rem
rem Delayed expansion is deliberately NOT enabled: it would mangle any argument
rem containing an exclamation mark, and issue bodies are arbitrary text.
rem
rem FAIL-OPEN: if Python or the wrapper script is missing, this falls back to
rem the real gh so your gh commands never stop working.
rem ---------------------------------------------------------------------------
setlocal

set "WRAPPER=%~dp0gh-wrapper.py"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY where python3 >nul 2>&1 && set "PY=python3"

if defined PY if exist "%WRAPPER%" (
  %PY% "%WRAPPER%" %*
  exit /b %ERRORLEVEL%
)

rem Fallback: run the real gh unchanged. Skip any match in this directory,
rem which would be this launcher calling itself.
for /f "delims=" %%I in ('where.exe gh 2^>nul') do (
  if /I not "%%~dpI"=="%~dp0" (
    "%%~fI" %*
    exit /b %ERRORLEVEL%
  )
)

echo claudish gh wrapper: found neither Python nor the real gh on PATH. 1>&2
exit /b 127
