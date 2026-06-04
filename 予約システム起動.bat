@echo off
cd /d C:\Users\kirin\dev\LIFE\reservation_system

:: バックアップフォルダ作成
if not exist "backup" mkdir backup

:: 日付を取得してバックアップ（例: reservations_20260529.db）
set DATESTAMP=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%
set BACKUP_FILE=backup\reservations_%DATESTAMP%.db

:: reservations.dbが存在する場合のみバックアップ
if exist "reservations.db" (
    copy /Y "reservations.db" "%BACKUP_FILE%" >nul
    echo バックアップ完了: %BACKUP_FILE%
) else (
    echo データベースがまだありません（初回起動）
)

echo 予約システムを起動中...
echo iPadからは http://192.168.0.5:5000 でアクセスしてください
"C:\Users\kirin\AppData\Local\Python\bin\python.exe" app.py
pause
start http://192.168.0.5:5000
