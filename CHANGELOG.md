# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.0] - 2026-11-15
Security and reliability audit 2026-11.
Minimum core unchanged (API 1.13); newer core APIs are used only behind `getattr`. No React bundle rebuild needed.
Roll out together with (or right after) the IaC orchestrator release that stops sending raw `terraform`/`config`
blocks. Recommendation, the owner decides: treat the credentials that were readable through the monitoring API as
exposed (MO-01); the upgrade stops the exposure but does not invalidate them.

### Security
- **Inventory metadata is allow-listed (MO-01, critical).** The inventory sync copied the raw `terraform` block of every
  host and the `config` block of every service into `metadata_json`, and `GET /dashboard` / `GET /monitors/{id}` returned
  it to every reader. Only an allow-list of placement facts is stored now, and existing rows are scrubbed on start.
- The SSRF guard covers ICMP and TCP monitors too; ICMP pings the validated address. A monitor on a loopback, link-local
  or metadata address is DOWN ("blocked address"), never "refused". API replies show `target`/`address` without
  `user:pw@` (MO-22). Probe error texts (`latest_error`, heartbeats, outages, logs, bus events) lose `user:pw@` too.
- `maintenance:override_started` from the bus is capped at start + max(`override_max_minutes`, 24 h);
  `maintenance:windows_published` replaces the source's earlier windows only with an explicit `"replaces": true`
  (it was the default); the origin of a quiesce request is logged (MO-17).
- `POST /admin-override` (`update`) validates like an upsert: an interval below 10 s, a timeout of 0 or a wrong type is
  HTTP 400 instead of being stored (MO-16).

### Fixed
- Probes have a hard deadline, DNS resolution runs off the event loop, probe kinds use their own pools, and an
  unavailable ICMP socket is "unknown" instead of DOWN (MO-04, MO-05, MO-06, MO-07).
- Alert suppression follows the current maintenance state, a DOWN is confirmed by a fresh probe before an outage is
  persisted, and one `effective_state` is computed per CI (MO-02, MO-03, MO-12).
- Every path that discards an outage emits `monitoring:outage_recovered`; outage events go through an outbox and survive
  a restart between the state change and the emit (MO-08, MO-09).
- A monitor whose parent is down is suppressed instead of opening its own outage (MO-10).
- `health()` reports `probes_stale` when the prober stopped writing (MO-11).
- The inventory stale-delete has a plausibility guard, deletes in batches and quiesces the prober first (MO-15).
- The warm-up panel (503) is shown only for the very first sweep without data, afterwards a banner; losing the prober
  lease stops probing only, and writes of probes started under the old lease are discarded (MO-14, MO-20).
- The quiesce marker is renewed every TTL/2 (MO-18); naive timestamps are read as UTC (MO-19); the 00:05 roll-up
  computes the day that ended (MO-21).

### Changed
- `GET /overview?hours=` rounds up to 24, 72 or 168 and returns the span it used in `hours`; the timeline aggregate is
  computed once per span instead of per request (MO-13).

## [1.7.0] - 2026-10-18
### Added
- Maintenance windows, overrides and the outage rule (core API ≥ 1.13, `core.api.maintenance`; `min_core_version` 1.11.0):
  - New tables `monitoring_maintenance_windows`, `monitoring_overrides`, `monitoring_outages` (idempotent DDL under the bootstrap lock) and the module `app/controller/maintenance.py` (`MaintenanceStore`, per-instance `MaintenanceCache`, `MaintenanceManager`).
  - Consumes `maintenance:windows_published` (`replaces: true` swaps every row of the publishing source in one transaction; kinds are open — any valid slug is stored), `maintenance:override_started` / `override_ended` (`until` + `linger_minutes`). Own windows/overrides (`source: "monitoring"`) are ignored by the handlers; the cache of the peers is invalidated with the plugin's own `monitoring:monitors_changed` broadcast (`reason: maintenance_windows`).
  - Monitor state `MAINTENANCE`: a failed probe (active or passive) inside an active window/override is stored as `MAINTENANCE` instead of `DOWN`; a successful probe stays `UP`, `latest_error` is kept. New aggregate column `maintenance_samples`; the uptime denominator is `total − paused − maintenance`. Roll-up order `DOWN > UNKNOWN > MAINTENANCE > UP > PAUSED`; colour `--lx-state-maintenance` (fallback paused), badge `lx-badge--maintenance`; header counter "Maintenance".
  - `ci_id` / `host_ci_id` on monitors — taken from the orchestrator's inventory sync (hosts and services, orchestrator ≥ 1.6.0), part of the config hash; a service without its own ci borrows the ci of its host monitor.
  - Outage clock: `monitoring_outages` is written in the heartbeat transaction (`down_since`, `suppressed_until` = window end). A 60 s job on the probing instance (`monitoring:outages`; sharded: the owner, orphans by the housekeeping worker) reports an outage once `now − max(down_since, suppressed_until)` reaches the rule's `offline_minutes` — conditional `UPDATE … WHERE state='open'` → exactly one `monitoring:outage_persisted` (`outage_id = "<ci_id>:<since>"`), `monitoring:outage_recovered` after the recovering commit. Rule cascade: service CI → host CI → settings default; no `ci_id` → no event.
  - Settings: `outage_rules_enabled`, per-kind switches (`…_hypervisor/_host/_service/_container`), `outage_rules_disabled_kinds`, `outage_default_offline_minutes/severity/auto_resolve`, `override_default_minutes`, `override_max_minutes`, `override_linger_minutes` (apply at runtime).
  - API: `GET/POST /windows`, `DELETE /windows/{id}` (manual rows only), `GET/POST /overrides`, `DELETE /overrides/{id}`, `GET /outages`. Manual windows/overrides are published locally (`maintenance:*`, `source: "monitoring"`) for the registry and the server manager.
  - React UI: `MaintenanceDialog` (override 30/60/120/240 min + reason, or weekly/once window) from the host detail card and the toolbar (host picker), `OverridesPanel` with "End" for own overrides, badge "Maintenance · until hh:mm"; NiceGUI mirrors colour and counters.
  - `monitoring:state_changed` carries `ci_id` and `maintenance`; `health()` reports `monitors_maintenance`.

### Changed
- `apply_admin_override(pause)`, `delete_monitors`, `clear_states` and the inventory stale-delete also clear the outage clock (and manual windows pinned to a deleted monitor).

## [1.6.2] - 2026-10-16
### Fixed
- The "Show / hide sections" menu of the toolbar was clipped by the toolbar card (overflow hidden for the rounded corners); it is now positioned fixed from the button's rectangle and scrolls when it would leave the viewport.

## [1.6.1] - 2026-10-15
### Changed
- Narrowing `probe_roles` at runtime now takes effect at once: an instance that may no longer probe gives the prober lease up via `ctx.release_lease` (core API ≥ 1.9), its `stop_probing` runs and an eligible peer takes over on its next heartbeat. Older cores keep the previous behaviour (reload needed, warning logged).

## [1.6.0] - 2026-10-15
### Added
- Probe sharding: setting `probe_nodes` (default 1). With > 1 and core API ≥ 1.8, up to `probe_nodes` eligible instances (`probe_roles`) split the monitors via `ctx.my_shard("probe", …)` instead of one lease-holding prober; the shard is recomputed on `cluster:changed` and on every monitor-set change, starting/stopping only the jobs that moved.
- Global housekeeping (prune, rollups, uptime windows) runs on one `housekeeping` worker in sharded mode.
- `health()` reports `probe_mode`, `probe_nodes` and `shard_size`.

### Changed
- `probe_nodes` (within > 1) and `probe_roles` apply at runtime in sharded mode; switching between single prober and sharding needs a plugin reload.
- A resync requested while one is running is no longer dropped (runs again afterwards).
- Probe-all and quiesce reach every worker in sharded mode; a sharded instance always waits the quiesce grace period.

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
