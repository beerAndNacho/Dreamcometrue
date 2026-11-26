@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
set HOSTED_MODE=0
set DEMO_MODE=1
set AUTO_COLLECT=0
echo Open http://127.0.0.1:8000
echo Admin: http://127.0.0.1:8000/admin
echo User: admin. Password: data\settings\demo\admin-password.txt (unless ADMIN_PASSWORD is set).
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
:fail
pause
