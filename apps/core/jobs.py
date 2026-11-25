"""時間のかかる読み取り（OCR）を、裏で1件ずつ順番に行う共通の仕組み。

無料サーバーはCPUが小さいので、同時に何枚も読むと全部が遅くなって止まったように見える。
ここに入れた仕事は1本の作業スレッドが順番に片付ける。サーバーの再起動で消えた仕事は、
画面を開いた時に recover 側（各アプリ）から入れ直せるよう、今の状況（queued）を見られるようにしている。
"""

from __future__ import annotations

import queue
import threading

from django.db import close_old_connections

_jobs: "queue.Queue" = queue.Queue()
_lock = threading.Lock()
_worker = None
_pending: set = set()  # 待っている・読んでいる仕事のキー


def _loop():
    while True:
        key, func, args = _jobs.get()
        close_old_connections()
        try:
            func(*args)
        except Exception:  # noqa: BLE001
            pass
        finally:
            close_old_connections()
            with _lock:
                _pending.discard(key)
            _jobs.task_done()


def submit(key, func, *args) -> bool:
    """仕事を順番待ちに入れる。同じキーがもう入っていれば何もしない（False）。"""
    global _worker
    with _lock:
        if key in _pending:
            return False
        _pending.add(key)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True, name="ocr-jobs")
            _worker.start()
    _jobs.put((key, func, args))
    return True


def is_queued(key) -> bool:
    with _lock:
        return key in _pending
