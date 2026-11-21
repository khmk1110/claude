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


def match_member(text: str, *, include_retired: bool = True) -> MatchResult:
    key = normalize_name(text)
    if not key:
        return MatchResult(None, [], "none")
    qs = Member.objects.all() if include_retired else Member.objects.filter(status=Member.Status.ACTIVE)
    exact = list(qs.filter(name_normalized=key))
    if len(exact) == 1:
        return MatchResult(exact[0], exact, "exact")
    if len(exact) > 1:
        return MatchResult(None, exact, "ambiguous")
    alias = list({a.member for a in MemberAlias.objects.filter(alias_normalized=key, member__in=qs).select_related("member")})
    if len(alias) == 1:
        return MatchResult(alias[0], alias, "alias")
    if len(alias) > 1:
        return MatchResult(None, alias, "ambiguous")
    prefix = list(qs.filter(name_normalized__startswith=key)) if len(key) >= 1 else []
    if len(prefix) == 1:
        return MatchResult(prefix[0], prefix, "prefix")
    if len(prefix) > 1:
        return MatchResult(None, prefix, "ambiguous")
    return MatchResult(None, [], "none")
