@echo off
chcp 65001 >nul
rem Подключение компьютера к агенту (Windows). Файл скачан из чата: Настройки - Компьютер.
rem Скачивает мост в %USERPROFILE%\.agentkit и запускает первую настройку: выбор папки и автозапуск.
setlocal EnableExtensions
set "SERVER=__SERVER__"
set "KEY=__KEY__"
set "DIR=%USERPROFILE%\.agentkit"
title Подключение компьютера к агенту
if not exist "%DIR%" mkdir "%DIR%"

echo Скачиваю мост с %SERVER% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing '%SERVER%/bridge.py' -OutFile '%DIR%\bridge.py'"
if errorlevel 1 goto :nonet

call :findpy
if defined PY goto :run
echo Python не найден - устанавливаю (это один раз, пару минут)...
where winget >nul 2>nul
if errorlevel 1 goto :nopy
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if not defined PY goto :nopy

:run
echo Python: %PY%
"%PY%" "%DIR%\bridge.py" --server "%SERVER%" --key "%KEY%" --setup
echo.
pause
exit /b 0

:findpy
set "PY="
rem py.exe - лаунчер из установщика python.org; python.exe из WindowsApps - заглушка Store, её проверка отсеет.
for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%I"
if defined PY exit /b 0
for /f "delims=" %%I in ('python -c "import sys;assert sys.version_info>=(3,9);print(sys.executable)" 2^>nul') do set "PY=%%I"
if defined PY exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
exit /b 0

:nopy
echo.
echo Не получилось установить Python автоматически.
echo Откроется python.org: скачайте и установите Python 3 (галочка "Add python.exe to PATH"),
echo затем снова запустите этот файл.
start "" https://www.python.org/downloads/windows/
pause
exit /b 1

:nonet
echo.
echo Не удалось скачать мост с %SERVER%. Проверьте интернет и что сервис агента открывается в браузере.
pause
exit /b 1
