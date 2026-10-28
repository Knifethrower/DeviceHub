@echo off
title Restart WSL and DeviceHub
echo This fixes "DeviceHub runs but Windows cannot reach it" (WSL's port forwarding stuck).
echo It shuts WSL down completely: EVERYTHING running inside WSL stops, including Claude
echo sessions' builds, chroots and test servers. DeviceHub and its device connections come back by themselves.
echo.
choice /m "Restart WSL now"
if errorlevel 2 exit /b 0
wsl.exe --shutdown
echo WSL stopped. Starting it and DeviceHub...
wsl.exe -u root -e systemctl start devhub
for /l %%i in (1,1,40) do (
  curl.exe -s -m 2 -o nul http://127.0.0.1:8765/api/ping && goto up
  ping -n 2 127.0.0.1 >nul
)
echo DeviceHub did not answer. Check: wsl.exe -u root -e systemctl status devhub
pause
exit /b 1
:up
echo DeviceHub is up.
start "" http://localhost:8765/
ping -n 3 127.0.0.1 >nul
