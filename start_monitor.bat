@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title AGY 实时运行与任务监控台
set "SCRIPT_DIR=%~dp0"
set "URL=http://127.0.0.1:8765"

netstat -ano | findstr "127.0.0.1:8765" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    start "" "%URL%"
    exit /b 0
)

set "PY_EXE="
for %%C in ("python" "python3") do (
    for /f "delims=" %%P in ('where %%~C 2^>nul') do (
        echo "%%P" | findstr /i "WindowsApps" >nul
        if errorlevel 1 if not defined PY_EXE set "PY_EXE=%%P"
    )
)
if not defined PY_EXE (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if exist "%%P" set "PY_EXE=%%P"
    )
)
if not defined PY_EXE (
    for %%D in (
        "%CONDA_PREFIX%\python.exe"
        "%USERPROFILE%\anaconda3\python.exe"
        "%USERPROFILE%\miniconda3\python.exe"
        "C:\ProgramData\anaconda3\python.exe"
        "D:\anaconda3\python.exe"
        "D:\anaconda\python.exe"
        "D:\miniconda3\python.exe"
    ) do (
        if not defined PY_EXE if exist "%%~D" set "PY_EXE=%%~D"
    )
)

if not defined PY_EXE (
    echo [错误] 未检测到可用的 Python 3 环境，请先安装 Python 3.9+。
    pause
    exit /b 1
)

for %%F in ("%PY_EXE%") do set "PYW_CAND=%%~dpFpythonw.exe"
if exist "%PYW_CAND%" (
    start "" "%PYW_CAND%" "%SCRIPT_DIR%server.py" --host 127.0.0.1 --port 8765
) else (
    start "" /min "%PY_EXE%" "%SCRIPT_DIR%server.py" --host 127.0.0.1 --port 8765
)
timeout /t 1 /nobreak >nul
start "" "%URL%"
