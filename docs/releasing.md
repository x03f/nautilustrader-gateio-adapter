# Releasing

Checklist for cutting a release of `nautilustrader-gateio-adapter`.

**A release is published by pushing a tag, and by nothing else.**
[`.github/workflows/release.yml`](../.github/workflows/release.yml) runs on any
`v*` tag: it builds the sdist and the wheel from the tagged commit, checks the tag
against the version inside them, installs the wheel into a throwaway environment
outside the checkout and imports it, writes `SHA256SUMS`, attests the provenance of
those exact files, and creates the GitHub Release with them attached. A releaser
builds nothing that a user ever downloads. The local build in steps 3 and 4 below
is a rehearsal — it catches a broken package list before a tag exists, and its
artifacts are then thrown away.

Two things follow, and both matter:

* **Do not create the GitHub Release by hand.** The workflow checks first and
  stops with a message if one already exists, because a release it did not build
  carries no provenance. Delete the hand-made release and re-run the workflow.
* **Do not upload anything to a package index.** Not by hand, not from a
  workflow. The distribution name `nautilustrader-gateio-adapter` leads with the
  NautilusTrader mark, which their trademark policy does not permit for a project
  that is neither official nor affiliated, and it is being renamed to
  `gateio-nt-community` before anything is published to an index. A name claimed
  on PyPI is never released for reuse: uploading first and renaming afterwards
  would leave a squatted, infringing name behind permanently. Until the rename
  lands, an installation comes from a git URL or from the assets attached to a
  GitHub Release. This is enforced rather than asked for:
  `tests/test_ci_and_community.py` fails if any workflow, or any command written
  down on any page here, hands the distribution to an index — whichever tool does
  it, and whether it is invoked directly, through `python -m`, or through `uv`,
  `uvx` or `pipx`.

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

## 3. Rehearse the build from a clean tree

Nothing built here is published — the tag's workflow builds the artifacts a user
receives. This is the rehearsal that catches a broken package list, a bad
version string or a metadata error while it is still cheap, before a tag exists.

**Always remove the previous build artifacts first.** `dist/` is not cleaned by
the build backend, so stale wheels from an earlier version survive there — and
anything that globbed `dist/` would pick them up.

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
segment. A `.dev` in a filename here means step 1 was skipped, and the artifact
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

CI performs this check on every push (the `build` job) and the release workflow
performs it again on the artifacts it is about to attach, so a broken package
list fails there rather than in a published artifact.

Then discard the rehearsal, so nothing local can be mistaken for the release:

```bash
rm -rf dist/ build/ *.egg-info
```

## 5. Publish

Commit the version change on its own, so the tag lands on a commit whose only
subject is the release:

```bash
git commit -am "Release v<version>"
git push origin main
```

Then tag that commit and push the tag. **Pushing the tag is the publish button** —
it starts [`.github/workflows/release.yml`](../.github/workflows/release.yml),
and there is no other step:

```bash
git tag -a v<version> -m "v<version>"
git push origin v<version>
```

Watch the run. It fails, and publishes nothing, if the tag does not match the
version in `pyproject.toml`, if `twine check` rejects the metadata, if `dist/`
holds anything other than this version's two files, or if the wheel does not
import from outside the source tree. When it succeeds, the GitHub Release carries
the sdist, the wheel and `SHA256SUMS`, and a provenance attestation over the
sdist and the wheel — the same bytes, built by that run, from that commit.

Nothing else is uploaded anywhere. In particular there is no index upload: see
the note at the top of this page for why that step does not exist and must not be
added by hand.

A tag, once pushed, is a fixed point: it is never moved, and the artifacts built
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

* [ ] The release run is green and the GitHub Release carries three assets: the
      wheel, the sdist and `SHA256SUMS`.
* [ ] Verify the published artifacts the way a stranger would — from the release,
      not from the tree they were built in:
      ```bash
      gh release download v<version> --dir /tmp/verify
      cd /tmp/verify && sha256sum -c SHA256SUMS
      gh attestation verify nautilustrader_gateio_adapter-<version>-py3-none-any.whl \
        --repo x03f/nautilustrader-gateio-adapter
      ```
      The attestation names the workflow and the commit the bytes were built
      from. If it does not verify, the assets are not the ones the run produced.
* [ ] The branch reports the next `.dev` version and the tag reports the release
      — check both, not one:
      ```bash
      pip freeze | grep gateio
      ```
* [ ] The GitHub release notes match the changelog section. `--generate-notes`
      seeds the body from the commits; replace it with the changelog section.
* [ ] Install the downloaded wheel in a fresh environment and run the
      credential-free examples once more.
* [ ] Open a follow-up issue for anything the release notes had to describe as
      not yet validated.
