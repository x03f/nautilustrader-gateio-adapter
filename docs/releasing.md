# Releasing

Checklist for cutting a release of `nautilustrader-gateio-adapter`.

**The branch version is never equal to a released version.** Between releases the
default branch carries a [PEP 440](https://peps.python.org/pep-0440/)
developmental release of the next one — `0.2.0a2.dev0` at the time of writing —
so a build from the branch and a build from a tag never answer the same string.
Exactly one commit ever carries a release version, and it is the tagged one.
Steps 1 and 6 below are what keep that true.

## 1. Prepare

* [ ] **Decide the release number.** The branch's `.dev` version names the
      release it is working toward, so the number is normally that version
      without its `.dev` segment: `0.2.0a2.dev0` releases as `0.2.0a2`. If the
      round turned out to be a beta or a final instead, choose that number here
      — a `.dev` version sorts below `0.2.0a2`, `0.2.0b1` and `0.2.0` alike, so
      nothing already installed becomes wrong.
* [ ] **Drop the `.dev` segment** in the two places that hold the version, and
      nowhere else:
      * `pyproject.toml` — `version`
      * `nautilus_gateio/__init__.py` — `__version__`

      They must be identical strings: `tests/test_package.py` fails if they are
      not, and the `build` job compares the `pyproject.toml` value against the
      filenames in `dist/`. Write the version in PEP 440 canonical form —
      `0.2.0a2`, not `0.2.0-a2` or `0.2.0.a2` — because the build backend
      normalizes what goes into those filenames and the comparison is literal.
      `tests/test_package.py::TestPublicApi::test_version_is_canonical_pep440`
      checks the form before the build job does.
* [ ] `CHANGELOG.md`: rename `## [Unreleased]` to `## [<version>] - <date>`,
      drop the line naming the branch version, and list breaking changes first,
      then additions and fixes.
* [ ] `CHANGELOG.md` link definitions at the foot of the file:
      ```
      [Unreleased]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v<version>...HEAD
      [<version>]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v<previous>...v<version>
      ```
* [ ] Every page that states the version this branch **is** now names the release
      being cut: the `README.md` Status line, its requirements table and its
      `pip freeze` passage, the `SECURITY.md` supported-versions table, the
      branch note in `CONTRIBUTING.md` and the Stage 8 line in
      [roadmap.md](roadmap.md). References to an *earlier* release, or to a tag,
      stay as they are — they are history.
* [ ] The [feature support matrix](../README.md#feature-support-matrix) and
      [validation status](validation.md) reflect what was actually exercised.
      Nothing is **Stable** without a recorded mainnet result.
* [ ] The [migration guide](migration-0.1-to-0.2.md) covers every breaking
      change in this release.

## 2. Verify

```bash
pip install -e '.[dev]'
ruff check nautilus_gateio tests examples
ruff format --check nautilus_gateio tests examples
pytest
```

Run the credential-free examples against the live venue — they are the fastest
check that the documented API still works end to end:

```bash
python examples/01_public_rest.py
python examples/02_public_websocket.py
python examples/03_instruments.py
```

## 3. Build from a clean tree

**Always remove the previous build artifacts first.** `dist/` is not cleaned by
the build backend, so stale wheels from an earlier version survive there — and a
bare `dist/` upload glob would happily republish them.

```bash
rm -rf dist/ build/ *.egg-info
python -m build
twine check dist/*
ls dist/
```

`ls dist/` must show exactly two files, both carrying the version being
released: `nautilustrader_gateio_adapter-<version>-py3-none-any.whl` and
`nautilustrader_gateio_adapter-<version>.tar.gz`.

Both filenames must carry the version from `pyproject.toml` with no `.dev`
segment. A `.dev` in a filename here means step 1 was skipped, and the artefact
must not be published.

## 4. Verify the built wheel, not the source tree

A wheel can be missing sub-packages while every import still works in the source
checkout, because the checkout is on `sys.path`. Install into a throwaway
environment **outside the source tree** and import from there:

```bash
python -m venv /tmp/relcheck && /tmp/relcheck/bin/pip install -q dist/*.whl
cd /tmp && /tmp/relcheck/bin/python -c "
import nautilus_gateio
from nautilus_gateio import GATEIO, GateioDataClient, GateioExecutionClient
from nautilus_gateio.common.symbols import instrument_id_to_gateio
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.websocket.public import GateioPublicWebSocket
print(nautilus_gateio.__version__, GATEIO)
"
```

The version printed here is the one being released, with no `.dev` segment.

CI performs this check on every push (the `build` job), so a broken package list
fails there rather than in a published artefact.

## 5. Publish

Commit the version change on its own, so the tag lands on a commit whose only
subject is the release:

```bash
git commit -am "Release v<version>"
git push origin main
```

> **Do not upload to PyPI yet.** The current distribution name,
> `nautilustrader-gateio-adapter`, uses the NautilusTrader trademark and is going
> to be replaced by `gateio-nt-community` (import `gateio_nt`). A PyPI name is
> never released once claimed, so uploading under the present name is an
> irreversible mistake, not a step that can be undone in the next release.
> Releases are cut as git tags with their artefacts attached until the rename
> has landed. When it has, upload the files for **this** version explicitly and
> never with a bare glob:
>
> ```bash
> twine upload dist/<distribution_name>-<version>*
> ```

Then tag and push:

```bash
git tag -a v<version> -m "v<version>"
git push origin v<version>
```

A tag, once pushed, is a fixed point: it is never moved, and the artefacts built
from it are never rebuilt or replaced. A mistake in a release is corrected by the
next release, not by re-cutting this one.

## 6. Reopen the branch

Do this in the same sitting, before anything else is merged. Until it is done,
the branch and the tag answer the same version, and a bug report cannot say which
of the two it is about.

Set both version places to the next developmental release — the next pre-release
number with `.dev0`:

* `pyproject.toml` — `version = "0.2.0a3.dev0"`
* `nautilus_gateio/__init__.py` — `__version__ = "0.2.0a3.dev0"`

PEP 440 orders `0.2.0a2 < 0.2.0a3.dev0 < 0.2.0a3`, which is what makes the string
honest about which side of the release a build sits on. The number is a
statement of intent, not a promise: if the next release turns out to be a beta or
a final, step 1 retargets it.

Open a fresh `## [Unreleased]` in `CHANGELOG.md` and put the branch version under
it:

```
A build of this branch reports `0.2.0a3.dev0`. Everything in this section is on
the branch and in no released version; `0.2.0a2` is below.
```

Commit it separately:

```bash
git commit -am "Open the branch for the next release"
git push origin main
```

## 7. After the release

* [ ] The branch reports the next `.dev` version and the tag reports the release
      — check both, not one:
      ```bash
      pip freeze | grep gateio
      ```
* [ ] The GitHub release notes match the changelog section.
* [ ] Install the published artifact in a fresh environment and run the
      credential-free examples once more.
* [ ] Open a follow-up issue for anything the release notes had to describe as
      not yet validated.
