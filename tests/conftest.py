"""Test bootstrap.

* Importing ``core.api`` cold trips a circular import inside lyndrix-core
  (core.api → plugins → ui.layout → notifications → core.api). The running
  platform never hits it because main.py loads core.services first; mirror
  that order here before any plugin module pulls in core.api.
* ``entrypoint.py`` uses package-relative imports (``from .app…``); expose the
  plugin root as the package ``monitoring_plugin`` so tests can import it.
"""
# TODO(agent): fix the core.api ↔ ui.layout cycle in lyndrix-core and drop this.
import core.services  # noqa: F401

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if "monitoring_plugin" not in sys.modules:
    pkg = types.ModuleType("monitoring_plugin")
    pkg.__path__ = [str(_ROOT)]  # type: ignore[attr-defined]
    sys.modules["monitoring_plugin"] = pkg
