@echo off
REM ============================================================================
REM  Start the two relay daemons: WeChat gateway + screen capture.
REM
REM  THIS FILE IS DELIBERATELY ASCII-ONLY -- DO NOT ADD CHINESE TEXT HERE.
REM  cmd.exe parses .bat files with the OEM codepage (GBK on zh-CN Windows).
REM  A UTF-8 file containing Chinese gets byte-misaligned while parsing, which
REM  silently eats characters from later lines (it turned "wait_events.py"
REM  into "ait_events.py" and reported "not a command"). All Chinese output
REM  lives in launcher.py, which handles its own encoding.
REM
REM  Run this from a terminal, not by double-clicking (the window would close
REM  before you could read the output).
REM ============================================================================
setlocal
set "ROOT=%~dp0.."
set "PY=python"
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"

"%PY%" "%~dp0launcher.py" start

endlocal
