@echo off
title DeviceHub status
for /f %%s in ('wsl.exe -e systemctl is-active devhub') do set STATE=%%s
echo WSL service:     %STATE%
curl.exe -s -m 3 -o nul http://127.0.0.1:8765/api/ping && (echo From Windows:    reachable at http://localhost:8765) || (echo From Windows:    NOT reachable)
echo.
python "%~dp0devhub.py" status
echo.
pause
