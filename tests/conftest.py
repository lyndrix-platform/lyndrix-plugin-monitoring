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

import importlib
import importlib.abc
import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if "monitoring_plugin" not in sys.modules:
    pkg = types.ModuleType("monitoring_plugin")
    pkg.__path__ = [str(_ROOT)]  # type: ignore[attr-defined]
    sys.modules["monitoring_plugin"] = pkg


class _SharedAppFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Make ``monitoring_plugin.app.*`` the SAME module objects as ``app.*``.

    Tests import the service as ``app.controller.service`` while
    ``entrypoint.py`` uses package-relative imports (``from .app…`` →
    ``monitoring_plugin.app…``). Without this, the model module is executed
    twice, every ``index=True`` column registers a second Index on the shared
    ``Base.metadata`` and ``create_all`` fails with "index … already exists"
    for whichever test runs after the entrypoint was imported.
    """

    PREFIX = "monitoring_plugin.app"

    def find_spec(self, name, path=None, target=None):
        if name == self.PREFIX or name.startswith(self.PREFIX + "."):
            return importlib.util.spec_from_loader(name, self)
        return None

    def create_module(self, spec):
        return importlib.import_module(spec.name[len("monitoring_plugin."):])

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _SharedAppFinder())
