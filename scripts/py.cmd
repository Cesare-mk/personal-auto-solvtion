@echo off
REM scripts\py 的 cmd.exe / PowerShell 版本，查找顺序完全一致：
REM   1. %ONE_AGENT_PYTHON%          显式指定，最高优先级
REM   2. <项目根>\.python-path       本机固定值（不入库）
REM   3. <项目根>\.venv\Scripts\python.exe
REM   4. PATH 上的 python3 / python   （不探测 mss，PATH 上的通常就是它）
REM
REM 固化本机解释器：scripts\py.cmd --pin [路径]
setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PIN_FILE=%ROOT%\.python-path"

if /i "%~1"=="--pin" goto :pin

if defined ONE_AGENT_PYTHON (
    set "PY=%ONE_AGENT_PYTHON%"
    goto :run
)
if exist "%PIN_FILE%" (
    set /p PY=<"%PIN_FILE%"
    if defined PY goto :run
)
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
    goto :run
)
where python3 >nul 2>&1 && ( set "PY=python3" & goto :run )
where python  >nul 2>&1 && ( set "PY=python"  & goto :run )

echo scripts\py.cmd: 找不到可用的 Python。>&2
echo   ^· 装依赖见 README「首次使用」；>&2
echo   ^· 或显式指定：scripts\py.cmd --pin ^<解释器路径^>>>&2
exit /b 127

:pin
set "TARGET=%~2"
if not defined TARGET call :resolve TARGET
if not defined TARGET (
    echo scripts\py.cmd: 现在也找不到可用解释器，请显式给一个路径>&2
    exit /b 1
)
> "%PIN_FILE%" echo %TARGET%
echo [scripts\py.cmd] 已固定解释器: %TARGET%
echo                  记录在 %PIN_FILE%（本机专用，不入库）
exit /b 0

:resolve
if defined ONE_AGENT_PYTHON ( set "%~1=%ONE_AGENT_PYTHON%" & exit /b 0 )
if exist "%PIN_FILE%" (
    set /p T=<"%PIN_FILE%"
    if defined T set "%~1=!T!" & exit /b 0
)
if exist "%ROOT%\.venv\Scripts\python.exe" ( set "%~1=%ROOT%\.venv\Scripts\python.exe" & exit /b 0 )
for %%C in (python3 python) do (
    where %%C >nul 2>&1 && ( set "%~1=%%C" & exit /b 0 )
)
exit /b 1

:run
"%PY%" %*
endlocal
