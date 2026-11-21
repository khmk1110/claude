from django import template

register = template.Library()

ACTION_LABELS = {
    "payroll.calculate": "計算", "payroll.confirm": "確定", "payroll.reopen": "確定の取り消し",
    "payroll.mark_review": "確認待ちにした", "payroll.input_override": "元データの修正", "payroll.line_override": "金額の修正",
    "payroll.adhoc_add": "当月のみの項目を追加", "payroll.adhoc_remove": "当月のみの項目を削除",
    "payroll.flag_review": "確定後の元データ変更", "payroll.pdf": "明細PDFの出力",
    "attendance.update": "勤怠の修正", "attendance.confirm": "勤怠の確定", "shift.update": "シフト変更",
}


@register.filter
def action_label(code: str) -> str:
    return ACTION_LABELS.get(code, code)


@register.filter
def kv(value) -> str:
    """監査ログの変更前・変更後を読みやすく表示する。"""
    if value in (None, "", {}):
        return ""
    if isinstance(value, dict):
        names = {"net": "差引", "earnings": "支給", "deductions": "控除", "errors": "エラー", "override": "修正後",
                 "auto": "自動値", "key": "項目", "name": "項目", "amount": "金額", "kind": "区分", "note": "内容"}
        return "、".join(f"{names.get(k, k)}: {v if v is not None else '-'}" for k, v in value.items())
    return str(value)
