# Contributing

Thanks for contributing to `wifimimo`.

## Before you open an issue

- Use the GitHub issue forms so reports arrive with the details needed to reproduce them.
- Open one bug or one feature request per issue.
- Include the version from `VERSION`, your distro, Plasma version, Wi-Fi chipset, and any relevant logs.
- Security problems should follow [SECURITY.md](SECURITY.md), not public issues.

## Before you open a pull request

- Keep the change scoped to one problem.
- Update user-facing docs when behavior changes.
- Add a changelog entry under `Unreleased` when the change is notable to users.
- If a release version changes, update `VERSION`, `plasmoid/org.kde.plasma.wifimimo/metadata.json`, and `CHANGELOG.md` together.

## Local checks

- `python3 -m py_compile wifimimo-daemon.py wifimimo-mon.py wifimimo-plasmoid-source.py wifimimo_core.py`
- `bash -n install.sh`
- `python3 -m json.tool plasmoid/org.kde.plasma.wifimimo/metadata.json >/dev/null`

## Versioning and releases

- This project uses Semantic Versioning: `MAJOR.MINOR.PATCH`.
- Increment `PATCH` for backward-compatible bug fixes.
- Increment `MINOR` for backward-compatible features or operational improvements.
- Increment `MAJOR` for incompatible changes.
- Release tags should use a `v` prefix, for example `v0.2.0`.
- Publish releases through GitHub Releases and keep `CHANGELOG.md` as the human-curated summary.
