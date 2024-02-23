from __future__ import annotations

try:
    import django

    django_version = django.__version__
    django_installed = True
except ImportError:
    django_installed = False
