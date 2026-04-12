# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-04-12

### Added

- Structured GitHub issue forms for bug reports and feature requests.
- A top-level `VERSION` file and a Keep a Changelog-based release log.

### Changed

- Standardized the project on Semantic Versioning with `0.2.0` as the first intentional release baseline.
- Simplified the public project surface to versioning, changelog tracking, and GitHub Issues.
- Updated the Plasma widget metadata to advertise the public GitHub repository and the current release version.
- Switched the plasmoid state read path away from the heavy Python helper to a lightweight guarded runtime-file read.

### Fixed

- Prevented the installer from following a development symlink and deleting the source checkout during plasmoid upgrades.
- Preserved the plasmoid's quiet behavior when the state file is missing and stopped idle polling when the runtime directory is unavailable.

[Unreleased]: https://github.com/pizzimenti/wifimimo/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pizzimenti/wifimimo/releases/tag/v0.2.0
