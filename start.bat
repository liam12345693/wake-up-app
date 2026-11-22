@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 wake_UP 自建前端...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" server.py
) else (
  python server.py
)
pause
