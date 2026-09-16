@echo off
REM ============================================================================
REM  Stop the relay daemons started by start-all.bat.
REM
REM  ASCII-ONLY ON PURPOSE -- see the note at the top of start-all.bat for why
REM  Chinese text in a .bat file breaks cmd.exe's parser on zh-CN Windows.
REM
REM  This does NOT stop wait_events.py: that is the Agent-side blocking loop
REM  and is owned by the Claude Code session, not by this script.
REM ============================================================================
setlocal
set "ROOT=%~dp0.."
set "PY=python"
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"

"%PY%" "%~dp0launcher.py" stop

endlocal
