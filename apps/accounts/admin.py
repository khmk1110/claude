from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import StepUpAttempt, StepUpPasswordHistory, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (("社内設定", {"fields": ("role", "display_name")}),)
    list_display = ("username", "display_name", "role", "is_active", "last_login")


@admin.register(StepUpPasswordHistory)
class StepUpPasswordHistoryAdmin(admin.ModelAdmin):
    list_display = ("scope", "changed_at", "changed_by", "note")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StepUpAttempt)
class StepUpAttemptAdmin(StepUpPasswordHistoryAdmin):
    list_display = ("at", "user", "scope", "success", "ip")
    list_filter = ("scope", "success")
