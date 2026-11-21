from django.contrib import admin

from .models import AttendanceDay, AttendanceMonthLock, PaidLeaveAdjustment, PaidLeaveGrant, PaidLeaveRequest

admin.site.register([AttendanceMonthLock, PaidLeaveGrant, PaidLeaveRequest, PaidLeaveAdjustment])


@admin.register(AttendanceDay)
class AttendanceDayAdmin(admin.ModelAdmin):
    list_display = ("date", "member", "planned_work_type", "actual_work_type", "status")
    list_filter = ("status",)
    date_hierarchy = "date"
