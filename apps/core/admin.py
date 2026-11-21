from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "username", "action", "target_type", "target_repr", "ip")
    list_filter = ("action", "target_type")
    search_fields = ("username", "action", "target_repr", "reason")
    date_hierarchy = "at"

    def get_queryset(self, request):
        # 給与・経費の操作履歴（金額を含む）は、それぞれの追加認証が済んでいる場合のみ表示
        from apps.accounts import stepup

        qs = super().get_queryset(request)
        for scope in ("payroll", "expenses"):
            if not stepup.is_verified(request, scope):
                qs = qs.exclude(action__startswith=f"{scope}.")
        return qs

    # 閲覧のみ（追加・変更・削除は不可）
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
