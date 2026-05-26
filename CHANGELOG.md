# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-26
### Changed
- Refactored to the new Lyndrix Core plugin standard (`./app/` sub-package layout).
- `entrypoint.py` is now a pure wiring layer — the inline `monitoring_page()` body has been moved to `app/ui/page.py`.
- Manifest `repo_url` corrected to the canonical `lyndrix-platform` repository URL.
- `min_core_version` bumped to `0.0.6` for alignment with other Lyndrix plugins.

### Added
- `CHANGELOG.md`.
- `requirements-dev.txt` with the standard toolchain (pytest, pytest-asyncio, pytest-cov, mypy, ruff, black).
- `tests/` scaffold with a smoke test asserting `MonitoringService` construction and the `plugin_state["service"] is None` initial state.

### Fixed
- `repo_url` previously pointed to a personal fork.

## [0.0.4] - earlier
- Last release on the legacy flat layout.
