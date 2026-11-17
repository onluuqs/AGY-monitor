@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title AGY 实时运行监控台 - 一键安装向导

echo ========================================================
echo   AGY-monitor 实时运行与任务监控台 (v2.2.0) 一键安装
echo ========================================================
echo.

set "SRC_PLUGIN="
if exist "%~dp0ag-monitor\server.py" set "SRC_PLUGIN=%~dp0ag-monitor"
if not defined SRC_PLUGIN if exist "%~dp0server.py" set "SRC_PLUGIN=%~dp0"

if not defined SRC_PLUGIN (
    echo [错误] 未找到 server.py 插件文件，请先完整解压压缩包后再运行！
    pause
    exit /b 1
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
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\ProgramData\anaconda3\python.exe"
        "C:\ProgramData\miniconda3\python.exe"
        "D:\anaconda3\python.exe"
        "D:\anaconda\python.exe"
        "D:\miniconda3\python.exe"
    ) do (
        if not defined PY_EXE if exist "%%~D" set "PY_EXE=%%~D"
    )
)

if not defined PY_EXE (
    echo [提示] 未在默认路径自动找到 Python 3，请输入您电脑上 python.exe 的完整路径：
    set /p "PY_EXE=python.exe 路径: "
)

if not exist "%PY_EXE%" (
    echo [错误] 找不到指定的 Python 解释器: %PY_EXE%
    pause
    exit /b 1
)

echo [1/3] 已识别 Python 环境: %PY_EXE%

set "DEST_DIR=%USERPROFILE%\.gemini\config\plugins\ag-monitor"
echo [2/3] 正在安装全局插件至: %DEST_DIR%
if not exist "%DEST_DIR%" mkdir "%DEST_DIR%"

for %%F in (plugin.json mcp_config.json server.py index.html run_mcp.cmd start_monitor.bat logo.svg README.md) do (
    if exist "%SRC_PLUGIN%\%%F" copy /Y "%SRC_PLUGIN%\%%F" "%DEST_DIR%\%%F" >nul
)

"%PY_EXE%" -c "import json, os, sys; py=sys.argv[1]; dst=sys.argv[2]; cfg={'mcpServers':{'ag-monitor-daemon':{'command':py,'args':[os.path.join(dst,'server.py'),'--mcp','--host','127.0.0.1','--port','8765']}}}; open(os.path.join(dst,'mcp_config.json'),'w',encoding='utf-8').write(json.dumps(cfg,indent=2,ensure_ascii=False)+'\n')" "%PY_EXE%" "%DEST_DIR%"

echo [3/3] 正在启动本地监控服务并打开面板 (http://127.0.0.1:8765) ...
call "%DEST_DIR%\start_monitor.bat"

echo.
echo [安装完成] 以后每次打开 Antigravity 客户端，监控服务都会自动同步启动！
echo            日常直接浏览器访问 http://127.0.0.1:8765 即可查看。
echo.
pause
