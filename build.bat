@echo off
setlocal
cd /d "%~dp0"

echo === MicroBackUp Build Script ===

where python >nul 2>nul
if %errorlevel% equ 0 (
    set PYTHON=python
    goto :found_python
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    set PYTHON=py
    goto :found_python
)

echo Python не найден. Установите Python 3.10+ и добавьте его в PATH.
pause
exit /b 1

:found_python
echo Installing build dependencies from requirements-build.txt (including PyInstaller)...
%PYTHON% -m pip install -r requirements-build.txt
if %errorlevel% neq 0 (
    echo Failed to install dependencies.
    pause
    exit /b %errorlevel%
)

%PYTHON% build.py
set BUILD_EXIT=%errorlevel%

echo.
pause
exit /b %BUILD_EXIT%
