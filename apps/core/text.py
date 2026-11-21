"""氏名などの表記揺れ吸収。"""

import re
import unicodedata

_SPACES = re.compile(r"\s+")


def normalize_name(value: str | None) -> str:
    """全角・半角の統一（NFKC）と、空白（全角スペースを含む）の除去。

    例: '関    佑翔' / '関佑翔' / '関　佑翔' は同じ '関佑翔' になる。
    """
    if not value:
        return ""
    return _SPACES.sub("", unicodedata.normalize("NFKC", value)).strip()
