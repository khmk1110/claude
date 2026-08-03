@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Pythonが見つかりません。
    echo https://www.python.org/downloads/ からインストールしてから、もう一度このファイルをダブルクリックしてください。
    echo インストール時は「Add python.exe to PATH」に必ずチェックを入れてください。
    pause
    exit /b 1
)

if not exist ".venv" (
    echo 初回起動の準備をしています。少し時間がかかります...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install --quiet -r requirements.txt
    echo 準備が完了しました。
) else (
    call .venv\Scripts\activate.bat
)

echo アプリを起動しています。自動的にブラウザが開きます...
start /min "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:5000"

python app.py

echo.
echo アプリを終了しました。このウィンドウは閉じて大丈夫です。
pause
