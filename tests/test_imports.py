"""Import every module so CI fails fast on any import-time error."""
import importlib

import pytest

MODULES = [
    "app.main",
    "app.config",
    "app.database",
    "app.models",
    "app.schemas",
    "app.engine",
    "app.spotify_client",
    "app.auth",
    "app.routers.auth_routes",
    "app.routers.pages",
    "app.routers.queue",
    "app.routers.spaces",
    "app.routers.spotify",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)
