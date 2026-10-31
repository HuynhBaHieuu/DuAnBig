from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "Hệ thống"

    def ready(self):
        from . import signals  # noqa: F401
