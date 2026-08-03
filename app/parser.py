import re
from datetime import date

import pdfplumber

AMOUNT_KEYWORDS = [
    "ご請求金額",
    "御請求金額",
    "請求金額",
    "税込合計",
    "総合計",
    "合計金額",
    "お支払金額",
    "合計",
]

DUE_DATE_KEYWORDS = [
    "お支払い期限",
    "お支払期限",
    "支払期限",
    "振込期限",
    "支払期日",
]

ISSUE_DATE_KEYWORDS = [
    "発行年月日",
    "発行日",
    "請求日",
]

PAYEE_SECTION_KEYWORDS = [
    "お振込先",
    "振込先",
    "お振込み先",
]

AMOUNT_RE = re.compile(r"[¥￥]?\s*([0-9][0-9,]{2,})\s*(?:円|-)?")
DATE_RE = re.compile(r"(\d{4})[年/./-](\d{1,2})[月/./-](\d{1,2})日?")
COMPANY_RE = re.compile(r"(?:株式会社|有限会社)?[^\s\n　]{1,20}(?:株式会社|有限会社)")


def extract_text(pdf_path):
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    return "\n".join(text_parts)


def _find_near_keyword(lines, keywords, value_re, to_value):
    for i, line in enumerate(lines):
        for kw in keywords:
            if kw not in line:
                continue
            match = value_re.search(line)
            if match:
                value = to_value(match)
                if value is not None:
                    return value
            if i + 1 < len(lines):
                match = value_re.search(lines[i + 1])
                if match:
                    value = to_value(match)
                    if value is not None:
                        return value
    return None


def _to_amount(match):
    try:
        return int(match.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None


def _to_iso_date(match):
    try:
        y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return date(y, m, d).isoformat()
    except (ValueError, IndexError):
        return None


BANK_LINE_KEYWORDS = ["銀行", "信用金庫", "信用組合", "支店", "口座", "普通", "当座"]


def _guess_payee(lines):
    for i, line in enumerate(lines):
        for kw in PAYEE_SECTION_KEYWORDS:
            if kw not in line:
                continue
            section = lines[i + 1 : i + 6]

            for candidate_line in section:
                m = COMPANY_RE.search(candidate_line)
                if m:
                    return m.group(0).strip()

            # 会社名の正規表現に一致しない場合、口座名義らしき行(銀行情報を含まず
            # 数字を含まない行)を振込先セクションの中から拾う。
            for candidate_line in section:
                if any(k in candidate_line for k in BANK_LINE_KEYWORDS):
                    continue
                if re.search(r"\d", candidate_line):
                    continue
                if candidate_line:
                    return candidate_line

    for line in lines:
        if "御中" in line:
            continue
        m = COMPANY_RE.search(line)
        if m:
            return m.group(0).strip()
    return None


def extract_fields(pdf_path):
    """PDFからテキストを抽出し、支払先・金額・発行日・支払期限の候補を返す。
    抽出できなかった項目はNoneになるため、画面で編集して補完する前提。
    """
    text = extract_text(pdf_path)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    amount = _find_near_keyword(lines, AMOUNT_KEYWORDS, AMOUNT_RE, _to_amount)
    due_date = _find_near_keyword(lines, DUE_DATE_KEYWORDS, DATE_RE, _to_iso_date)
    issue_date = _find_near_keyword(lines, ISSUE_DATE_KEYWORDS, DATE_RE, _to_iso_date)
    payee = _guess_payee(lines)

    return {
        "payee": payee,
        "amount": amount,
        "issue_date": issue_date,
        "due_date": due_date,
        "raw_text": text,
    }
