"""PDF の文字抽出と OCR（画像PDF用）。

- 文字情報のある PDF は pdfplumber で抽出する
- 文字情報がない／読めない PDF は、ページを画像にして表の罫線を消し、Tesseract（日本語）で読み取る
OCR の結果は誤読の可能性があるため、呼び出し側で必ず「確認待ち」にする。
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float

    @property
    def cx(self) -> float:
        return self.left + self.width / 2


@dataclass
class Line:
    words: list
    top: int

    @property
    def text(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.left))


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None and shutil.which("pdftoppm") is not None


def pdf_text_pages(data: bytes) -> list[str]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [(p.extract_text() or "") for p in pdf.pages]


def render_pages(data: bytes, dpi: int = 300, first: int | None = None, last: int | None = None) -> list[Image.Image]:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(data)
        cmd = ["pdftoppm", "-r", str(dpi), "-png"]
        if first:
            cmd += ["-f", str(first)]
        if last:
            cmd += ["-l", str(last)]
        subprocess.run([*cmd, str(src), str(Path(tmp) / "p")], check=True, capture_output=True, timeout=120)
        return [Image.open(p).copy() for p in sorted(Path(tmp).glob("p-*.png"), key=lambda p: int(p.stem.split("-")[-1]))]


def _long_runs(mask: np.ndarray, minlen: int) -> np.ndarray:
    """各行で minlen 以上続く True の区間を返す（罫線の検出）。"""
    out = np.zeros_like(mask)
    padded = np.pad(mask.astype(np.int8), ((0, 0), (1, 1)))
    diff = np.diff(padded, axis=1)
    for i in range(mask.shape[0]):
        starts = np.where(diff[i] == 1)[0]
        ends = np.where(diff[i] == -1)[0]
        for s, e in zip(starts, ends):
            if e - s >= minlen:
                out[i, s:e] = True
    return out


def remove_grid_lines(img: Image.Image, minlen: int = 120) -> Image.Image:
    a = np.array(img.convert("L"))
    dark = a < 160
    lines = _long_runs(dark, minlen) | _long_runs(dark.T, minlen).T
    a = a.copy()
    a[lines] = 255
    return Image.fromarray(a)


def ocr_words(img: Image.Image, lang: str = "jpn+eng", psm: int = 6) -> list[Word]:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "img.png"
        img.save(p)
        res = subprocess.run(
            ["tesseract", str(p), "stdout", "-l", lang, "--psm", str(psm), "tsv"],
            check=True, capture_output=True, timeout=180,
        )
    words = []
    for row in res.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        words.append(Word(cols[11].strip(), int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), float(cols[10])))
    return words


def group_lines(words: list[Word], tolerance: int = 18) -> list[Line]:
    lines: list[Line] = []
    for w in sorted(words, key=lambda w: w.top):
        if lines and abs(lines[-1].top - w.top) <= tolerance:
            lines[-1].words.append(w)
        else:
            lines.append(Line([w], w.top))
    return lines


def ocr_digits(img: Image.Image) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "img.png"
        img.save(p)
        res = subprocess.run(
            ["tesseract", str(p), "stdout", "-l", "eng", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789,"],
            check=True, capture_output=True, timeout=60,
        )
    return re.sub(r"[^\d]", "", res.stdout.decode())
