@echo off
rem ===========================================================================
rem  Launcher of the native messaging host.
rem
rem  Firefox starts this file and speaks the native messaging protocol over
rem  stdin/stdout. Only the protocol may go to stdout: any stray print() from
rem  Python corrupts the stream. So we run pythonw.exe - it never opens a
rem  console window and never writes to stdout on its own.
rem
rem  host.py itself is written in task 17. Until then this launcher only
rem  checks that Python exists and reports the problem on stderr.
rem
rem  Comments are ASCII-only on purpose: cmd.exe reads .bat files in the
rem  console OEM codepage and UTF-8 text in `rem` lines renders as garbage.
rem ===========================================================================

setlocal
set "HOST=%~dp0host.py"

rem MUSIC_WIDGET_PYTHON lets you point at a specific interpreter, e.g.
rem   set MUSIC_WIDGET_PYTHON=C:\Python312\pythonw.exe
if defined MUSIC_WIDGET_PYTHON (
    "%MUSIC_WIDGET_PYTHON%" "%HOST%"
    exit /b %errorlevel%
)

rem Find any python.exe on PATH.
set "PY="
for /f "delims=" %%I in ('where.exe python.exe 2^>nul') do if not defined PY set "PY=%%I"
if not defined PY goto :no_python

rem Prefer pythonw.exe - same interpreter, but no console window.
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" (
    "%PYW%" "%HOST%"
) else (
    "%PY%" "%HOST%"
)
exit /b %errorlevel%

:no_python
echo [ERROR] Python not found. Install Python 3.10+ or set MUSIC_WIDGET_PYTHON. 1>&2
exit /b 1