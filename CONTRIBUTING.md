# Contributing

## Releasing / publishing to PyPI

Releases are published by GitHub Actions (`.github/workflows/publish-to-pipy.yml`)
when a tag matching `v<version>` is pushed. The workflow cross-checks that
the tag matches the `version` in `pyproject.toml`, builds the wheel and
sdist, runs `twine check`, and uploads to PyPI.

To cut a release:

```bash
# 1. bump version in pyproject.toml, commit to main
# 2. tag and push — this triggers the publish workflow
git tag v0.1.0
git push origin main v0.1.0
```

Watch the run at the repo's Actions page; on success `asf02` appears on
https://pypi.org/project/asf02/.

### Auth: PyPI trusted publishing (OIDC, PEP 741)

No API token is stored in this repo. One-time setup on the PyPI side: in
the `asf02` project settings on pypi.org (pypi.org/projects/asf02/publishing),
under **Publishing**, add a trusted publisher with the PEP 741 configuration
for this repo (owner, repository, and the `publish.yml` workflow). Note that
trusted publishing requires the repository to be **public** — PyPI verifies
the workflow via GitHub's OIDC, which it cannot do for private repos.

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```
