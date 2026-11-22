FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OMP_THREAD_LIMIT=1

# OCR（画像PDF）・PDF の画像化・日本語フォント（明細PDF / シフト画像）
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-jpn poppler-utils fonts-ipafont-gothic fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN DEBUG=False SECRET_KEY=build-only DATABASE_URL=postgres://x:x@localhost/x python manage.py collectstatic --noinput

RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
# 起動時に DB の更新と初期設定（未設定の場合のみ）を行う。無料プランのメモリに合わせて 1 プロセス・複数スレッド。
# 画像PDFの読み取り（OCR）は処理能力の低いプランでは時間がかかるため、タイムアウトを長めにする。
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py bootstrap && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4 --timeout 300"]
