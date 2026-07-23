"""Settings entry point for django-stubs' mypy plugin only.

The plugin imports the configured settings module to introspect real
Django/model types, regardless of environment — it has no notion of
"production" vs. "dev". Rather than weaken config/settings.py's fail-closed
SECRET_KEY/DEBUG handling for tooling's sake, point django-stubs (see
[tool.django-stubs] in pyproject.toml) at this shim instead. Never imported
at runtime.
"""

import os

os.environ.setdefault("DJANGO_DEBUG", "true")

from .settings import *  # noqa: E402, F401, F403
