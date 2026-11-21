from django.contrib import admin

from .models import ShiftEntry, ShiftMonth, WorkType


@admin.register(WorkType)
class WorkTypeAdmin(admin.ModelAdmin):
    list_display = ("display_order", "name", "short_label", "attendance_days", "paid_leave_days", "is_active")
    list_display_links = ("name",)


admin.site.register(ShiftMonth)


@admin.register(ShiftEntry)
class ShiftEntryAdmin(admin.ModelAdmin):
    list_display = ("date", "member", "work_type", "updated_by", "updated_at")
    list_filter = ("work_type",)
    date_hierarchy = "date"
