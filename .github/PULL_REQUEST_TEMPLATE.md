# Pull request

## Summary

<!-- What does this change do, and why? -->

## Linked issue

<!-- e.g. "Closes #12", or "N/A" for small fixes -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Tests / CI

## Checklist

- [ ] `pytest` passes locally (unit tests need no network or credentials)
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] Docs and the README feature matrix are updated if behavior changed
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` (if user-visible)
- [ ] The version in `pyproject.toml` and `nautilus_gateio/__init__.py` is unchanged — a release
      sets it, a pull request does not
- [ ] No credentials, API keys, or account identifiers committed (including fixtures)
