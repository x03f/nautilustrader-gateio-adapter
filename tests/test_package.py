"""Package-level hygiene tests: imports, version, public API, source cleanliness."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import nautilus_gateio

SUBMODULES = [
    "config",
    "constants",
    "data",
    "errors",
    "execution",
    "factories",
    "futures",
    "http",
    "paper",
    "providers",
    "reconcile",
    "schemas",
    "signing",
    "symbols",
    "websocket",
]

# Strings that must never appear in released sources (internal names, local
# filesystem paths). Assembled from fragments so this test file itself passes.
_FORBIDDEN = [
    "nt" + "lab",
    "kod" + "labs",
    "octo" + "bot",
    "/" + "opt/",
]


class TestImports:
    def test_package_imports(self):
        assert nautilus_gateio is not None

    @pytest.mark.parametrize("name", SUBMODULES)
    def test_every_submodule_imports(self, name):
        module = importlib.import_module(f"nautilus_gateio.{name}")
        assert module is not None


class TestPublicApi:
    def test_version(self):
        assert nautilus_gateio.__version__ == "0.1.0"

    def test_all_names_resolve(self):
        assert len(nautilus_gateio.__all__) > 0
        for name in nautilus_gateio.__all__:
            assert getattr(nautilus_gateio, name, None) is not None, name

    def test_key_exports_present(self):
        for name in (
            "GateioDataClient",
            "GateioExecutionClient",
            "GateioLiveDataClientFactory",
            "GateioLiveExecClientFactory",
            "GateioHttpClient",
            "build_currency_pair",
            "reconcile",
        ):
            assert name in nautilus_gateio.__all__


class TestSourceCleanliness:
    def _source_files(self) -> list[Path]:
        package_dir = Path(nautilus_gateio.__file__).parent
        files = sorted(package_dir.glob("*.py"))
        assert files, "package source files not found"
        return files

    def test_no_forbidden_strings_in_sources(self):
        for path in self._source_files():
            text = path.read_text(encoding="utf-8").lower()
            for token in _FORBIDDEN:
                assert token not in text, f"forbidden string in {path.name}"

    def test_sources_contain_no_cyrillic(self):
        """Released sources must be English-only (no Cyrillic characters)."""
        for path in self._source_files():
            content = path.read_text(encoding="utf-8")
            cyrillic = [ch for ch in content if "\u0400" <= ch <= "\u04ff"]
            assert not cyrillic, f"Cyrillic characters in {path.name}"
