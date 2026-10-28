@echo off
title Restart DeviceHub
echo Restarting DeviceHub (WSL service devhub)...
wsl.exe -u root -e systemctl restart devhub
if errorlevel 1 (
  echo Could not restart the service.
  pause
  exit /b 1
)
for /l %%i in (1,1,20) do (
  curl.exe -s -m 2 -o nul http://127.0.0.1:8765/api/ping && goto up
  ping -n 2 127.0.0.1 >nul
)
echo.
echo DeviceHub runs in WSL but Windows cannot reach http://127.0.0.1:8765.
echo If it stays like that, run "Restart WSL and DeviceHub.bat".
pause
exit /b 1
:up
echo DeviceHub is up again (open pages reload themselves).
ping -n 3 127.0.0.1 >nul
