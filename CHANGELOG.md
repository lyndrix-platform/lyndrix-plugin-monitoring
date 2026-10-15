# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-10-15
### Added
- Setting `probe_roles` (comma list of instance roles): only instances carrying one of them contend for the `monitoring:prober` lease (e.g. `runner` hosts with ICMP/NET_RAW). Empty = any instance. `health()` reports `probe_eligible`.
- Subscribes to the bridged `plugin:settings_changed` (core ≥ 1.5.1) and re-applies the settings cluster-wide; settings are now also loaded at plugin start.
- Cluster quiesce: destructive maintenance emits `monitoring:quiesce` (`cluster=True`, TTL 120 s) so the prober stops probing even when the job runs on a standby.
- Unique key `(monitor_id, day)` on `monitoring_daily_aggregates` via a deduplicating, idempotent migration; MariaDB probe writes upsert the aggregate with `INSERT … ON DUPLICATE KEY UPDATE`.

### Fixed
- `POST /inventory-sync` now reloads the probe jobs, starts the sweep and announces the change (new monitors were never probed until a restart).
- Upserts/overrides from worker threads no longer fail with "no running event loop" — scheduler changes and `ctx.emit` are marshalled onto the core loop.
- A failing scheduled job (e.g. DB error) no longer ends permanently; it is logged and retried with a small backoff.
- `apply_admin_override` announces `monitoring:monitors_changed`.
- `/maintenance/probe-all` on a standby forwards to the prober (`reason: probe_all`) instead of setting a local flag nobody reads.
- `clear_states` uses batched READ COMMITTED deletes with 1020/1205/1213 retry, quiesces the prober and resyncs (API and NiceGUI danger zone).
- Rollups and uptime windows commit in chunks of 200 monitors (READ COMMITTED, retry) instead of one transaction over every monitor row.
- `/maintenance/prune` on a former prober works again (`_stopping` reset after a lease loss).

### Changed
- `upsert_monitor` no longer runs DDL on every call (bootstrap creates the tables under the cluster lock).
- Probe and passive-result writes use READ COMMITTED sessions on MariaDB.

### Removed
- Unused `messaging:outbound` emit permission.

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
