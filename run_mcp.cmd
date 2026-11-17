@echo off
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "PY_EXE="

for %%C in ("python" "python3") do (
    for /f "delims=" %%P in ('where %%~C 2^>nul') do (
        echo "%%P" | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            if not defined PY_EXE set "PY_EXE=%%P"
        )
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

if not defined PY_EXE exit /b 1
"%PY_EXE%" -u "%SCRIPT_DIR%server.py" %*
