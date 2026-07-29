"""Package-level hygiene: imports, version, public API, source cleanliness, packaging.

Three groups of guarantees are checked here.

1. **Import health.** Every module in the package tree imports cleanly. The
   module list is derived by walking the tree, never hand-maintained, so a new
   module is covered the moment it is added. The test suite itself is held to
   the same standard: it must import only modules the package still has, and it
   must collect as a whole.
2. **Source cleanliness.** No released source file references internal
   infrastructure and every file is English-only. The scan walks the tree
   recursively; a lower bound on the number of scanned files makes a future
   layout change unable to shrink the scan silently.
3. **Packaging.** The built wheel contains every sub-package, and every
   documented public import works from the *installed* wheel rather than from
   the source tree.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import nautilus_gateio

PACKAGE_NAME = "nautilus_gateio"
PACKAGE_DIR = Path(nautilus_gateio.__file__).resolve().parent


def find_repo_root() -> Path:
    """Locate the directory holding ``pyproject.toml``.

    ``PACKAGE_DIR.parent`` is correct for a source checkout and for an editable
    install; walking up from this test file covers the case where the package
    under test was installed elsewhere.
    """
    for candidate in (PACKAGE_DIR.parent, *Path(__file__).resolve().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("could not locate pyproject.toml")


REPO_ROOT = find_repo_root()

#: The test suite's own directory, scanned by the layout guard below.
TESTS_DIR = Path(__file__).resolve().parent

#: The sub-packages introduced by the v0.2.0 layout. Each must be importable and
#: must survive the wheel build.
SUBPACKAGES = ("common", "http", "websocket")

#: Lower bound on the number of modules/source files. The flat v0.1.0 layout had
#: 8 top-level modules; a scan that finds fewer than this has stopped recursing.
MIN_MODULES = 25

# Strings that must never appear in released sources (internal names, local
# filesystem paths). Assembled from fragments so this test file itself passes.
FORBIDDEN = [
    "nt" + "lab",
    "kod" + "labs",
    "octo" + "bot",
    "/" + "opt/",
]


def walk_module_names() -> list[str]:
    """Every module in the package tree, found recursively."""
    names = [PACKAGE_NAME]
    names += [
        name
        for _, name, _ in pkgutil.walk_packages(
            nautilus_gateio.__path__,
            prefix=f"{PACKAGE_NAME}.",
        )
    ]
    return sorted(names)


def source_files() -> list[Path]:
    """Every Python source file in the package tree, found recursively."""
    return sorted(p for p in PACKAGE_DIR.rglob("*.py") if "__pycache__" not in p.parts)


MODULE_NAMES = walk_module_names()


class TestImports:
    def test_package_imports(self):
        assert nautilus_gateio is not None

    def test_the_module_walk_finds_the_whole_tree(self):
        """Regression: the module list must be derived, not hand-maintained."""
        assert len(MODULE_NAMES) >= MIN_MODULES, MODULE_NAMES

    @pytest.mark.parametrize("subpackage", SUBPACKAGES)
    def test_every_subpackage_is_present(self, subpackage):
        assert f"{PACKAGE_NAME}.{subpackage}" in MODULE_NAMES
        assert any(name.startswith(f"{PACKAGE_NAME}.{subpackage}.") for name in MODULE_NAMES)

    @pytest.mark.parametrize("name", MODULE_NAMES)
    def test_every_module_imports(self, name):
        assert importlib.import_module(name) is not None

    def test_no_module_is_left_out_of_the_walk(self):
        """Every source file must correspond to a walked module."""
        from_files = {
            str(path.relative_to(PACKAGE_DIR).with_suffix("")).replace("/", ".")
            for path in source_files()
        }
        walked = {name[len(PACKAGE_NAME) + 1 :] or "__init__" for name in MODULE_NAMES[1:]} | {
            "__init__"
        }
        missing = {
            candidate
            for candidate in from_files
            if candidate not in walked and candidate.removesuffix(".__init__") not in walked
        }
        assert not missing, f"source files with no walked module: {sorted(missing)}"


# -- the test suite must target the layout the package actually has -----------
#
# When the package was split into sub-packages, the test modules were left
# importing the retired flat ones. `pytest` then stopped at "errors during
# collection", so for as long as it lasted nothing validated the new code at
# all - and a collection error is easy to step around by naming a subset on the
# command line, which is how it survived. The guards below name the offending
# file and the module it wants, instead of failing on whichever module pytest
# happened to import first.


def _adapter_imports_in(path: Path) -> list[tuple[str, str]]:
    """Return ``(module, imported name)`` for the adapter imports in one file.

    Parsed rather than imported: a module that imports something removed is
    precisely the case this has to report, and importing it to find out would
    raise before anything could be reported. ``ast`` also sees imports inside
    functions and fixtures, which a collection run reaches only if the test
    that holds them is selected.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                (alias.name, "")
                for alias in node.names
                if alias.name == PACKAGE_NAME or alias.name.startswith(f"{PACKAGE_NAME}.")
            ]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                continue  # a relative import, which never names the adapter
            if module != PACKAGE_NAME and not module.startswith(f"{PACKAGE_NAME}."):
                continue
            found += [(module, alias.name) for alias in node.names]
    return found


def suite_adapter_imports() -> list[tuple[str, str, str]]:
    """``(test module, adapter module, imported name)`` for the whole suite."""
    return [
        (path.name, module, name)
        for path in sorted(TESTS_DIR.glob("*.py"))
        for module, name in _adapter_imports_in(path)
    ]


class TestSuiteTargetsTheCurrentLayout:
    def test_the_scan_reaches_the_test_modules(self):
        """A scan that finds nothing would let the guard below pass vacuously."""
        imports = suite_adapter_imports()
        scanned = {source for source, _, _ in imports}
        assert len(scanned) >= 10, f"only {len(scanned)} test modules import the adapter"
        assert len(imports) >= MIN_MODULES, f"only {len(imports)} adapter imports found"

    def test_every_adapter_import_in_the_suite_resolves(self):
        offenders: list[str] = []
        for source, module, name in suite_adapter_imports():
            try:
                imported = importlib.import_module(module)
            except ImportError as exc:
                offenders.append(f"{source}: {module} ({exc})")
                continue
            if name and not hasattr(imported, name):
                offenders.append(f"{source}: {module} has no attribute {name!r}")
        assert not offenders, "the test suite imports what the package does not have: " + "; ".join(
            offenders,
        )

    def test_the_whole_suite_collects(self):
        """The condition the 0.1.0 layout broke: `pytest` must reach every test.

        Deliberately a whole-suite collection rather than an import of each
        module in turn: collection is what a contributor and CI actually run,
        and it fails on a broken fixture or a conftest as well as on an import.
        """
        result = subprocess.run(  # noqa: S603 - fixed argv, our own suite
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                str(TESTS_DIR),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert result.returncode == 0, (
            "the test suite does not collect:\n" + (result.stdout + result.stderr)[-2000:]
        )


class TestPublicApi:
    def test_version_matches_pyproject(self):
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert nautilus_gateio.__version__ == pyproject["project"]["version"]

    def test_version_is_exported(self):
        assert "__version__" in nautilus_gateio.__all__

    def test_all_is_a_sorted_unique_list(self):
        names = nautilus_gateio.__all__
        assert names
        assert len(names) == len(set(names)), "duplicate names in __all__"
        assert names == sorted(names), "__all__ is not sorted"

    @pytest.mark.parametrize("name", sorted(nautilus_gateio.__all__))
    def test_every_exported_name_resolves(self, name):
        assert getattr(nautilus_gateio, name, None) is not None

    @pytest.mark.parametrize("name", sorted(nautilus_gateio.__all__))
    def test_every_exported_name_is_importable_from_the_package(self, name):
        namespace: dict[str, object] = {}
        exec(f"from {PACKAGE_NAME} import {name}", namespace)  # noqa: S102
        assert name in namespace

    @pytest.mark.parametrize(
        "name",
        [
            "GATEIO",
            "GATEIO_VENUE",
            "GateioDataClient",
            "GateioDataClientConfig",
            "GateioExecClientConfig",
            "GateioExecutionClient",
            "GateioHttpClient",
            "GateioInstrumentProvider",
            "GateioLiveDataClientFactory",
            "GateioLiveExecClientFactory",
            "GateioProductType",
            "gateio_to_instrument_id",
            "instrument_id_to_gateio",
        ],
    )
    def test_documented_entry_points_are_exported(self, name):
        assert name in nautilus_gateio.__all__

    @pytest.mark.parametrize("name", ["paper", "reconcile", "schemas", "constants", "symbols"])
    def test_removed_v0_1_0_modules_are_gone(self, name):
        """The flat layout is retired; these names must not resurface top level."""
        assert f"{PACKAGE_NAME}.{name}" not in MODULE_NAMES


class TestSourceCleanliness:
    """The repo's only automated guard against leaking internal references."""

    def test_the_scan_covers_the_whole_tree(self):
        """Regression for a scan that silently stopped recursing when the
        package became sub-packaged (it then covered 8 of 27 files)."""
        files = source_files()
        assert len(files) >= MIN_MODULES, f"only {len(files)} source files scanned"
        for subpackage in SUBPACKAGES:
            assert any(subpackage in path.relative_to(PACKAGE_DIR).parts for path in files), (
                f"no file from {subpackage}/ was scanned"
            )

    @pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
    def test_no_forbidden_strings(self, path):
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN:
            assert token not in text, f"forbidden string {token!r} in {path}"

    @pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
    def test_sources_are_english_only(self, path):
        content = path.read_text(encoding="utf-8")
        cyrillic = sorted({ch for ch in content if "\u0400" <= ch <= "\u04ff"})
        assert not cyrillic, f"Cyrillic characters {cyrillic} in {path}"

    @pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
    def test_sources_carry_no_emoji(self, path):
        content = path.read_text(encoding="utf-8")
        emoji = sorted({ch for ch in content if ord(ch) >= 0x1F000})
        assert not emoji, f"emoji {emoji} in {path}"

    def test_py_typed_marker_is_present(self):
        assert (PACKAGE_DIR / "py.typed").is_file()

    @pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
    def test_no_module_logs_through_the_standard_library(self, path):
        """Every component logs through the platform's logging subsystem.

        A module holding its own `logging.getLogger` writes where the Nautilus
        log file, `log_level`, `log_level_file` and `log_component_levels`
        cannot reach it. The WebSocket transport did exactly that while
        `instruments.py` used the platform `Logger`, so the same run answered
        the operator's configuration in one place and ignored it in another.
        Regression guard for the whole tree, not just the file that was fixed.
        """
        text = path.read_text(encoding="utf-8")
        assert "getLogger" not in text, f"standard library logger in {path}"
        assert "import logging" not in text, f"standard library logging imported in {path}"


class TestVersionControlCoverage:
    """Every source file must reach a fresh clone.

    Regression for `.gitignore` carrying a bare `credentials*` secrets rule,
    which silently excluded `nautilus_gateio/common/credentials.py`. The module
    existed on disk, so imports, tests and even the locally built wheel were all
    green; only a clean checkout was missing it, and it failed there at import
    time. Building the wheel from the working tree cannot catch this class of
    defect, because the working tree is exactly what is misleading.
    """

    def tracked_paths(self) -> set[Path] | None:
        """Paths git would hand a fresh clone, or None outside a git checkout."""
        root = find_repo_root()
        if not (root / ".git").exists() or shutil.which("git") is None:
            return None
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", str(PACKAGE_DIR.relative_to(root))],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return {root / name for name in result.stdout.split("\0") if name}

    def test_every_source_module_is_tracked(self):
        tracked = self.tracked_paths()
        if tracked is None:
            pytest.skip("not a git checkout")
        untracked = sorted(str(path) for path in source_files() if path not in tracked)
        assert not untracked, f"source files missing from version control: {untracked}"

    def test_no_ignore_rule_matches_a_source_module(self):
        """Fail loudly even before a file is added, naming the rule at fault."""
        root = find_repo_root()
        if not (root / ".git").exists() or shutil.which("git") is None:
            pytest.skip("not a git checkout")
        result = subprocess.run(
            ["git", "check-ignore", "-v", "--no-index", "--", *map(str, source_files())],
            cwd=root,
            capture_output=True,
            text=True,
        )
        # Exit code 1 means nothing matched, which is the outcome we want.
        assert result.returncode == 1, f"ignore rules match source files:\n{result.stdout}"


# -- packaging regression ------------------------------------------------------
#
# The v0.2.0 sub-package split broke the wheel once already: an explicit,
# non-recursive `packages = ["nautilus_gateio"]` shipped only the top-level
# modules, so the installed package could not import at all while the source
# tree on PYTHONPATH kept every test green. These tests build the wheel from a
# clean copy of the repo and exercise the public API from the INSTALLED artefact.


def env_without_source_tree() -> dict[str, str]:
    """A copy of the environment with the repo removed from ``PYTHONPATH``.

    Leaving it in would let the tooling discover the in-tree ``.egg-info`` and
    treat the package as already installed, which would make the packaging
    tests pass without ever exercising the wheel.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a wheel from a pristine copy of the repo and return its path."""
    workspace = tmp_path_factory.mktemp("wheel-build")
    source = workspace / "src"
    source.mkdir()

    shutil.copytree(
        PACKAGE_DIR,
        source / PACKAGE_NAME,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, source / name)

    outdir = workspace / "dist"
    attempts = [
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(outdir)],
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
    ]
    failures: list[str] = []
    for command in attempts:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=source,
            capture_output=True,
            text=True,
            env=env_without_source_tree(),
            check=False,
        )
        if result.returncode == 0:
            break
        failures.append(f"{' '.join(command)}\n{result.stdout}\n{result.stderr}")
    else:
        pytest.fail("could not build the wheel:\n" + "\n---\n".join(failures))

    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    return wheels[0]


@pytest.fixture(scope="session")
def installed_wheel_env(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Install the wheel into a throwaway venv and return how to run it.

    The dependency (NautilusTrader and its own dependencies) is made available
    through ``PYTHONPATH``; the repo source tree deliberately is not, so the
    import under test can only resolve to the installed wheel.
    """
    import nautilus_trader

    venv_dir = tmp_path_factory.mktemp("wheel-venv") / "venv"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        env=env_without_source_tree(),
        check=True,
    )
    python = venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    install = subprocess.run(  # noqa: S603
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--force-reinstall",
            str(built_wheel),
        ],
        capture_output=True,
        text=True,
        env=env_without_source_tree(),
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    installed = list(venv_dir.rglob(f"site-packages/{PACKAGE_NAME}/__init__.py"))
    assert installed, f"the wheel did not install a {PACKAGE_NAME} package:\n{install.stdout}"

    dependency_path = Path(nautilus_trader.__file__).resolve().parent.parent
    return {
        "python": str(python),
        "venv_dir": str(venv_dir.resolve()),
        "env": {"PYTHONPATH": str(dependency_path)},
        "cwd": str(venv_dir.parent),
    }


def run_in_installed_env(installed_wheel_env: dict, script: str) -> str:
    """Run ``script`` against the installed wheel and return its stdout."""
    env = env_without_source_tree()
    env.update(installed_wheel_env["env"])
    result = subprocess.run(  # noqa: S603
        [installed_wheel_env["python"], "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=installed_wheel_env["cwd"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


class TestWheelContents:
    def test_the_wheel_contains_every_source_module(self, built_wheel):
        """Regression: an explicit package list shipped only the top level."""
        with zipfile.ZipFile(built_wheel) as archive:
            names = set(archive.namelist())
        expected = {
            f"{PACKAGE_NAME}/{path.relative_to(PACKAGE_DIR).as_posix()}" for path in source_files()
        }
        missing = sorted(expected - names)
        assert not missing, f"modules missing from the wheel: {missing}"

    @pytest.mark.parametrize("subpackage", SUBPACKAGES)
    def test_every_subpackage_is_in_the_wheel(self, built_wheel, subpackage):
        with zipfile.ZipFile(built_wheel) as archive:
            names = archive.namelist()
        prefix = f"{PACKAGE_NAME}/{subpackage}/"
        assert f"{prefix}__init__.py" in names
        assert sum(1 for name in names if name.startswith(prefix)) > 1

    def test_the_wheel_ships_the_typing_marker(self, built_wheel):
        with zipfile.ZipFile(built_wheel) as archive:
            assert f"{PACKAGE_NAME}/py.typed" in archive.namelist()

    def test_the_wheel_version_matches_the_package_version(self, built_wheel):
        assert nautilus_gateio.__version__ in built_wheel.name


class TestInstalledWheel:
    def test_the_import_resolves_to_the_installed_wheel_not_the_source_tree(
        self,
        installed_wheel_env,
    ):
        location = run_in_installed_env(
            installed_wheel_env,
            f"import {PACKAGE_NAME}; print({PACKAGE_NAME}.__file__)",
        ).strip()
        assert location.startswith(installed_wheel_env["venv_dir"]), location
        assert not location.startswith(str(REPO_ROOT)), location

    def test_every_module_imports_from_the_installed_wheel(self, installed_wheel_env):
        script = (
            "import importlib, json, pkgutil\n"
            f"import {PACKAGE_NAME} as pkg\n"
            "names = [n for _, n, _ in pkgutil.walk_packages("
            f"pkg.__path__, prefix='{PACKAGE_NAME}.')]\n"
            "for name in names:\n"
            "    importlib.import_module(name)\n"
            "print(json.dumps(sorted(names)))\n"
        )
        installed = json.loads(run_in_installed_env(installed_wheel_env, script))
        assert sorted(installed) == MODULE_NAMES[1:]

    def test_every_documented_public_import_works_from_the_installed_wheel(
        self,
        installed_wheel_env,
    ):
        names = ", ".join(sorted(nautilus_gateio.__all__))
        script = (
            f"from {PACKAGE_NAME} import {names}\n"
            f"import {PACKAGE_NAME} as pkg\n"
            "print(pkg.__version__)\n"
        )
        assert run_in_installed_env(installed_wheel_env, script).strip() == (
            nautilus_gateio.__version__
        )

    def test_the_documented_quick_start_imports_work_from_the_installed_wheel(
        self,
        installed_wheel_env,
    ):
        script = (
            f"from {PACKAGE_NAME} import (\n"
            "    GATEIO,\n"
            "    GateioDataClientConfig,\n"
            "    GateioExecClientConfig,\n"
            "    GateioLiveDataClientFactory,\n"
            "    GateioLiveExecClientFactory,\n"
            "    GateioProductType,\n"
            ")\n"
            f"from {PACKAGE_NAME}.common.symbols import gateio_to_instrument_id\n"
            f"from {PACKAGE_NAME}.http import GateioSpotHttpAPI\n"
            f"from {PACKAGE_NAME}.websocket import GateioPublicWebSocket\n"
            "print(str(gateio_to_instrument_id(GateioProductType.PERP, 'BTC_USDT')))\n"
        )
        assert run_in_installed_env(installed_wheel_env, script).strip() == "BTC_USDT-PERP.GATE_IO"
