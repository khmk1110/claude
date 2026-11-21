"""ファイルを PostgreSQL に保存するストレージ。

サーバーにファイルを置かないため、DB の自動バックアップで資料も保全でき、サーバーの入れ替えにも強い。
公開 URL は持たない（資料は認証付きのビュー経由でのみ返す）。
"""

import os

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from .models import StoredFile

        return StoredFile

    def _open(self, name, mode="rb"):
        obj = self._model().objects.get(name=name)
        f = ContentFile(bytes(obj.content), name=name)
        return f

    def _save(self, name, content):
        content.seek(0) if hasattr(content, "seek") else None
        data = content.read()
        if isinstance(data, str):
            data = data.encode()
        self._model().objects.create(name=name, content=data, size=len(data))
        return name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        return self._model().objects.values_list("size", flat=True).get(name=name)

    def url(self, name):
        raise NotImplementedError("資料は認証付きのビューからのみ取得できます。")

    def get_available_name(self, name, max_length=None):
        base, ext = os.path.splitext(name)
        candidate, i = name, 1
        while self.exists(candidate):
            candidate = f"{base}_{i}{ext}"
            i += 1
        return candidate
