from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from apps.core import audit


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit.log("auth.login", user=user, request=request, target=user)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        audit.log("auth.logout", user=user, request=request, target=user)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    audit.log(
        "auth.login_failed",
        request=request,
        target_type="auth",
        target_repr=str(credentials.get("username", ""))[:100],
    )
