"""氏名からメンバーを特定する（表記揺れ・姓のみ・同姓に対応）。推測では確定しない。"""

from __future__ import annotations

from dataclasses import dataclass

from apps.core.text import normalize_name

from .models import Member, MemberAlias


@dataclass
class MatchResult:
    member: Member | None
    candidates: list
    method: str  # exact / alias / prefix / none / ambiguous

    @property
    def ok(self) -> bool:
        return self.member is not None


# 名前でよく使われる旧字・異体字を同じ字として扱う（例: 中沢＝中澤、山崎＝山﨑、高橋＝髙橋）
_VARIANTS = str.maketrans({
    "澤": "沢", "濵": "浜", "濱": "浜", "邊": "辺", "邉": "辺", "齋": "斎", "齊": "斉", "髙": "高", "﨑": "崎", "嵜": "崎",
    "廣": "広", "嶋": "島", "嶌": "島", "櫻": "桜", "國": "国", "眞": "真", "關": "関", "瀨": "瀬", "藏": "蔵", "惠": "恵",
    "德": "徳", "冨": "富", "槇": "槙", "籠": "篭", "龍": "竜", "實": "実", "條": "条", "淵": "渕",
})


def _fold(s: str) -> str:
    return normalize_name(s).translate(_VARIANTS)


def _prefer_active(found: list) -> list:
    """候補が複数の時は、在籍中の人を優先する（退職者と同じ名字でも在籍中が1人ならその人）。"""
    active = [m for m in found if m.status == Member.Status.ACTIVE]
    return active if active else found


def match_member(text: str, *, include_retired: bool = True) -> MatchResult:
    """名前・別名・名字だけ・下の名前だけで特定する。同じ名字の人が複数いる時だけ「ambiguous」（選んでもらう）。"""
    key = _fold(text)
    if not key:
        return MatchResult(None, [], "none")
    qs = Member.objects.all() if include_retired else Member.objects.filter(status=Member.Status.ACTIVE)
    members = list(qs)

    def result(found, method):
        found = _prefer_active(found)
        if len(found) == 1:
            return MatchResult(found[0], found, method)
        return MatchResult(None, found, "ambiguous")

    exact = [m for m in members if _fold(m.name) == key]
    if exact:
        return result(exact, "exact")
    alias = list({a.member for a in MemberAlias.objects.filter(member__in=qs).select_related("member") if _fold(a.alias) == key})
    if alias:
        return result(alias, "alias")
    # 名字だけ（名前の先頭と一致）
    prefix = [m for m in members if _fold(m.name).startswith(key)]
    if prefix:
        return result(prefix, "prefix")
    # 下の名前だけ（「矢木 淳之介」の「淳之介」）
    given = [m for m in members if " " in (m.name or "").replace("\u3000", " ") and
             _fold(m.name.replace("\u3000", " ").split(" ", 1)[1]) == key]
    if given:
        return result(given, "given")
    return MatchResult(None, [], "none")
