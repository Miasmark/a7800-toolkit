@echo off
setlocal enabledelayedexpansion
title Record a 7800 session
rem ---------------------------------------------------------------------------
rem  Record a play session so it can be replayed and measured.
rem
rem  Drag a .a78 onto this file. MAME opens as normal; play to the point you
rem  want captured, then close MAME. The recording lands in a "recordings"
rem  folder beside the cartridge as the next free run-NN.inp -- never over an
rem  old one.
rem
rem  A replay reproduces the session exactly, so two replays of one recording
rem  give identical profiles and a before-and-after number means something.
rem  Record one per build: a recording is button presses against frame
rem  numbers, and a build running at a different speed lands them elsewhere.
rem  MAME and the BIOS are found as tools\capture.py finds them (A7800_MAME,
rem  A7800_ROMPATH).
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
%PY% "%HERE%tools\session.py" record "%~1"
:finish
echo.
pause
endlocal
