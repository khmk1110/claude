from django.contrib import admin

from apps.accounts.admin_mixins import StepUpAdminMixin

from . import models as m


class PayrollAdmin(StepUpAdminMixin, admin.ModelAdmin):
    stepup_scope = "payroll"


class ReadOnlyWhenConfirmed:
    def has_change_permission(self, request, obj=None):
        statement = getattr(obj, "statement", obj)
        if isinstance(statement, m.PayrollStatement) and statement.is_confirmed:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        statement = getattr(obj, "statement", obj)
        if isinstance(statement, m.PayrollStatement) and statement.is_confirmed:
            return False
        return super().has_delete_permission(request, obj)


class PayrollInline(StepUpAdminMixin, ReadOnlyWhenConfirmed, admin.TabularInline):
    stepup_scope = "payroll"
    extra = 0


class InputInline(PayrollInline):
    model = m.PayrollInput


class LineInline(PayrollInline):
    model = m.PayrollLine


@admin.register(m.PayrollStatement)
class StatementAdmin(ReadOnlyWhenConfirmed, PayrollAdmin):
    list_display = ("month", "member", "employment_type", "status", "net_amount", "needs_review")
    list_filter = ("status", "employment_type", "month")
    readonly_fields = ("snapshot", "confirmed_at", "confirmed_by", "calculated_at")
    inlines = [InputInline, LineInline]


class MembershipInline(StepUpAdminMixin, admin.TabularInline):
    stepup_scope = "payroll"
    model = m.TeamMembership
    extra = 0


@admin.register(m.Team)
class TeamAdmin(PayrollAdmin):
    inlines = [MembershipInline]


@admin.register(m.PayrollItemDefinition)
class ItemAdmin(PayrollAdmin):
    list_display = ("display_order", "name", "kind", "method", "scope", "is_active")
    list_display_links = ("name",)
    list_filter = ("kind", "method", "scope", "is_active")
    filter_horizontal = ("members",)


for model in (m.RateTable, m.Product, m.SalesCount, m.BaseSalary, m.PayrollMonth):
    admin.site.register(model, PayrollAdmin)
