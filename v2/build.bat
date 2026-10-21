@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build Roblox Shield

set "OUT=%~dp0release"
set "SRC_CORE=%~dp0..\core"
set "ICON=%~dp0..\app_icon.ico"

rem --- Locate Python ---------------------------------------------------------
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")
)
%PY% --version >nul 2>&1 || (
    echo [LOI] Khong tim thay Python. Cai Python 3.12 tu python.org roi chay lai.
    goto :fail
)

rem --- Check sing-box --------------------------------------------------------
if not exist "%SRC_CORE%\sing-box.exe" (
    echo [LOI] Khong thay "%SRC_CORE%\sing-box.exe".
    goto :fail
)

rem --- Ensure PyInstaller ----------------------------------------------------
%PY% -m PyInstaller --version >nul 2>&1 || (
    echo [*] Dang cai PyInstaller...
    %PY% -m pip install --disable-pip-version-check -q pyinstaller || goto :fail
)

%PY% -c "import pystray, PIL" >nul 2>&1 || (
    echo [*] Dang cai pystray, Pillow...
    %PY% -m pip install --disable-pip-version-check -q pystray pillow || goto :fail
)

rem --- Build -----------------------------------------------------------------
echo [*] Dang dong goi RobloxShield.exe...
set "ICON_ARG="
if exist "%ICON%" set "ICON_ARG=--icon "%ICON%""
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --noupx ^
    --name RobloxShield %ICON_ARG% ^
    --distpath "%OUT%" --workpath "%~dp0build" --specpath "%~dp0build" ^
    app.py || goto :fail

echo [*] Dang dong goi SteamUnblock.exe...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --noupx ^
    --name SteamUnblock %ICON_ARG% ^
    --distpath "%OUT%" --workpath "%~dp0build" --specpath "%~dp0build" ^
    steam_app.py || goto :fail

echo [*] Dang dong goi RobloxGuard.exe...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --noupx ^
    --name RobloxGuard %ICON_ARG% ^
    --distpath "%OUT%" --workpath "%~dp0build" --specpath "%~dp0build" ^
    guard_app.py || goto :fail

rem --- Runtime files ---------------------------------------------------------
if not exist "%OUT%\core" mkdir "%OUT%\core"
copy /y "%SRC_CORE%\sing-box.exe" "%OUT%\core\" >nul || goto :fail
if exist "%SRC_CORE%\wintun.dll" copy /y "%SRC_CORE%\wintun.dll" "%OUT%\core\" >nul
if exist "%ICON%" copy /y "%ICON%" "%OUT%\app_icon.ico" >nul
if exist "%~dp0settings.json" if not exist "%OUT%\settings.json" copy /y "%~dp0settings.json" "%OUT%\" >nul

rem --- Desktop shortcut ------------------------------------------------------
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Roblox Shield.lnk');$s.TargetPath='%OUT%\RobloxShield.exe';$s.WorkingDirectory='%OUT%';$s.IconLocation='%OUT%\RobloxShield.exe,0';$s.Save()"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Steam Unblock.lnk');$s.TargetPath='%OUT%\SteamUnblock.exe';$s.WorkingDirectory='%OUT%';$s.IconLocation='%OUT%\SteamUnblock.exe,0';$s.Save()"

rmdir /s /q "%~dp0build" 2>nul
echo.
echo [OK] Xong! File: %OUT%\RobloxShield.exe, %OUT%\SteamUnblock.exe
echo      Da tao shortcut "Roblox Shield" va "Steam Unblock" ngoai Desktop.
pause
exit /b 0

:fail
echo.
echo [THAT BAI] Xem loi o tren.
pause
exit /b 1
