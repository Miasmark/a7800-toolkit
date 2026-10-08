@echo off
setlocal enabledelayedexpansion
title Play a 7800 recording
rem ---------------------------------------------------------------------------
rem  Watch a recorded session play back.
rem
rem  Drag a .a78 onto this file, or run it from a prompt with the recording
rem  name as the second argument:   "Play a recording.bat" game.a78 run-01
rem  With no name, the recordings beside the cartridge are listed.
rem
rem  P pauses, Esc quits. When the recording runs out the game carries on
rem  under your control.
rem ---------------------------------------------------------------------------
set "HERE=%~dp0"
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo No usable Python found. Install Python 3.7 or newer and try again.
  goto :finish
)
if "%~1"=="" (
  echo Drag a .a78 cartridge onto this file.
  goto :finish
)
if "%~2"=="" (
  %PY% "%HERE%tools\session.py" list "%~1"
) else (
  %PY% "%HERE%tools\session.py" play "%~1" "%~2"
)
:finish
echo.
pause
endlocal
