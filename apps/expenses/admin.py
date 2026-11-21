from django.contrib import admin

from apps.accounts.admin_mixins import StepUpAdminMixin

from . import models as m


class ExpensesAdmin(StepUpAdminMixin, admin.ModelAdmin):
    stepup_scope = "expenses"


class IssueInline(StepUpAdminMixin, admin.TabularInline):
    stepup_scope = "expenses"
    model = m.ExpenseIssue
    extra = 0


@admin.register(m.ExpenseRecord)
class RecordAdmin(ExpensesAdmin):
    list_display = ("source_file", "row_number", "auto_member", "member", "auto_amount", "amount", "status")
    list_filter = ("status",)
    inlines = [IssueInline]


for model in (m.ExpenseCategory, m.ImportProfile, m.SourceFile, m.ExpenseIssue):
    admin.site.register(model, ExpensesAdmin)
