"""無料サーバーのスリープ防止（外部サービスへの登録なしで、サイトを開いたらすぐ表示されるようにする）。

Render の無料プランは、外からのアクセスが15分ないとサーバーを止める。
サーバー自身が10分おきに自分の公開アドレス（/healthz）へアクセスして、止まらないようにする。
/healthz はデータベースに触らないので、無料のデータベースは休めたまま（使える時間を節約）。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.request

log = logging.getLogger(__name__)
INTERVAL_SECONDS = 10 * 60
_started = False


def _loop(url: str) -> None:
    time.sleep(60)  # 起動直後は少し待つ
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as res:
                res.read(16)
            print(f"[keep-awake] ok {url}", flush=True)
        except Exception as e:  # noqa: BLE001 — 失敗しても次の回に試す
            print(f"[keep-awake] failed: {e}", flush=True)
        time.sleep(INTERVAL_SECONDS)


def start() -> None:
    """本番（Render 上の gunicorn）で動いている時だけ始める。テスト・管理コマンドでは動かさない。"""
    global _started
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    enabled = os.environ.get("KEEP_AWAKE", "1") == "1"
    under_server = "gunicorn" in os.path.basename(sys.argv[0]) or os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn")
    if _started or not host or not enabled or not under_server:
        return
    _started = True
    threading.Thread(target=_loop, args=(f"https://{host}/healthz",), daemon=True, name="keep-awake").start()
