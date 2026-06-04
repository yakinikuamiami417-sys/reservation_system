@echo off
chcp 65001 >nul
cd /d "C:\Users\kirin\dev\LIFE\reservation_system"
echo.
echo  =========================================
echo   Kirin-ya Yoyaku System
echo   http://localhost:5000
echo  =========================================
echo.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo  Package check...
"C:\Users\kirin\AppData\Local\Python\bin\pip.exe" install -r requirements.txt -q 2>nul
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"
echo  Server starting...  Browser will open automatically.
echo  Press Ctrl+C to stop.
echo.
"C:\Users\kirin\AppData\Local\Python\bin\python.exe" app.py
pause