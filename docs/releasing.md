# Releasing

Checklist for cutting a release of `nautilustrader-gateio-adapter`.

## 1. Prepare

* [ ] `CHANGELOG.md` has a section for the new version listing breaking changes
      first, then additions and fixes.
* [ ] `pyproject.toml` `version` and `nautilus_gateio.__version__` agree with it.
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

CI performs this check on every push (the `build` job), so a broken package list
fails there rather than on PyPI.

## 5. Publish

Upload the files for **this** version explicitly. Never use a bare glob:

```bash
twine upload dist/nautilustrader_gateio_adapter-<version>*
```

Then tag and push:

```bash
git tag -a v<version> -m "v<version>"
git push origin v<version>
```

## 6. After the release

* [ ] The GitHub release notes match the changelog section.
* [ ] Install the published artifact in a fresh environment and run the
      credential-free examples once more.
* [ ] Open a follow-up issue for anything the release notes had to describe as
      not yet validated.
