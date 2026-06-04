@echo off
cd /d "%~dp0"
echo.
echo  =========================================
echo   ^^ Kirin-ya Reservation System
echo   ^^ (C) Kirin-ya 2026
echo  =========================================
echo.
echo  Flaskをインストール中...
pip install -r requirements.txt -q
echo.
echo  サーバー起動中 → http://localhost:5000
echo  停止: Ctrl+C
echo.
python app.py
pause
