#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
  echo "Python3が見つかりません。"
  echo "https://www.python.org/downloads/ からインストールしてから、もう一度このファイルをダブルクリックしてください。"
  read -p "Enterキーを押すと閉じます..."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "初回起動の準備をしています。少し時間がかかります..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install --quiet -r requirements.txt
  echo "準備が完了しました。"
else
  source .venv/bin/activate
fi

echo "アプリを起動しています。自動的にブラウザが開きます..."
(sleep 2 && open "http://127.0.0.1:5000") &

python3 app.py

echo ""
echo "アプリを終了しました。このウィンドウは閉じて大丈夫です。"
read -p "Enterキーを押すと閉じます..."
