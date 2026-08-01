"""Regression tests for the user-facing documentation, examples and CI.

These cover the failure mode where the code moves and the prose does not: a
document that promises a feature the package no longer has, or a default the
code no longer uses, is worse than no document at all. The checks here are
deliberately mechanical, so they fail on the next drift rather than on the next
review.

Covered:

* the configuration reference matches the real struct defaults, field by field
  (in particular that execution defaults to **mainnet**);
* no page advertises a feature removed in 0.2.0 (the order kill switch,
  synthetic quotes, the paper module, the standalone reconciliation helper, the
  ``GATEIO`` venue string) except where it explicitly documents the removal;
* every import shown in the documentation and the examples resolves;
* the feature matrix uses the platform's two glyphs and nothing else, and claims
  no mainnet run while none is recorded;
* CI verifies the built wheel from outside the source tree, importing every
  sub-package, and cleans stale artefacts before building;
* three behaviours a page could describe accurately in the tense the code has
  left — the refused `spot_account_mode=UNIFIED` pairing, the submission deadline
  the execution client now enables on connect, and the cash-borrowing
  registration that is gone — are each pinned by running the code and reading the
  prose against the result, in one test, so neither side can move alone.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import msgspec
import pytest
from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig

from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode
from nautilus_gateio.config import GateioDataClientConfig, GateioExecClientConfig
from nautilus_gateio.http.client import EXPIRY_HEADER

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
EXAMPLES = REPO / "examples"
README = REPO / "README.md"
CHANGELOG = REPO / "CHANGELOG.md"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

#: Pages that describe 0.1.0 on purpose, and so may name removed features.
#: Pages that describe what the project USED to do. They quote removed symbols
#: and superseded claims on purpose, so the drift guards below would fail them
#: for being accurate.
HISTORICAL_PAGES = {"migration-0.1-to-0.2.md", "review-matrix.md"}

#: The only two cell values a capability column may hold, which is
#: NautilusTrader's own convention for an integration matrix: a check mark for
#: supported, a hyphen for unsupported. The set is closed on purpose. "mostly
#: works" and "should be fine" are how a reader ends up trusting an untested path
#: with money, and a third glyph invites exactly that. What a check mark is
#: evidence *of* is a separate question, which the Mainnet column answers.
MATRIX_GLYPHS = frozenset({"✓", "-"})

#: Headers of the columns whose cells are capability glyphs.
PRODUCT_COLUMNS = ("Spot", "Perp", "Inverse", "Delivery", "Options")


def _table_cells(line: str) -> list[str]:
    """Split one markdown table row, honouring escaped pipes in type columns."""
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _doc_pages() -> list[Path]:
    """Every user-facing page whose claims must match the current code."""
    pages = [README, *sorted(DOCS.glob("*.md")), EXAMPLES / "README.md"]
    return [p for p in pages if p.name not in HISTORICAL_PAGES]


def _example_scripts() -> list[Path]:
    return sorted(EXAMPLES.glob("*.py"))


# --------------------------------------------------------------------------
# Configuration reference (DOC-01)
# --------------------------------------------------------------------------


def _documented_defaults(class_name: str) -> dict[str, str]:
    """Return ``{field: documented default}`` from the configuration reference."""
    text = (DOCS / "configuration.md").read_text(encoding="utf-8")
    heading = f"## `{class_name}`"
    assert heading in text, f"{heading} is not documented in docs/configuration.md"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]

    defaults: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = _table_cells(line)
        if len(cells) < 3:
            continue
        field = cells[0].strip("`")
        defaults[field] = cells[2]
    return defaults


def _actual_defaults(config_class: type) -> dict[str, object]:
    return {
        field.name: field.default
        for field in msgspec.structs.fields(config_class)
        if field.default is not msgspec.NODEFAULT
    }


def _parse_documented_default(cell: str) -> object:
    """Evaluate a documented default cell such as ``` `"mainnet"` ```."""
    literal = cell.strip().strip("`").strip()
    if literal.startswith("``") and literal.endswith("``"):
        literal = literal.strip("`").strip()
    return eval(  # noqa: S307 - fixed vocabulary from our own documentation
        literal,
        {"__builtins__": {}},
        {
            "GateioProductType": GateioProductType,
            "GateioSpotAccountMode": GateioSpotAccountMode,
            "None": None,
        },
    )


CONFIG_CLASSES = [
    ("GateioDataClientConfig", GateioDataClientConfig, LiveDataClientConfig),
    ("GateioExecClientConfig", GateioExecClientConfig, LiveExecClientConfig),
]


def _own_fields(config_class: type, base_class: type) -> set[str]:
    """Field names the adapter adds; the inherited ones are documented upstream."""
    inherited = {field.name for field in msgspec.structs.fields(base_class)}
    return {field.name for field in msgspec.structs.fields(config_class)} - inherited


class TestConfigurationReference:
    """docs/configuration.md must describe the defaults the code actually has."""

    def test_execution_environment_default_is_documented_as_mainnet(self) -> None:
        # The specific inversion this test exists for: 0.1.0 defaulted execution
        # to the testnet, the docs said so, and 0.2.0 changed the code only.
        assert GateioExecClientConfig().environment == "mainnet"
        documented = _documented_defaults("GateioExecClientConfig")["environment"]
        assert _parse_documented_default(documented) == "mainnet"

    def test_no_page_claims_execution_defaults_to_testnet(self) -> None:
        pattern = re.compile(
            r"(execution|exec client|`?GateioExecClientConfig`?)[^.\n]{0,80}"
            r"default[^.\n]{0,40}testnet",
            re.IGNORECASE,
        )
        offenders = [
            f"{page.relative_to(REPO)}: {line.strip()}"
            for page in _doc_pages()
            for line in page.read_text(encoding="utf-8").splitlines()
            if pattern.search(line)
        ]
        assert not offenders, "documentation claims execution defaults to testnet: " + "; ".join(
            offenders,
        )

    @pytest.mark.parametrize(("class_name", "config_class", "base_class"), CONFIG_CLASSES)
    def test_every_field_is_documented(
        self,
        class_name: str,
        config_class: type,
        base_class: type,
    ) -> None:
        documented = _documented_defaults(class_name)
        actual = _actual_defaults(config_class)
        # Fields inherited from the NautilusTrader base configs are documented
        # upstream; only the adapter's own fields must appear here.
        missing = sorted(_own_fields(config_class, base_class) - set(documented))
        assert not missing, f"{class_name} fields missing from docs/configuration.md: {missing}"

        unknown = sorted(set(documented) - set(actual))
        assert not unknown, (
            f"docs/configuration.md documents unknown {class_name} fields: {unknown}"
        )

    @pytest.mark.parametrize(("class_name", "config_class", "base_class"), CONFIG_CLASSES)
    def test_documented_defaults_match_the_code(
        self,
        class_name: str,
        config_class: type,
        base_class: type,  # noqa: ARG002 - shared parametrisation
    ) -> None:
        documented = _documented_defaults(class_name)
        actual = _actual_defaults(config_class)
        mismatches = []
        for field, cell in documented.items():
            expected = actual[field]
            try:
                parsed = _parse_documented_default(cell)
            except Exception as exc:  # noqa: BLE001 - reported as a mismatch
                mismatches.append(f"{field}: cannot parse documented default {cell!r} ({exc})")
                continue
            if parsed != expected:
                mismatches.append(f"{field}: documented {parsed!r}, actual {expected!r}")
        assert not mismatches, f"{class_name} documentation drift: " + "; ".join(mismatches)


# --------------------------------------------------------------------------
# Removed-feature vocabulary (DOC-02, DP-4)
# --------------------------------------------------------------------------

#: Things 0.2.0 does not have. A page may name one only while stating that it is
#: gone, which the removal markers below detect.
REMOVED_VOCABULARY = {
    "live_orders": "the order kill switch",
    "LiveOrdersDisabledError": "the kill switch error",
    "emit_synthetic_quotes": "synthetic quotes",
    "synthetic quote": "synthetic quotes",
    "PaperExecution": "the paper module",
    "GateioPaperConfig": "the paper config",
    "paper.py": "the paper module",
    "reconcile.py": "the standalone reconciliation helper",
    "StaticInstrumentProvider": "the static provider",
    "place_order_validated": "the removed validating order helper",
    "instrument_id_to_gate_pair": "the old symbology function",
    "gate_pair_to_instrument_id": "the old symbology function",
    "GATEIO_WS_MAINNET": "the old WebSocket constant",
    "emergency_stop": "the removed emergency stop",
    "use_websocket": "a removed config field",
    "poll_interval_secs": "a removed config field",
}

#: A line naming a removed feature is acceptable when it says it was removed.
REMOVAL_MARKER = re.compile(
    r"\b(removed|is gone|are gone|no longer|0\.1\.0|was\b|used to)\b",
    re.IGNORECASE,
)

#: The 0.1.0 venue string in instrument-id position (``BTC_USDT.GATEIO``).
STALE_VENUE = re.compile(r"\.GATEIO\b")


class TestNoRemovedVocabulary:
    """No page may advertise a feature 0.2.0 does not have."""

    @pytest.mark.parametrize("page", _doc_pages(), ids=lambda p: str(p.name))
    def test_page_does_not_advertise_removed_features(self, page: Path) -> None:
        offenders = []
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
            if REMOVAL_MARKER.search(line):
                continue  # documents the removal rather than advertising it
            for token, description in REMOVED_VOCABULARY.items():
                if token in line:
                    offenders.append(
                        f"line {number} names {description} ({token!r}): {line.strip()}"
                    )
        assert not offenders, f"{page.relative_to(REPO)} advertises removed features: " + "; ".join(
            offenders,
        )

    @pytest.mark.parametrize("page", _doc_pages(), ids=lambda p: str(p.name))
    def test_page_uses_the_current_venue_string(self, page: Path) -> None:
        offenders = [
            f"line {number}: {line.strip()}"
            for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1)
            if STALE_VENUE.search(line) and not REMOVAL_MARKER.search(line)
        ]
        assert not offenders, (
            f"{page.relative_to(REPO)} still uses the 0.1.0 venue string `.GATEIO`: "
            + "; ".join(offenders)
        )

    @pytest.mark.parametrize("script", _example_scripts(), ids=lambda p: str(p.name))
    def test_example_does_not_use_removed_api(self, script: Path) -> None:
        text = script.read_text(encoding="utf-8")
        offenders = [token for token in REMOVED_VOCABULARY if token in text]
        offenders += [m.group(0) for m in STALE_VENUE.finditer(text)]
        assert not offenders, f"{script.name} uses removed API: {offenders}"

    def test_the_kill_switch_removal_is_stated_explicitly(self) -> None:
        # DP-4: the docs must say plainly that there is no local order block,
        # not merely stop mentioning the old flag.
        readme = README.read_text(encoding="utf-8")
        configuration = (DOCS / "configuration.md").read_text(encoding="utf-8")
        assert "no local order kill switch" in configuration
        assert "no local order kill switch" in readme
        assert 'defaults to `"mainnet"`' in readme


class TestMigrationGuide:
    """The migration page must document every breaking change of 0.2.0."""

    @pytest.mark.parametrize(
        "topic",
        [
            "GATE_IO",
            "-PERP",
            "mainnet",
            "live_orders",
            "emit_synthetic_quotes",
            "PaperExecution",
            "reconcile",
            "sub-package",
        ],
    )
    def test_breaking_change_is_documented(self, topic: str) -> None:
        text = (DOCS / "migration-0.1-to-0.2.md").read_text(encoding="utf-8")
        assert topic in text, f"the migration guide does not mention {topic!r}"

    @pytest.mark.parametrize(
        "topic",
        ["GATE_IO", "-PERP", "mainnet", "live_orders", "synthetic quotes", "sub-packaged"],
    )
    def test_changelog_lists_breaking_change(self, topic: str) -> None:
        text = CHANGELOG.read_text(encoding="utf-8")
        # Match the 0.2.0 line by prefix so a pre-release suffix such as
        # `[0.2.0a1]` still resolves. Pinning the exact string made this test
        # fail on the version bump rather than on anything about the content.
        section = re.split(r"^## \[0\.2\.0[^\]]*\]", text, maxsplit=1, flags=re.MULTILINE)
        assert len(section) == 2, "CHANGELOG.md has no 0.2.0 section"
        body = section[1].split("## [0.1.0]", 1)[0]
        assert topic in body, f"the 0.2.0 changelog entry does not mention {topic!r}"


# --------------------------------------------------------------------------
# Documented imports resolve (DOC-02)
# --------------------------------------------------------------------------

IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+(nautilus_gateio[\w.]*)\s+import\s+(.+)|import\s+(nautilus_gateio[\w.]*))",
)


def _documented_imports() -> list[tuple[str, str, str]]:
    """Return ``(source, module, name)`` for every documented adapter import."""
    found: list[tuple[str, str, str]] = []
    sources = [*_doc_pages(), DOCS / "migration-0.1-to-0.2.md", *_example_scripts()]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".md":
            blocks = re.findall(r"```python\n(.*?)```", text, re.S)
        else:
            blocks = [text]
        for block in blocks:
            # Join implicit continuations of parenthesised import lists.
            flat = re.sub(r"\(\s*\n([^)]*)\)", lambda m: m.group(1).replace("\n", " "), block)
            for line in flat.splitlines():
                match = IMPORT_LINE.match(line)
                if not match:
                    continue
                module, names, plain = match.groups()
                if plain:
                    found.append((path.name, plain, ""))
                    continue
                for name in names.split(","):
                    cleaned = name.strip().strip("()").split(" as ")[0].strip()
                    if cleaned and cleaned.isidentifier():
                        found.append((path.name, module, cleaned))
    return found


class TestDocumentedImports:
    """Every import shown to a user must exist."""

    def test_documented_imports_are_found(self) -> None:
        assert len(_documented_imports()) > 30, "no documented imports were parsed"

    @pytest.mark.parametrize(
        ("source", "module", "name"),
        _documented_imports(),
        ids=lambda value: str(value),
    )
    def test_documented_import_resolves(self, source: str, module: str, name: str) -> None:
        if source == "migration-0.1-to-0.2.md" and name in REMOVED_VOCABULARY:
            pytest.skip("the migration guide names removed symbols on purpose")
        imported = importlib.import_module(module)
        if name:
            assert hasattr(imported, name), f"{source}: {module} has no attribute {name!r}"


# --------------------------------------------------------------------------
# Feature matrix honesty
# --------------------------------------------------------------------------


def _matrix_cells(headers: tuple[str, ...]) -> list[str]:
    """Every README feature-matrix cell under one of ``headers``."""
    text = README.read_text(encoding="utf-8")
    section = text.split("## Feature support matrix", 1)[1].split("\n## ", 1)[0]
    found: list[str] = []
    columns: list[int] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            columns = []
            continue
        cells = _table_cells(line)
        # Every matrix table opens with a Feature column, and a data row may
        # legitimately hold a column name of its own ("Spot" in the Mainnet
        # cell), so the header is recognized by that first cell rather than by
        # the presence of a name anywhere in the row.
        if cells and cells[0] == "Feature":
            columns = [index for index, cell in enumerate(cells) if cell in headers]
            continue
        if not columns or set("".join(cells)) <= {"-", ":"}:
            continue
        found.extend(cells[index] for index in columns if index < len(cells))
    return found


class TestFeatureMatrix:
    """The matrix must use the two glyphs, and claim no run it does not have."""

    def test_matrix_rows_were_parsed(self) -> None:
        assert len(_matrix_cells(PRODUCT_COLUMNS)) > 20

    def test_every_capability_cell_is_a_glyph(self) -> None:
        unknown = sorted(set(_matrix_cells(PRODUCT_COLUMNS)) - MATRIX_GLYPHS)
        assert not unknown, f"feature matrix uses cells outside the two glyphs: {unknown}"

    def test_nothing_claims_a_run_that_is_not_recorded(self) -> None:
        """A named product in the Mainnet column has to be backed by the record.

        The column names the products a real run covered, so while the record is
        empty every cell in it must be the hyphen. This is the successor to a
        guard on the word ``Stable``, which the matrix no longer uses: the claim
        a reader acts on now is the Mainnet cell.
        """
        validation = (DOCS / "validation.md").read_text(encoding="utf-8")
        results = validation.split("### Mainnet validation results", 1)[1].split("\n###", 1)[0]
        if "nothing recorded yet" not in results:
            return
        claimed = sorted({cell for cell in _matrix_cells(("Mainnet",)) if cell != "-"})
        assert not claimed, (
            "the feature matrix names mainnet runs while docs/validation.md records "
            f"none: {claimed}"
        )

    def test_the_validation_page_records_runs(self) -> None:
        """The results section must carry rows, and the README must point at it.

        This replaces a scaffolding guard that asserted an authoring comment was
        still present in the page. That comment told whoever filled the section
        in what to write; once the runs were recorded it was a stale instruction
        sitting in a published document, and the guard was keeping it there. What
        matters now is the opposite property — that the section is not empty.
        """
        validation = (DOCS / "validation.md").read_text(encoding="utf-8")
        results = validation.split("### Mainnet validation results", 1)[1].split("\n###", 1)[0]
        rows = [
            line
            for line in results.splitlines()
            if line.startswith("|") and not set(line) <= set("|- ")
        ]
        # A header row and a separator alone are not results.
        assert len(rows) > 1, "docs/validation.md records no mainnet run"
        assert "](docs/validation.md" in README.read_text(encoding="utf-8"), (
            "the README does not link the page that carries the record"
        )

    def test_the_validation_page_states_no_venue_identifier(self) -> None:
        """No account id, order id, trade id or balance, on the page that is most
        tempted to carry one.

        Every row here is written from a real run against a real account, which
        is exactly where such a value would leak in. The rule is stated in the
        page's own reporting section; this holds it to it.
        """
        validation = (DOCS / "validation.md").read_text(encoding="utf-8")
        # Instrument ids and dates are fine; a bare run of seven or more digits
        # is not something this page has any reason to state.
        suspects = re.findall(r"(?<![\w.-])\d{7,}(?![\w.-])", validation)
        assert not suspects, f"docs/validation.md may carry a venue identifier: {suspects}"


class TestDocumentationLinks:
    """Relative links between pages must resolve."""

    @pytest.mark.parametrize(
        "page",
        [README, CHANGELOG, *sorted(DOCS.glob("*.md")), EXAMPLES / "README.md"],
        ids=lambda p: str(p.name),
    )
    def test_relative_links_resolve(self, page: Path) -> None:
        text = page.read_text(encoding="utf-8")
        broken = []
        for target in re.findall(r"\]\(([^)#][^)]*)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = (page.parent / target.split("#")[0]).resolve()
            if not path.exists():
                broken.append(target)
        assert not broken, f"{page.relative_to(REPO)} has broken links: {broken}"


# --------------------------------------------------------------------------
# Examples (DOC-02)
# --------------------------------------------------------------------------


class TestExamples:
    """Examples must import cleanly against the current package."""

    @pytest.mark.parametrize("script", _example_scripts(), ids=lambda p: str(p.name))
    def test_example_imports_and_defines_main(self, script: Path) -> None:
        spec = importlib.util.spec_from_file_location(f"example_{script.stem}", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "main"), f"{script.name} defines no main()"

    def test_every_example_is_listed_in_the_examples_readme(self) -> None:
        listing = (EXAMPLES / "README.md").read_text(encoding="utf-8")
        missing = [s.name for s in _example_scripts() if s.name not in listing]
        assert not missing, f"examples/README.md does not list: {missing}"


# --------------------------------------------------------------------------
# CI wheel verification (CI-01) and release hygiene (PKG-03)
# --------------------------------------------------------------------------


def _workflow_steps(job: str) -> list[tuple[str, str]]:
    """Return ``(step name, run block)`` for one job, without a YAML parser."""
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = re.split(r"^  (\w[\w-]*):$", text, flags=re.M)
    blocks = dict(zip(jobs[1::2], jobs[2::2], strict=True))
    assert job in blocks, f"the workflow has no {job!r} job"
    steps: list[tuple[str, str]] = []
    for chunk in re.split(r"^      - ", blocks[job], flags=re.M)[1:]:
        name_match = re.match(r"name: (.+)", chunk)
        name = name_match.group(1).strip() if name_match else ""
        run_match = re.search(r"^        run: (.*)$", chunk, flags=re.M | re.S)
        steps.append((name, run_match.group(1) if run_match else ""))
    return steps


def _heredoc(block: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\n\s*PY\b", block, re.S)
    assert match, "the step embeds no <<'PY' ... PY script"
    return textwrap.dedent(match.group(1))


#: The build step whose job is to prove the distribution is importable.
WHEEL_STEP = "verify the installed wheel"


def _wheel_verification_block() -> str:
    for name, run in _workflow_steps("build"):
        if name.lower().startswith(WHEEL_STEP):
            return run
    names = [name for name, _ in _workflow_steps("build")]
    raise AssertionError(f"the build job has no {WHEEL_STEP!r} step: {names}")


def _sub_packages() -> list[str]:
    """Every importable sub-package of the distribution, from the source tree."""
    root = REPO / "nautilus_gateio"
    return sorted(
        f"nautilus_gateio.{path.parent.name}"
        for path in root.glob("*/__init__.py")
        if path.parent.name != "__pycache__"
    )


class TestCiWheelVerification:
    """CI must be able to fail on a distribution that is missing sub-packages."""

    def test_build_job_verifies_the_installed_wheel(self) -> None:
        assert _wheel_verification_block(), "the wheel verification step runs nothing"

    def test_verification_runs_outside_the_source_tree(self) -> None:
        # With the checkout on sys.path, importing the package proves nothing
        # about what the wheel actually contains.
        block = _wheel_verification_block()
        assert "python -m venv" in block, "the wheel is not installed into a clean environment"
        assert re.search(r"^\s*cd /tmp", block, flags=re.M), (
            "the wheel verification does not leave the source tree before importing"
        )

    def test_verification_imports_every_sub_package(self) -> None:
        script = _heredoc(_wheel_verification_block())
        missing = [package for package in _sub_packages() if package not in script]
        assert not missing, f"the CI wheel check does not import: {missing}"

    def test_verification_script_passes_against_the_current_package(self) -> None:
        # Execute the exact snippet CI runs, from outside the repository, so a
        # check that cannot pass is caught here rather than on a release branch.
        block = _wheel_verification_block()
        env = dict(os.environ, PYTHONPATH=str(REPO))
        result = subprocess.run(  # noqa: S603 - fixed argv, our own script
            [sys.executable, "-c", _heredoc(block)],
            cwd=REPO.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"the CI wheel verification script fails: {result.stdout}\n{result.stderr}"
        )

    def test_import_smoke_test_exercises_sub_packages(self) -> None:
        steps = _workflow_steps("test")
        smoke = next(run for name, run in steps if "import smoke" in name.lower())
        assert "nautilus_gateio.common" in smoke
        assert "nautilus_gateio.http" in smoke
        assert "nautilus_gateio.websocket" in smoke


class TestReleaseArtefactHygiene:
    """Stale artefacts must not survive into a release upload."""

    def test_build_job_cleans_before_building(self) -> None:
        steps = _workflow_steps("build")
        names = [name for name, _ in steps]
        clean_index = next(
            (i for i, (_, run) in enumerate(steps) if re.search(r"rm -rf .*dist/", run)),
            None,
        )
        build_index = next(
            (i for i, (_, run) in enumerate(steps) if "python -m build" in run),
            None,
        )
        assert clean_index is not None, f"the build job never removes dist/: {names}"
        assert build_index is not None, f"the build job never builds: {names}"
        assert clean_index < build_index, "dist/ is cleaned after the build instead of before it"

        clean_block = steps[clean_index][1]
        for artefact in ("dist/", "build/", "egg-info"):
            assert artefact in clean_block, f"the clean step leaves {artefact} behind"

    def test_build_job_asserts_dist_holds_only_this_version(self) -> None:
        steps = _workflow_steps("build")
        assert any("dist" in name.lower() and "version" in name.lower() for name, _ in steps), (
            "the build job does not assert that dist/ holds exactly the current version"
        )

    def test_release_guide_documents_the_clean_and_a_pinned_upload(self) -> None:
        text = (DOCS / "releasing.md").read_text(encoding="utf-8")
        assert re.search(r"rm -rf dist/ build/ \*\.egg-info", text), (
            "docs/releasing.md does not tell the releaser to clean dist/ first"
        )
        assert re.search(r"twine upload dist/<[a-z_]+>-<version>\*", text), (
            "docs/releasing.md does not show an upload pinned to one version's files"
        )
        assert "twine upload dist/*" not in text, (
            "docs/releasing.md still recommends a bare upload glob"
        )

    def test_no_document_tells_the_releaser_to_claim_the_current_pypi_name(self) -> None:
        """A PyPI name is never released once claimed.

        The current distribution name uses the NautilusTrader trademark and is
        to be replaced by `gateio-nt-community`. Uploading under the present
        name would be irreversible, so no page may spell out the command that
        does it.
        """
        offenders = [
            str(page.relative_to(REPO))
            for page in _doc_pages()
            if re.search(
                r"twine upload\s+\S*nautilustrader[-_]gateio[-_]adapter",
                page.read_text(encoding="utf-8"),
            )
        ]
        assert not offenders, (
            f"an upload of the trademark-infringing name is spelled out in: {offenders}"
        )

    def test_no_document_recommends_a_bare_upload_glob(self) -> None:
        offenders = [
            str(page.relative_to(REPO))
            for page in _doc_pages()
            if "twine upload dist/*" in page.read_text(encoding="utf-8")
        ]
        assert not offenders, f"a bare `twine upload dist/*` is recommended in: {offenders}"


# --------------------------------------------------------------------------
# Documented behaviour is the landed behaviour (DOC-03)
# --------------------------------------------------------------------------
#
# The guards above catch a page that names something the package no longer
# exports. They cannot catch the harder drift, which is a page that describes a
# behaviour accurately in the tense the code has left: the symbol still exists,
# the sentence still parses, and it is a lie. Three behaviours changed under this
# heading, and every page that described them kept the old description.
#
# Each test below establishes the fact by *running the code*, then reads the
# shipped prose against it. Neither half is worth anything alone — a prose guard
# alone pins the wording of a claim nobody has checked, and a behaviour test
# alone is what let the documentation drift in the first place — so the two are
# deliberately in one test: the page and the package can only pass together.


def _sentences(page: Path) -> list[tuple[int, str]]:
    """The page split into sentences, each with the line it starts on.

    The sentence is the unit a claim is made in, and it has to be: a markdown
    bullet list carries no blank lines, so a whole "before your first order"
    section is one paragraph, and a guard reading paragraphs is satisfied by the
    word "refuses" appearing four bullets away about something else. Splitting on
    a period followed by whitespace leaves `AccountFactory.register_cash_borrowing`
    and `0.1.0` intact, which is the only precision that matters here.
    """
    text = page.read_text(encoding="utf-8")
    sentences: list[tuple[int, str]] = []
    offset = 0
    for piece in re.split(r"(?<=\.)\s", text):
        sentences.append((text[:offset].count("\n") + 1, piece))
        offset += len(piece) + 1
    return sentences


#: Pages that own the account-mode rules. A reader configuring a node reaches the
#: combination through one of these four and nowhere else.
LEDGER_PAGES = (README, DOCS / "configuration.md", DOCS / "execution.md", DOCS / "products.md")

#: Pages that own the transport's clock and the submission deadline.
DEADLINE_PAGES = (DOCS / "configuration.md", DOCS / "architecture.md")


def _phrases(*phrases: str) -> re.Pattern[str]:
    """One case-insensitive alternation over phrases, tolerant of line wrapping.

    Every one of these patterns is read against prose that a formatter has already
    hard-wrapped at 80 columns, so a phrase is as likely to straddle a newline as
    not. A pattern that assumes single spaces silently stops guarding whichever
    page the wrap fell badly on — which is how the architecture page kept its
    false sentence while the configuration page lost its own.
    """
    joined = "|".join(phrase.replace(" ", r"\s+") for phrase in phrases)
    return re.compile(f"({joined})", re.IGNORECASE)


#: Prose that says a setting selects nothing and costs nothing.
INERT_CLAIM = _phrases(
    r"\binert\b",
    r"has no effect",
    r"only takes effect",
    r"not take effect",
    r"no spot order is ever sent",
    r"only visible effect",
)

#: Prose that says the client will not build.
REFUSAL_CLAIM = _phrases(
    r"\brefus\w+",
    r"\breject\w+",
    r"raises? `?ValueError`?",
    r"will not construct",
)

#: Falsified verbatim: the execution client does cross-check the two fields, and
#: the validation table's Execution row no longer reads "product set only".
#: Read line by line, because the table row is one line and the sentence is not.
CROSS_CHECK_DENIALS = _phrases(
    r"does not cross-?check",
    r"product set only",
    r"inert rather than wrong",
)

#: A sentence denying that any client in the package reads the venue clock. The
#: subject is what makes it false, so the pattern requires both halves in one
#: sentence: a page may still say the *data* client never calls it.
NO_CLIENT_SYNCS = re.compile(
    r"(neither\s+client|no\s+client|nothing\s+in\s+th(?:e|is)\s+package)"
    r"[^.]*?(sync_time|clock\s+offset|exptime|deadline|that\s+call)",
    re.IGNORECASE,
)

#: Prose tying the clock reading that enables the deadline to the execution
#: client's connect. All three of subject, moment and topic have to be in one
#: sentence: "the execution client opens one private connection per product" is
#: about neither the clock nor the deadline, and a page saying only that would
#: otherwise pass for having explained where the deadline comes from.
CLOCK_TOPIC = r"(clock|sync_time|offset|deadline|exptime)"
CLOCK_READ_ON_CONNECT = re.compile(
    rf"execution\s+client[^.]*?(connect|_connect)[^.]*?{CLOCK_TOPIC}"
    rf"|execution\s+client[^.]*?{CLOCK_TOPIC}[^.]*?(connect|_connect)",
    re.IGNORECASE,
)

#: Words that deny the cash-borrowing registration happens.
BORROWING_DENIAL = _phrases(
    r"nothing registers",
    r"never registers",
    r"no longer registers",
    r"is not registered",
    r"does not register",
    r"removed",
    r"is gone",
)


@pytest.fixture
def doc_node_components():
    """Clock, message bus, cache and loop, as a ``TradingNode`` would provide."""
    import asyncio

    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.model.identifiers import TraderId

    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache()
    loop = asyncio.new_event_loop()
    yield clock, msgbus, cache, loop
    loop.close()


def _build_exec_client(components, config: GateioExecClientConfig):
    from nautilus_gateio.factories import GateioLiveExecClientFactory

    clock, msgbus, cache, loop = components
    return GateioLiveExecClientFactory.create(
        loop=loop,
        name="GATE_IO",
        config=config,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )


class TestDocumentedBehaviourIsTheLandedBehaviour:
    """Prose that the code contradicts, pinned against the code that contradicts it."""

    def test_the_refused_unified_configuration_is_not_described_as_harmless(
        self,
        doc_node_components,
        block_network,
    ) -> None:
        # The fact, taken from the constructor rather than from a page.
        with pytest.raises(ValueError, match="UNIFIED"):
            _build_exec_client(
                doc_node_components,
                GateioExecClientConfig(
                    products=(GateioProductType.PERP,),
                    spot_account_mode=GateioSpotAccountMode.UNIFIED,
                ),
            )

        offenders = []
        for page in _doc_pages():
            for start, sentence in _sentences(page):
                if "UNIFIED" not in sentence:
                    continue
                if INERT_CLAIM.search(sentence) and not REFUSAL_CLAIM.search(sentence):
                    offenders.append(
                        f"{page.relative_to(REPO)}:{start} calls the mode inert without "
                        f"saying it is refused without SPOT: {sentence.strip()[:120]!r}"
                    )
        assert not offenders, "; ".join(offenders)

        silent = [
            str(page.relative_to(REPO))
            for page in LEDGER_PAGES
            if not any(
                "UNIFIED" in sentence and "SPOT" in sentence and REFUSAL_CLAIM.search(sentence)
                for _, sentence in _sentences(page)
            )
        ]
        assert not silent, (
            "these pages document `spot_account_mode` but never state in one sentence that "
            f"`UNIFIED` without `SPOT` is refused: {silent}"
        )

    def test_no_page_denies_the_cross_check_the_constructor_performs(
        self,
        doc_node_components,
        block_network,
    ) -> None:
        # It is a *cross*-check on two fields, so pin all three corners. A refusal
        # keyed to the product set alone refuses the first of these; one keyed to
        # the mode alone refuses the second, which is the configuration every page
        # here tells an operator to use.
        _build_exec_client(
            doc_node_components,
            GateioExecClientConfig(
                products=(GateioProductType.OPT,),
                spot_account_mode=GateioSpotAccountMode.SPOT,
            ),
        )
        _build_exec_client(
            doc_node_components,
            GateioExecClientConfig(
                products=(GateioProductType.SPOT,),
                spot_account_mode=GateioSpotAccountMode.UNIFIED,
            ),
        )
        with pytest.raises(ValueError):
            _build_exec_client(
                doc_node_components,
                GateioExecClientConfig(
                    products=(GateioProductType.OPT,),
                    spot_account_mode=GateioSpotAccountMode.UNIFIED,
                ),
            )

        offenders = []
        for page in _doc_pages():
            text = page.read_text(encoding="utf-8")
            for match in CROSS_CHECK_DENIALS.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{page.relative_to(REPO)}:{line}: {match.group(0).strip()!r}")
        assert not offenders, (
            "the execution client cross-checks `spot_account_mode` against `products`, "
            f"but these passages deny it: {offenders}"
        )

    def test_the_submission_deadline_is_documented_as_sent(
        self,
        doc_node_components,
        block_network,
    ) -> None:
        # Connect a default execution client with every venue call stubbed out
        # *except* the clock reading, which runs for real against a stubbed
        # transport, and then ask the transport for the headers it would put on an
        # order. Stubbing `sync_time` itself would only pin that `_connect` calls
        # something by that name: the measurement, the flag it sets and the header
        # it unlocks are three separate steps, and the deadline is only sent if
        # all three happen.
        _, _, _, loop = doc_node_components
        client = _build_exec_client(
            doc_node_components,
            GateioExecClientConfig(account_polling_interval_secs=0.0),
        )

        async def _noop(*args, **kwargs):
            return None

        client._instrument_provider.initialize = _noop
        client._load_user_id = _noop
        client._assert_one_way_position_mode = _noop
        client._update_account_state = _noop
        client._await_account_registered = _noop
        client._connect_private_websocket = _noop

        transport = client._http_client
        asked: list[tuple[str, str]] = []

        async def fake_request(method: str, path: str, *args, **kwargs):
            asked.append((method, path))
            return {"server_time": int(time.time() * 1000)}

        transport.request = fake_request
        loop.run_until_complete(client._connect())

        assert ("GET", "/spot/time") in asked, (
            f"connecting read no venue clock, so no order can carry a deadline: {asked}"
        )
        headers = transport._build_headers(
            "POST",
            "/api/v4/spot/orders",
            "",
            "",
            signed=False,
            expiring=True,
        )
        assert EXPIRY_HEADER in (headers or {}), (
            "a default execution client puts no submission deadline on a spot order after "
            "connecting; the pages below would then be right and this test wrong"
        )

        offenders = []
        for page in _doc_pages():
            text = page.read_text(encoding="utf-8")
            for match in NO_CLIENT_SYNCS.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{page.relative_to(REPO)}:{line}: {match.group(0).strip()!r}")
        assert not offenders, (
            "the execution client reads the venue clock on connect, so the deadline is sent "
            f"in the default configuration, but these passages deny it: {offenders}"
        )

        silent = [
            str(page.relative_to(REPO))
            for page in DEADLINE_PAGES
            if not CLOCK_READ_ON_CONNECT.search(page.read_text(encoding="utf-8"))
        ]
        assert not silent, (
            "these pages document the `x-gate-exptime` deadline but never say the execution "
            f"client measures the clock offset on connect, which is what enables it: {silent}"
        )

    def test_no_page_claims_cash_borrowing_is_registered(
        self,
        doc_node_components,
        block_network,
    ) -> None:
        from nautilus_trader.accounting.accounts.cash import CashAccount
        from nautilus_trader.accounting.error import AccountBalanceNegative
        from nautilus_trader.accounting.factory import AccountFactory
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.model.currencies import USDT
        from nautilus_trader.model.enums import AccountType
        from nautilus_trader.model.events import AccountState
        from nautilus_trader.model.identifiers import AccountId
        from nautilus_trader.model.objects import AccountBalance, Money

        from nautilus_gateio.common.constants import GATEIO

        try:
            _build_exec_client(
                doc_node_components,
                GateioExecClientConfig(
                    products=(GateioProductType.SPOT,),
                    spot_account_mode=GateioSpotAccountMode.CROSS_MARGIN,
                ),
            )
            funded = Money(1_000, USDT)
            account = AccountFactory.create(
                AccountState(
                    account_id=AccountId(f"{GATEIO}-master"),
                    account_type=AccountType.CASH,
                    base_currency=None,
                    reported=True,
                    balances=[AccountBalance(funded, Money(0, USDT), funded)],
                    margins=[],
                    info={},
                    event_id=UUID4(),
                    ts_event=0,
                    ts_init=0,
                )
            )
            assert isinstance(account, CashAccount)
            overdrawn = Money(-1, USDT)
            with pytest.raises(AccountBalanceNegative):
                account.update_balances([AccountBalance(overdrawn, Money(0, USDT), overdrawn)])
        finally:
            AccountFactory.deregister_cash_borrowing(GATEIO)

        offenders = []
        for page in _doc_pages():
            for start, sentence in _sentences(page):
                claim = re.search(
                    r"register(s|ed)?\s+cash\s+borrowing|register_cash_borrowing",
                    sentence,
                )
                if claim and not BORROWING_DENIAL.search(sentence):
                    offenders.append(
                        f"{page.relative_to(REPO)}:{start}: {sentence.strip()[:120]!r}"
                    )
        assert not offenders, (
            "a margin-mode client registers no cash borrowing — a Gate.io cash account still "
            f"refuses a negative balance — but these passages say it does: {offenders}"
        )
