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

    actions = None  # 一括削除は使わない

    def has_delete_permission(self, request, obj=None):
        # 記録が1件もない人（登録の誤りなど）だけ削除できる。記録がある人は退職処理で履歴を残す
        if obj is None:
            return False
        return not obj.has_history() and super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        from apps.core import audit

        audit.log("member.delete", target=obj, before={"name": obj.name, "member_no": obj.member_no,
                                                         "employment_type": obj.get_employment_type_display()}, request=request)
        super().delete_model(request, obj)


@admin.register(CardCode)
class CardCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "member", "label", "ignored", "is_active")
