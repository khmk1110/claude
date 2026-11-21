from django.contrib import admin

from .models import CardCode, Member, MemberAlias, MonthlyStandardOverride


class MemberAliasInline(admin.TabularInline):
    model = MemberAlias
    extra = 0


class MonthlyStandardInline(admin.TabularInline):
    model = MonthlyStandardOverride
    extra = 0


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ("display_order", "member_no", "name", "employment_type", "is_agency", "status")
    list_display_links = ("member_no", "name")
    list_filter = ("employment_type", "status", "is_agency")
    search_fields = ("name", "name_kana", "member_no", "aliases__alias")
    inlines = [MemberAliasInline, MonthlyStandardInline]

    def has_delete_permission(self, request, obj=None):
        return False  # 退職者も削除しない（退職処理で対応）


@admin.register(CardCode)
class CardCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "member", "label", "ignored", "is_active")
