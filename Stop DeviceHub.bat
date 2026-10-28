@echo off
title Stop DeviceHub
echo Stopping DeviceHub (WSL service devhub)...
echo The shared SSH connections it opened stay up and are taken over again on the next start.
wsl.exe -u root -e systemctl stop devhub
if errorlevel 1 (
  echo Could not stop the service.
  pause
  exit /b 1
)
echo DeviceHub stopped. Sessions' devhub commands will start it again when they need it;
echo it also starts by itself the next time WSL starts.
ping -n 4 127.0.0.1 >nul
