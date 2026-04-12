# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-04-12

### Added

- Public-project community files: `CONTRIBUTING.md`, `SUPPORT.md`, `SECURITY.md`, and a repository code of conduct.
- Structured GitHub issue forms for bug reports, feature requests, and support questions.
- A pull request template, release notes configuration, Dependabot configuration, and a basic GitHub Actions CI workflow.
- A top-level `VERSION` file and release-process documentation.

### Changed

- Standardized the project on Semantic Versioning with `0.2.0` as the first intentional release baseline.
- Updated project documentation to point contributors at GitHub Issues, Releases, the changelog, and the support/security paths.
- Updated the Plasma widget metadata to advertise the public GitHub repository and the current release version.
- Switched the plasmoid state read path away from the heavy Python helper to a lightweight guarded runtime-file read.

### Fixed

- Prevented the installer from following a development symlink and deleting the source checkout during plasmoid upgrades.
- Preserved the plasmoid's quiet behavior when the state file is missing and stopped idle polling when the runtime directory is unavailable.

[Unreleased]: https://github.com/pizzimenti/wifimimo/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pizzimenti/wifimimo/releases/tag/v0.2.0
