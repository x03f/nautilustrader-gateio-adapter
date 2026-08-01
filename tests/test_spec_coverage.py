"""The specification-case registry must keep naming tests that exist.

``docs/spec-coverage.md`` is the record NautilusTrader's adapter guide asks for:
for each numbered specification case, either the test that closes it or the
reason it is skipped. A record like that decays in one direction — a test is
renamed or deleted, the registry keeps citing it, and the citation reads exactly
as it did when it was true. That is worse than no registry: an acceptance record
built on it claims coverage that no longer exists, and nothing in a green suite
says otherwise.

So this module is the sentinel, and every check here has an input that breaks it:

* rename ``test_a_plain_limit_cancel_emits_exactly_one_order_canceled`` in
  ``tests/test_execution_cancel.py`` and leave the registry alone — the row for
  TC-E40 now names nothing, and :func:`test_every_named_test_exists` fails
  naming the case and the missing node;
* mistype a class in an evidence cell (``TestCancelSuceeds``) — same failure,
  because the node id is compared whole rather than by its last segment;
* delete a row, or add one for a case number the specification does not
  contain — :func:`test_the_registry_covers_every_specification_case` fails,
  which is what keeps the denominator at 95 and keeps the three identifiers
  that never existed (TC-E07, TC-E08, TC-E09) out;
* leave a row with a rung but no evidence, or an open gap with nothing said
  about what is missing — :func:`test_every_row_states_evidence_or_a_reason`
  fails.

The empty selection is a failure here too: if the parser stops finding rows or
stops finding node ids, :func:`test_the_registry_was_parsed` fails rather than
letting every other check pass vacuously over nothing.

What this module cannot do is read a test and judge whether it asserts what the
case requires. That check is a reading, it was done row by row when the registry
was written, and the page says so. This module holds the mechanical half.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "docs" / "spec-coverage.md"
TESTS_DIR = REPO_ROOT / "tests"

#: The case identifiers of upstream `spec_exec_testing.md` and
#: `spec_data_testing.md` at commit `e3ef686` (branch `develop`, 2026-08-01),
#: transcribed from the group tables at the head of each section — the one place
#: that lists every case, including TC-E74 to TC-E78, which the specification
#: states collectively instead of one section each.
#:
#: This is the specification side of the ledger; the registry is the repository
#: side, and the two are compared so that a row cannot quietly disappear and a
#: case number the specification never issued cannot quietly appear. TC-E07,
#: TC-E08 and TC-E09 are absent because the specification has no such cases —
#: an earlier internal map carried them, which is how a denominator of 93 for
#: the previous revision (90 real cases) got into circulation.
SPEC_EXEC_CASES = (
    "TC-E01",
    "TC-E02",
    "TC-E03",
    "TC-E04",
    "TC-E05",
    "TC-E06",
    "TC-E10",
    "TC-E11",
    "TC-E12",
    "TC-E13",
    "TC-E14",
    "TC-E15",
    "TC-E16",
    "TC-E17",
    "TC-E18",
    "TC-E19",
    "TC-E20",
    "TC-E21",
    "TC-E22",
    "TC-E23",
    "TC-E24",
    "TC-E25",
    "TC-E26",
    "TC-E27",
    "TC-E30",
    "TC-E31",
    "TC-E32",
    "TC-E33",
    "TC-E34",
    "TC-E35",
    "TC-E36",
    "TC-E40",
    "TC-E41",
    "TC-E42",
    "TC-E43",
    "TC-E44",
    "TC-E50",
    "TC-E51",
    "TC-E52",
    "TC-E53",
    "TC-E60",
    "TC-E61",
    "TC-E62",
    "TC-E63",
    "TC-E70",
    "TC-E71",
    "TC-E72",
    "TC-E73",
    "TC-E74",
    "TC-E75",
    "TC-E76",
    "TC-E77",
    "TC-E78",
    "TC-E80",
    "TC-E81",
    "TC-E82",
    "TC-E83",
    "TC-E84",
    "TC-E85",
    "TC-E86",
    "TC-E87",
    "TC-E90",
    "TC-E91",
    "TC-E92",
    "TC-E94",
    "TC-E96",
    "TC-E99",
    "TC-E100",
    "TC-E101",
)
SPEC_DATA_CASES = (
    "TC-D01",
    "TC-D02",
    "TC-D03",
    "TC-D10",
    "TC-D11",
    "TC-D12",
    "TC-D13",
    "TC-D14",
    "TC-D15",
    "TC-D20",
    "TC-D21",
    "TC-D30",
    "TC-D31",
    "TC-D40",
    "TC-D41",
    "TC-D50",
    "TC-D51",
    "TC-D52",
    "TC-D53",
    "TC-D60",
    "TC-D61",
    "TC-D62",
    "TC-D63",
    "TC-D70",
    "TC-D71",
    "TC-D72",
)
SPEC_CASES = SPEC_EXEC_CASES + SPEC_DATA_CASES

#: The rungs of the evidence ladder in `docs/validation.md`, plus the one verdict
#: that is not a rung: a case the specification's own *Skip when* condition
#: removes. Nothing else may appear in the column — a registry that invents a
#: grade of its own is the second status vocabulary the documentation forbids.
LADDER_RUNGS = frozenset(
    {"implemented", "unit-tested", "offline-harness", "mainnet-confirmed", "skipped"},
)

#: A pytest node id for a test in this repository, as the registry writes them:
#: inside backticks, path first, no parameter suffix.
NODE_ID = re.compile(r"`(tests/[A-Za-z0-9_./]+\.py(?:::[A-Za-z0-9_]+)+)`")

#: The three identifiers no revision of the specification contains.
PHANTOM_CASES = ("TC-E07", "TC-E08", "TC-E09")


class Row(NamedTuple):
    """One registry row: a specification case and what the repository has for it."""

    case: str
    name: str
    rung: str
    evidence: str
    gap: str

    @property
    def node_ids(self) -> list[str]:
        return NODE_ID.findall(self.evidence)


def _collect_rows() -> list[Row]:
    """Read every case row out of the registry's markdown tables."""
    rows: list[Row] = []
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("| TC-"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        assert len(cells) == 5, f"a case row must carry five columns: {stripped}"
        rows.append(Row(*cells))
    return rows


def _collect_node_ids() -> set[str]:
    """Every test node id the suite defines, read from the sources themselves.

    Parsing rather than collecting keeps this check independent of the run it is
    part of: a node id is present because a test is defined, not because some
    plugin chose to collect it. Parametrized tests are recorded under their base
    id, which is the form the registry uses.
    """
    node_ids: set[str] = set()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module.body:
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(
                        member, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and member.name.startswith("test"):
                        node_ids.add(f"{relative}::{node.name}::{member.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test"
            ):
                node_ids.add(f"{relative}::{node.name}")
    return node_ids


ROWS = _collect_rows()
NODE_IDS = _collect_node_ids()


def test_the_registry_was_parsed() -> None:
    """An empty selection is not a pass.

    Every other test here iterates over ``ROWS``; a parser that silently found
    nothing — the table reformatted, the file renamed, the column count changed
    — would turn all of them green while checking nothing at all.
    """
    assert REGISTRY.is_file(), f"{REGISTRY} is missing"
    assert len(ROWS) == len(SPEC_CASES), f"parsed {len(ROWS)} rows"
    assert len(NODE_IDS) > 500, f"only {len(NODE_IDS)} test node ids were found in {TESTS_DIR}"
    cited = {node_id for row in ROWS for node_id in row.node_ids}
    assert len(cited) > 100, f"only {len(cited)} tests are cited by the registry"


def test_the_registry_covers_every_specification_case() -> None:
    """One row per case, no case missing, no case invented."""
    listed = [row.case for row in ROWS]
    duplicates = {case for case in listed if listed.count(case) > 1}
    assert not duplicates, f"a case is listed more than once: {sorted(duplicates)}"

    missing = sorted(set(SPEC_CASES) - set(listed))
    assert not missing, f"the registry does not record these specification cases: {missing}"

    unknown = sorted(set(listed) - set(SPEC_CASES))
    assert not unknown, (
        f"the registry records case identifiers the specification does not contain: {unknown}"
    )


def test_no_phantom_case_is_recorded() -> None:
    """TC-E07, TC-E08 and TC-E09 exist in no revision of the specification.

    They are stated separately from the set comparison above because their
    return is the specific regression: they came from an internal map, they made
    the denominator 93, and a reader who counts rows must not find them back.
    """
    listed = {row.case for row in ROWS}
    assert not listed & set(PHANTOM_CASES), (
        f"the registry lists cases that do not exist: {sorted(listed & set(PHANTOM_CASES))}"
    )
    assert len(ROWS) == 95, f"this revision numbers 95 cases; the registry has {len(ROWS)}"


@pytest.mark.parametrize("row", ROWS, ids=[row.case for row in ROWS])
def test_every_named_test_exists(row: Row) -> None:
    """The sentinel: a test the registry cites has to be in the suite.

    This is the check that fails when a test is renamed or deleted without the
    registry following it — the failure mode the page exists to prevent, because
    a stale citation reads exactly like a true one.
    """
    for node_id in row.node_ids:
        assert node_id in NODE_IDS, (
            f"{row.case} cites a test that does not exist: {node_id}. Either the test was "
            f"renamed or removed and docs/spec-coverage.md was not updated with it, or the "
            f"node id is mistyped"
        )


@pytest.mark.parametrize("row", ROWS, ids=[row.case for row in ROWS])
def test_every_row_uses_the_projects_own_vocabulary(row: Row) -> None:
    assert row.rung in LADDER_RUNGS, (
        f"{row.case} is graded {row.rung!r}, which is not a rung of the evidence ladder in "
        f"docs/validation.md nor the verdict 'skipped'"
    )
    assert row.name, f"{row.case} has no name"


@pytest.mark.parametrize("row", ROWS, ids=[row.case for row in ROWS])
def test_every_row_states_evidence_or_a_reason(row: Row) -> None:
    """No row may be silent about why it says what it says."""
    assert row.evidence and row.evidence != "—", f"{row.case} states no evidence at all"

    if row.rung in {"unit-tested", "offline-harness", "mainnet-confirmed"}:
        assert row.node_ids, (
            f"{row.case} claims {row.rung} but names no test. A rung above 'implemented' is a "
            f"claim about the suite and has to point at it"
        )
    if row.rung == "implemented":
        assert row.gap and row.gap != "—", (
            f"{row.case} is an open gap and must say what is not asserted"
        )
    if row.rung == "skipped":
        assert len(row.evidence) > 40, (
            f"{row.case} is skipped without stating the venue fact or capability limit that "
            f"makes the specification's own 'Skip when' apply: {row.evidence!r}"
        )


def test_the_summary_matches_the_rows() -> None:
    """The counts at the foot of the page are derived, so they must be derivable."""
    text = REGISTRY.read_text(encoding="utf-8")
    counted: dict[str, int] = {}
    for rung in LADDER_RUNGS:
        counted[rung] = sum(1 for row in ROWS if row.rung == rung)

    for rung, expected in counted.items():
        pattern = re.compile(rf"^\| {re.escape(rung)}[^|]*\|\s*(\d+)\s*\|", re.MULTILINE)
        match = pattern.search(text)
        assert match is not None, f"the summary table has no row for {rung!r}"
        assert int(match.group(1)) == expected, (
            f"the summary says {match.group(1)} {rung} cases; the tables hold {expected}"
        )

    total = re.search(r"^\| \*\*Total\*\* \| \*\*(\d+)\*\* \|", text, re.MULTILINE)
    assert total is not None, "the summary states no total"
    assert int(total.group(1)) == len(ROWS)


def test_the_registry_is_reachable_from_the_pages_that_owe_it() -> None:
    """A record nobody links to is a record nobody reads.

    The adapter guide asks for the supported and skipped cases as a field of the
    acceptance record, so the validation page has to reach this one, and the
    testing page is where a contributor looks for what the suite covers.
    """
    for page in ("testing.md", "validation.md"):
        text = (REPO_ROOT / "docs" / page).read_text(encoding="utf-8")
        assert "spec-coverage.md" in text, f"docs/{page} does not link to the case registry"
