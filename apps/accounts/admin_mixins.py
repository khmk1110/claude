from . import stepup


class StepUpAdminMixin:
    """管理画面でも給与・経費データは追加認証なしでは一切見せない。

    URL の先頭一致（/admin/payroll/ など）に加え、管理画面の autocomplete などの別経路からの
    取得もここで遮断する。
    """

    stepup_scope: str = ""

    def _verified(self, request) -> bool:
        return stepup.is_verified(request, self.stepup_scope)

    def has_module_permission(self, request):
        return self._verified(request) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._verified(request) and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._verified(request) and super().has_change_permission(request, obj)

    def has_add_permission(self, request, *args):
        return self._verified(request) and super().has_add_permission(request, *args)

    def has_delete_permission(self, request, obj=None):
        return self._verified(request) and super().has_delete_permission(request, obj)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if self._verified(request) else qs.none()
