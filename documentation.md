# State Monitoring — Documentation

## Overview

The State Monitoring plugin is the native infrastructure and service monitoring for Lyndrix. It maintains a monitor register with persistent status history, supports grouped overviews (by site, environment, etc.), timeline visualizations and admin overrides. The inventory comes from the IaC Orchestrator via HTTP (`POST /inventory-sync`, with `ci_id` per host and service) — it is the primary source for infrastructure monitors. Since 1.7.0 the plugin knows maintenance windows and overrides (state `MAINTENANCE`) and reports outages outside a window as `monitoring:outage_persisted` (see *Maintenance windows, overrides and outage rule*).

The plugin has **no notification path of its own** (no Discord, no notification router): state changes and outages are published on the event bus and consumed by other plugins (e.g. ITSM).

---

## Architecture

```
lyndrix-plugin-monitoring/
├── entrypoint.py               # Manifest + lifecycle hooks
├── locales/
│   └── monitoring.<locale>.json  # i18n translations (namespace: monitoring)
└── app/
    ├── controller/
    │   ├── api.py              # FastAPI router (auth + permission-gated)
    │   ├── service.py          # MonitoringService: register, history, stats
    │   ├── maintenance.py      # Maintenance windows, overrides, outage clock (store, cache, manager)
    │   ├── helpers.py          # Pure business logic: build_grouped_overview, flatten_monitors, …
    │   ├── scheduler.py        # Passive-check scheduler
    │   └── provider/           # Active check providers
    ├── model/
    │   └── models.py           # SQLAlchemy: Monitor, Heartbeat, daily aggregate, window, override, outage
    └── ui/
        ├── nicegui/            # NiceGUI UI: overview.py, timeline.py, cards.py, split.py, table.py, styles.py
        │   └── helpers.py      # Re-export shim → controller/helpers.py (backwards compatibility)
        └── react/              # React UI (canonical migration target)
```

**Important architecture note:** The business logic helpers (`build_grouped_overview`, `flatten_monitors`, `infer_site_and_stage`, `humanize_label` etc.) live in `app/controller/helpers.py`. The file `app/ui/nicegui/helpers.py` is a pure re-export shim for existing NiceGUI views — new callers import directly from `controller/helpers.py`. This follows the canonical anatomy rule: no business logic in the UI layer.

---

## API reference

All routes are reachable under `/api/plugins/lyndrix.plugin.state_monitoring/`. They require valid authentication and the matching permissions.

| Method | Path | Permission | Description |
|---|---|---|---|
| `GET` | `/dashboard` | `api:read` | Monitor list + statistics (cached, see performance model) |
| `GET` | `/overview` | `api:read` | Grouped + filtered overview for the React client (cached) |
| `GET` | `/monitors/{monitor_id}` | `api:read` | Single monitor (incl. `ci_id`, `in_maintenance`, `maintenance_until`, `outage_since`) |
| `GET` | `/history/{monitor_id}` | `api:read` | Status history |
| `POST` | `/monitors` | `api:write` | Create or update a monitor (upsert; `ci_id`/`host_ci_id` are part of the config hash) |
| `POST` | `/passive` | `api:write` | Receive a passive check result (`MAINTENANCE` is rejected — the state is derived) |
| `POST` | `/inventory-sync` | `api:write` | Bulk upsert from the IaC Orchestrator (hosts and services, with `ci_id`/`host_ci_id`) |
| `POST` | `/admin-override` | `api:write` | Admin override `pause` / `resume` / `update` |
| `POST` | `/clear-states` | `api:write` | Reset states and statistics (destructive) |
| `GET` | `/windows?ci_id=&monitor_id=&source=` | `api:read` | Stored maintenance windows (published + manual) with `effective` (`active`, `until`, `source`, `next_start`) |
| `POST` | `/windows` | `api:write` | Manual window `{ci_id \| monitor_id, windows: [...], note}` — input or normal form; validation errors → `400` with a message per window |
| `DELETE` | `/windows/{id}` | `api:write` | Manual rows only (`source == "monitoring"`), otherwise `409` |
| `GET` | `/overrides?active=true` | `api:read` | Overrides with `active`, `until_effective`, `own` plus `defaults` (minute defaults from the settings) |
| `POST` | `/overrides` | `api:write` | Manual override `{ci_ids \| monitor_ids, minutes, reason}` → `override_id = "mon-<uuid>"`; `minutes` is capped at `override_max_minutes` |
| `DELETE` | `/overrides/{id}` | `api:write` | Ends an own override now (`ended_at`, linger `override_linger_minutes`); others → `409` |
| `GET` | `/outages` | `api:read` | Open and reported outages with rule, `effective_since`, `effective_duration_s`, `in_maintenance` |

The maintenance actions under `/maintenance/*` (prune, rebuild, delete …) are the plugin upkeep (see *Maintenance actions*), not the maintenance windows.

### `/overview` endpoint

```
GET /overview?group_by=site&hours=24&include_paused=true&include_unknown=true
```

Returns the same grouped and filtered data that the NiceGUI dashboard displays — the business logic is identical, because the React client uses `controller/helpers.py` through this endpoint rather than its own frontend logic. The response contains `generated_at`; `hours` is (since 1.8.0) rounded up to 24, 72 or 168 (values above are capped at 168); the span actually used is in `hours` of the response. The timeline aggregate is computed once per span, not per request.

During the startup sequence (see below) the **core** answers every route of the plugin with `503` and `Retry-After` — the client does not have to handle this itself; the React client shows the progress and asks again shortly afterwards.

---

## Performance model

Starting point (2026-10): `/overview` took 200 s with 200 monitors and 1.7 million heartbeats, because every read operation aggregated the **entire** heartbeat table (`uptime_all`), the React client polled every 20 s and gave up after 15 s (the server query kept running), and the core `health()` triggered the same aggregation on every dashboard call. Since v1.2.0 the following applies:

| Read path | Where the data comes from |
|---|---|
| `uptime_all` (list, stats) | rolling 30 days from `monitoring_daily_aggregates` — a small query over ≤ 30 × monitors rows |
| `uptime_24h` / `7d` / `30d` | hourly job (`monitoring:uptime_hourly`) in one pass for all monitors, no longer per heartbeat |
| Timeline (24 h bars) | counted by the database in hour buckets (`_hourly_state_counts`, ≤ 24 × monitors rows) instead of loading all heartbeats of the day as ORM objects; result identical to `timeline_from_history` |
| `/overview`, `/dashboard` | single-flight cache: concurrent callers share one computation, result is valid for `overview_cache_seconds` (default 10 s); a state change invalidates it |
| `health()` | `status_counts()` (one `GROUP BY`) and `probe_freshness()` (a narrow query on the monitor table, never on the heartbeats); reports `degraded` with `probes_stale` when the prober no longer writes, and with `icmp_unavailable` when pinging is not possible (1.8.0) |

Write path: per heartbeat, the daily aggregate record is updated **incrementally** (counters + running latency mean via `latency_samples`); the daily rollup job recomputes it from the raw data. The index `(monitor_id, created_at)` turns the 24 h windows into index range scans.

**Retention:** heartbeats older than `heartbeat_retention_days` (default 7) are deleted — one minute after the startup sequence, hourly and daily, always in batches of 20 000 rows (each statement stays far below the core's 60 s statement limit). The former pure midnight job never ran if the process had been restarted beforehand.

For measuring: `curl -w %{time_total}` against `/overview` and `SHOW PROCESSLIST` on the database — no long-running `monitoring_heartbeats` queries should appear anymore.

---

## Startup sequence and maintenance windows

After start (and after a reload) the plugin checks all monitors that are unknown, down or stale, with `initial_probe_workers` parallel probes. In doing so it reports to the core (API ≥ 1.5) `ctx.set_status("starting", progress=(done, total), blocking=True)`:

- the core answers all plugin routes with `503 Retry-After`,
- the React shell shows a warm-up view with a progress bar instead of the plugin UI and switches over automatically as soon as `ready` is reported (SSE `plugin:runtime_status`),
- `/api/health` does not call `health()` in the meantime.

The window is only blocking if **unknown or stale** monitors are part of it — re-checking a set of permanently failed monitors after each restart runs as a non-blocking startup message. On a warm start without stale monitors there is no window. Inventory import and "Clear states" run as a **non-blocking** maintenance window (banner in the shell). `stop()` sets a stop flag and waits up to 5 s for running probes, so that a reload does not start a second sweep round against the database. On an older core without `ctx.set_status` everything runs unchanged, just without display.

---

## Cluster operation (core API ≥ 1.7)

If several core instances run behind a load balancer (`LYNDRIX_CLUSTER_MODE=cluster`, see the core docs *High Availability*), the following applies to this plugin:

- **Every instance serves the API** (`/overview`, `/dashboard`, maintenance actions …) from the shared database. The plugin has `placement: any`.
- **Exactly one instance probes.** After loading the settings the plugin registers `ctx.on_lease("monitoring:prober", acquired=start_probing, lost=stop_probing)`. Only the lease holder starts the scheduler, the startup sweep and housekeeping (prune, rollups, uptime windows). If it dies, another instance takes over within the lease TTL (default 30 s). In single mode the lease is granted immediately — one code path for both modes. On an older core without `on_lease` the plugin starts directly as before.
- **Standby instances** report `prober: false` in `health()` and no scheduler error; they only create the tables (under the core bootstrap lock, so that N instances do not run the DDL against each other).
- **Changes to the monitor set travel as a cluster event.** Inventory import (arrives where the orchestrator runs), single upserts and the maintenance actions send `monitoring:monitors_changed` with `cluster=True`; the prober then reloads its jobs and probes the new monitors.
- **Probe location (`probe_roles`).** If the setting is set (comma-separated list of roles from `LYNDRIX_INSTANCE_ROLES`, e.g. `runner`), only an instance with one of these roles competes for the prober lease — this allows probing to be pinned to Docker hosts with ICMP/`NET_RAW` and suitable network access. Empty (default) = every instance. If no live instance carries one of the roles, nobody probes (warning in the log, `health()` shows `probe_eligible: false`). Changes take effect immediately (`plugin:settings_changed`): an **extension** lets the instance bid for the lease again, a **restriction** releases a held lease via `ctx.release_lease` (core API ≥ 1.9) at once — `stop_probing` runs, and an eligible instance takes over at the next heartbeat. On older cores a restriction only takes effect after a plugin reload/restart.
- **Distributed probing (`probe_nodes` > 1, core API ≥ 1.8).** Instead of one lease, up to `probe_nodes` instances (filtered by `probe_roles`) share the monitors via rendezvous hashing (`ctx.my_shard("probe", monitor_ids, count=probe_nodes, roles=…)`): each instance computes the same split locally, and every monitor has exactly one owner. It is recalculated on `cluster:changed` (membership) and on every change to the monitor set (`monitoring:monitors_changed`, inventory import, upsert); only jobs for newly assigned monitors are started and jobs for released ones are stopped — running jobs are left untouched. If an instance leaves the cluster, only its monitors move. While the view of the members differs for a few seconds, a monitor can be probed twice (at-least-once) — harmless for probes. Global tasks (prune, rollups, uptime windows) are handled by exactly one worker (`ctx.is_worker("housekeeping")`). "Check all monitors now" lets every worker check its own share, quiesce pauses all workers. Without `ctx.my_shard` (older core) the plugin falls back to the single prober with a warning. `health()` reports `probe_mode` (`lease` | `sharded`), `probe_nodes` and `shard_size`.
- **Settings apply cluster-wide.** When the operator saves the plugin settings, the core (≥ 1.5.1) sends the bridged event `plugin:settings_changed`; each instance then re-reads the values from Vault.
- **"Check all monitors now" on a standby** sets nothing locally, but sends `monitoring:monitors_changed` with `{"reason": "probe_all"}` (`cluster=True`); the prober starts the full sweep on receipt. The response contains `prober_here`.
- **Destructive maintenance stops the prober cluster-wide (quiesce).** Deleting statistics, states or monitors runs on the instance that receives the request. Beforehand it sends `monitoring:quiesce` `{"on": true, "ttl": 120}` (`cluster=True`); the prober then skips all probes. If the executing instance is not the prober itself, it additionally waits 3 s (bus relay + completion of running probes). Afterwards `{"on": false}` and `monitoring:monitors_changed` (`maintenance`) → the prober reloads its jobs. If the "off" event is lost, the quiesce ends by itself after the TTL. The deletions additionally run in READ-COMMITTED batches with retry on 1020/1205/1213, so a late probe write does not abort them. The blocking maintenance window (503) only applies on the executing instance. The event was chosen instead of `ctx.lease("monitoring:prober")` because the one-shot lease would never be granted on a standby (the prober holds it permanently).
- **Inventory import via REST** (`POST /inventory-sync`) likewise synchronizes the jobs, triggers the sweep and reports the change cluster-wide — like the bus path.
- **Background jobs survive errors.** If a scheduler run fails (e.g. DB error), this is logged and the job keeps running; after repeated errors with a short additional pause (2, 4, 8 … max. 60 s). Rollups and uptime windows write in chunks of 200 monitors (READ COMMITTED, retry).
- **Lost writes are not an error.** If saving a probe result fails with MariaDB 1020/1205/1213 (maintenance on another instance, lease handover), the sample is discarded and the next interval run probes again — the scheduler job keeps living.

---

## Events

All topics are listed in `permissions.subscribe` resp. `permissions.emit` of the manifest (the core rejects undeclared emits with `PermissionError`).

| Direction | Topic | Description |
|---|---|---|
| subscribe | `db:connected` | Database ready — initialize tables (under the bootstrap lock), load the maintenance cache |
| subscribe | `vault:ready_for_data` | Apply plugin settings (retention, workers, cache, `probe_roles`, outage rule, override limits) from Vault |
| subscribe | `cluster:changed` | Core ≥ 1.6: membership changed → in distributed mode recalculate own share |
| subscribe | `plugin:settings_changed` | Core ≥ 1.5.1, bridged: operator has saved the settings → reload on matching `plugin_id` |
| subscribe | `monitoring:config_upsert` | `MonitorUpsert` via the bus (like `POST /monitors`) |
| subscribe | `monitoring:passive_result` | `PassiveResult` via the bus (like `POST /passive`) |
| subscribe | `monitoring:admin_override` | `AdminOverride` via the bus (like `POST /admin-override`) |
| subscribe | `maintenance:windows_published` | Core API ≥ 1.13: windows of a publisher (orchestrator, more in future). `replaces: true` replaces all rows of this `source` in one transaction — without an explicit `"replaces": true` (since 1.8.0, previously the default) earlier windows of the source remain; items without `ci_id`, with an invalid kind or an invalid window are skipped individually. `source: "monitoring"` (own windows) is ignored |
| subscribe | `maintenance:override_started` / `override_ended` | Override of a publisher (running deploy); upsert per `override_id`, end with `ended_at` + `linger_minutes`. Own ones (`source: "monitoring"`) are ignored. Since 1.8.0 the end is capped at start + max(`override_max_minutes`, 24 h) |
| subscribe / emit | `monitoring:monitors_changed` | Monitor set changed (inventory, upsert, admin override, maintenance) — with `cluster=True` to all instances; the prober reloads its jobs, on `reason: probe_all` it starts a full sweep; **`reason: maintenance_windows`** only reloads the maintenance cache (every instance, no job resync) |
| subscribe / emit | `monitoring:quiesce` | `{"on": bool, "ttl"}` with `cluster=True` — destructive maintenance on another instance, the prober pauses all probes; the marker is renewed every TTL/2, the origin of the request is logged (1.8.0) |
| emit | `monitoring:state_changed` | State change of a monitor `{monitor_id, previous_state, new_state, timestamp, error_message, ci_id, maintenance}` — local, before the commit (cache/UI signal only) |
| emit | `maintenance:windows_published` | Manual windows of this plugin `{source: "monitoring", published_at, replaces: true, items}` — **local**, after each creation/deletion; only rows with `ci_id` |
| emit | `maintenance:override_started` / `override_ended` | Manual overrides from the UI/API, `source: "monitoring"` — **local** |
| emit | `monitoring:outage_persisted` | Once per outage, after `offline_minutes` outside any maintenance — **local**, only from the probing instance (payload see below) |
| emit | `monitoring:outage_recovered` | `{outage_id, ci_id, since, recovered_at, duration_s}` — **local**, after the commit of the heartbeat that sets the monitor back to `UP`; since 1.8.0 also on every other path that discards an outage |

Since 1.8.0 the outage events run through an outbox (`monitoring_outage_events`): a restart between state change and emit does not lose them. A DOWN is confirmed by a fresh probe before it is persisted; a monitor whose parent is DOWN is suppressed instead of opening its own outage.

Transport rule (contract `00-ergebnis-p0.md` §3): `maintenance:*` and `monitoring:outage_*` are emitted **without** `cluster=True` — plugins with `placement: any` run on every instance, the local consumer writes to the shared database, and exactly one instance processes each event. Peers learn about changed windows via their own `monitoring:monitors_changed` (`reason: maintenance_windows`). The handlers are nevertheless idempotent (unique key per `(source, ci_id, monitor_id)`, upsert per `override_id`), so that a later switch to `cluster=True` breaks nothing.

### Inventory synchronization

The IaC Orchestrator **posts** its inventory with the system API key to `POST /inventory-sync` (at boot and after every `NativeGenerateStage`), not via the bus. Per host `host:<name>` is created (type `server`), per service `service:<host>:<name>` (`http` for a URL target, otherwise `service`). Since orchestrator 1.6.0 hosts carry `ci_id` and services `ci_id` + `host_ci_id` (Entity Registry UUIDs); they land on the monitor and are part of the config hash. Monitors from this source that are missing in the payload are deleted, together with their history, outage clock and manual windows pinned to them. A service monitor without its own `ci_id` takes over the `ci_id` of its host monitor for windows, overrides and outage messages.

Since 1.8.0 (audit MO-01) only an allowlist of placement facts is taken over into `metadata_json` (among others `site`, `stage`, `location`, `groups`, `baseline_roles`, `profiles`, `deploy_type`, `desired_state`, `terraform.node_name`, `terraform.vm_id`); the raw `terraform`/`config` blocks are no longer stored, existing rows are cleaned up at startup. The stale delete has a plausibility check: an import that would remove more than `inventory_max_stale_percent` of the IaC monitors (or arrives empty) deletes nothing, is logged and triggers the notification `inventory_guard`. The publisher can confirm an intended mass deletion with `allow_mass_delete=true`. Deletion runs in batches, the prober is paused beforehand. Orchestrator releases that no longer send raw `terraform`/`config` blocks should be rolled out together with 1.8.0.

### Probe security (1.8.0)

- The SSRF protection also applies to ICMP and TCP monitors; ICMP pings the validated address. Targets on loopback, link-local or metadata addresses are `DOWN` ("blocked address").
- API responses show `target`/`address` without `user:pw@`; error texts (`latest_error`, heartbeats, outages, logs, bus events) likewise.
- `POST /admin-override` (`update`) validates like an upsert: interval < 10 s, timeout 0 or wrong type yields HTTP 400.
- Probes have a hard time limit, DNS resolution runs outside the event loop, the probe types use their own pools. An unavailable ICMP socket yields `UNKNOWN` instead of `DOWN`.

---

## Maintenance windows, overrides and outage rule (1.7.0)

The basis is `core.api.maintenance` (core API ≥ 1.13): window formats `weekly` / `once` / `cron + duration` (normal form `{"type": …}`), DST-safe evaluation, override semantics (`until` + `linger_minutes`) and the bus contract. The core holds **no** data — the plugin stores what it receives and evaluates when reading.

### Storage and cache

| Table | Content |
|---|---|
| `monitoring_maintenance_windows` | one row per `(source, ci_id, monitor_id)`: `kind`, `name`, `host_ci_id`, `windows_json` (normal form), `inherited_from`, `incidents_json` (rule of the publisher), `deploy_policy`, `note`. Published rows have `ci_id`, manual ones (`source = monitoring`) `ci_id` and/or `monitor_id` |
| `monitoring_overrides` | `override_id` (PK), `source`, `ci_ids_json`, `monitor_ids_json` (manual only), `until`, `started_at`, `ended_at`, `linger_minutes`, `reason` |
| `monitoring_outages` | the outage clock per monitor (see below) |

Each instance keeps an **in-memory cache** (`MaintenanceCache`): windows per `ci_id` and per `monitor_id`, active overrides, rules per `ci_id`, kind per `ci_id`, mapping host name → host `ci_id`. The probe hot path reads only this cache (no DB access per heartbeat). It is reloaded after every own write, on `monitoring:monitors_changed` (`reason: maintenance_windows`) from a peer, after inventory import, at bootstrap and at the latest every 5 minutes by the outage job. Times are stored naively in UTC in the tables; the controller converts them.

### `MAINTENANCE` state

Every heartbeat (active probe, passive result) runs through `_store_heartbeat`: if the measured state is `DOWN` and a window or override is active for the monitor (`effective_state(...)` over windows/overrides at the `monitor_id` and at the effective `ci_id`), `MAINTENANCE` is stored — **only `DOWN` is replaced**, a successful probe stays `UP`, `latest_error` is kept for diagnosis. The flap dampening (three failures) applies before that. Consequences:

- Daily aggregate: own column `maintenance_samples`; the uptime denominator is `total − paused − maintenance` (aggregates, 24 h heartbeat window, rollups).
- Precedence for hosts and groups: `DOWN > UNKNOWN > MAINTENANCE > UP > PAUSED`.
- Colour `--lx-state-maintenance` (core ≥ 1.11 / shell ≥ 1.8), fallback `--lx-state-paused`; badge `lx-badge--maintenance`; in the React UI "Maintenance · until hh:mm", its own counter in the header card, widget and NiceGUI page.
- The probe job keeps running (recovery is detected); `MAINTENANCE` monitors are treated like `DOWN` in the sweep. `PassiveResult` with `MAINTENANCE` is rejected (validation error).
- `GET /overview`, `/dashboard`, `/monitors/{id}` return per monitor `ci_id`, `in_maintenance`, `maintenance_until`, `maintenance_source` (`window` | `override`), `outage_since`; hosts additionally `monitor_ids`, `ci_ids`, `in_maintenance`, `maintenance_until`.

### Outage clock and outage rule

`monitoring_outages` is written **in the same transaction** as the heartbeat (no phantoms):

- `DOWN`/`MAINTENANCE` without a row → `state = open`, `down_since = now`, `suppressed_until =
  window end` (if maintenance is active). Every further heartbeat in maintenance writes
  `suppressed_until = max(…, window end)`; `down_since` stays.
- `UP` and `state = open` → row deleted (never reported). `UP` and `state = persisted` →
  conditional `UPDATE … WHERE state='persisted'`, on a hit **after the commit**
  `monitoring:outage_recovered` (`duration_s = recovered_at − down_since`), row deleted.
- `UNKNOWN` changes nothing; `pause`, `clear-states`, `delete-monitors` and the inventory
  reconciliation delete the rows along with them.

The interval job `monitoring:outages` (60 s, only on the probing instance; in shard mode the
owner of the monitor, orphaned rows are taken over by the `housekeeping` worker) checks every
open row: if maintenance is running → advance `suppressed_until`. Otherwise
`effective outage time = now − max(down_since, suppressed_until)`; once it reaches the
`offline_minutes` of the rule, the row is set to `persisted` via a conditional
`UPDATE … WHERE state='open'` — `rowcount == 1` wins, also during a lease change or with two
simultaneous instances — and **emitted once**:

```json
{"outage_id": "<ci_id>:<since ISO-UTC>", "ci_id": "…", "kind": "host", "name": "<Monitor-Name>",
 "host_ci_id": null, "monitor_id": "host:docker-hydra", "since": "…Z", "persisted_at": "…Z",
 "duration_s": 600, "rule": {"offline_minutes": 10, "severity": "high", "auto_resolve": true},
 "error": "connection refused"}
```

`outage_id` is deterministic (`make_outage_id`) and serves ITSM as `external_id`. Without a
`ci_id` on the monitor (also not via the host monitor) there is **no event**, only a log entry
per monitor; the row stays visible for `GET /outages`.

**Rule cascade** (`rule_for`): `incidents` of the published item of the service `ci_id` → the
host `ci_id` → settings default (`outage_default_*`). `enabled: false` or `offline_minutes: 0`
= no message. **Kill switches:** `outage_rules_enabled` globally; the known kinds
`hypervisor`, `host`, `service`, `container` have their own switches; any other kind (the kinds
are open, decision A) is silent if it is listed in `outage_rules_disabled_kinds` — otherwise
the rule applies. The kind comes from the published item, otherwise a `server` monitor → `host`, everything
else → `service`.

### Manual windows and overrides (UI/API)

In the React UI "Maintenance" (detail card of a host or toolbar with host selection) opens the
dialog: **Override now** (30/60/120/240 min or free, max. `override_max_minutes`, reason) or
**Window** (weekly with days/start/end/time zone, or one-time). Overrides apply to the
monitors of the host and the `ci_id`s resolved from them; windows are stored per monitor
(a second entry for the same target appends the windows). The panel "Active maintenance overrides"
shows own and foreign overrides; own ones can be ended (linger
`override_linger_minutes`). Every change is emitted **locally** as `maintenance:windows_published`
(`source: "monitoring"`) respectively `override_started`/`override_ended`, so that the Entity
Registry and Server Manager see them; the plugin's own handlers ignore this source.

---

## Database schema

All models use `extend_existing=True` to avoid `InvalidRequestError` on plugin reload.

| Table | Description |
|---|---|
| `monitoring_monitors` | Monitor register (ID, name, type, group, last status, uptime windows 24h/7d/30d, `ci_id` (index) and `host_ci_id`) |
| `monitoring_heartbeats` | Raw probe results (timestamp, status, latency, source); retention `heartbeat_retention_days`; indexes `monitor_id`, `created_at`, `(monitor_id, created_at)` |
| `monitoring_daily_aggregates` | Daily aggregates per monitor (samples total/up/down/paused/maintenance, latency mean, `latency_samples`) — source for `uptime_all`/7d/30d; unique key `uq_monitoring_daily_aggregates_monitor_day` on `(monitor_id, day)` |
| `monitoring_maintenance_windows` | Maintenance windows per `(source, ci_id, monitor_id)` (unique), published or manual — see *Maintenance windows* |
| `monitoring_overrides` | Overrides per `override_id` |
| `monitoring_outages` | Outage clock per monitor (`open` → `persisted`, deleted on recovery) |
| `monitoring_outage_events` | Outbox for `outage_persisted`/`outage_recovered` (1.8.0), survives restarts |

Schema changes (index, new column) are applied idempotently in `MonitoringService._ensure_schema` (`CREATE INDEX IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`; 1.7.0:
`ci_id`, `host_ci_id`, `maintenance_samples`); new tables are created by `create_all` under the
bootstrap lock `lyndrix_bootstrap`.
`_ensure_daily_aggregate_unique` merges existing duplicates per `(monitor_id, day)`
(counters summed, latency mean weighted by `latency_samples`, row with the smallest `id` remains)
and then creates the unique index (1061 "Duplicate key name" is tolerated). On MariaDB
each probe writes its daily aggregate atomically via `INSERT … ON DUPLICATE KEY UPDATE`.

---

## Configuration

**`auto_enable_on_install=False`** — the plugin requires a running database connection and ideally a configured IaC Orchestrator as inventory source before it should be activated.

Plugin settings (core settings dialog, stored in Vault, applied after
`vault:ready_for_data`):

| Key | Default | Meaning |
|---|---|---|
| `heartbeat_retention_days` | `7` | Retention of raw heartbeats (1–365) |
| `initial_probe_workers` | `8` | parallel probes during the startup sequence (1–64) |
| `overview_cache_seconds` | `10` | Validity of a computed overview for concurrent viewers (0 = always recompute) |
| `probe_roles` | empty | cluster only: instance roles that may probe (comma-separated list, e.g. `runner`); empty = every instance — see *Cluster operation*. Effective immediately in distributed mode |
| `probe_nodes` | `1` | cluster only (core API ≥ 1.8): number of instances that share probing (1–64). `1` = single prober via lease (previous behaviour). Changes while > 1 take effect immediately; switching between `1` and > 1 only after a plugin reload/restart |
| `outage_rules_enabled` | `true` | category *Incidents*: global kill switch for `monitoring:outage_persisted` / `outage_recovered` |
| `outage_rules_enabled_hypervisor` / `_host` / `_service` / `_container` | `true` / `true` / `true` / `false` | switch per known kind |
| `outage_rules_disabled_kinds` | empty | comma-separated list of further kinds without their own switch that are silent (e.g. `network,generic`); unlisted unknown kinds are reported |
| `outage_default_offline_minutes` | `10` | minutes outside maintenance until reporting, if neither the CI nor the host has published a rule (`0` = default rule off) |
| `outage_default_severity` | `high` | `critical` / `high` / `medium` / `low` in the payload of the default rule |
| `outage_default_auto_resolve` | `true` | whether the consumer (ITSM) may resolve automatically on `outage_recovered` (default rule) |
| `override_default_minutes` | `60` | category *Maintenance windows*: preselection of the duration of a manual override |
| `override_max_minutes` | `240` | upper limit of a manual override (longer requests are capped) |
| `inventory_max_stale_percent` | `30` | category *Inventory* (1.8.0): max. share of IaC-managed monitors an import may remove; `0` = never without `allow_mass_delete=true`, `100` = only an empty inventory is rejected |
| `override_linger_minutes` | `15` | linger after the end of an own override before `DOWN` counts again |

The settings are applied when the plugin loads, after `vault:ready_for_data` and on
`plugin:settings_changed` (core ≥ 1.5.1) — including the outage and override values,
without reload.

### Maintenance actions

In the same settings dialog (category **Maintenance**, manifest fields with `kind: "action"`,
core API ≥ 1.6) cleanup jobs are available as buttons. Each calls a route of the plugin
(`POST /api/plugins/lyndrix.plugin.state_monitoring/maintenance/…`, permission
`api:write`) and shows the response (`message` + counters). Destructive actions ask for confirmation.

| Action | Route | Effect |
|---|---|---|
| Prune old heartbeats now | `/maintenance/prune` | runs the hourly retention job immediately (batches of 20 000) |
| Recalculate statistics | `/maintenance/rebuild-stats` | rebuild daily aggregates from the raw heartbeats + refresh uptime windows (repair for wrong counters) |
| Check all monitors now | `/maintenance/probe-all` | sweep over **all** active monitors (not only stale/unknown), non-blocking, progress in the banner — always runs on the prober instance (see *Cluster operation*) |
| Delete statistics | `/maintenance/reset-stats` | delete all heartbeats + daily aggregates, uptime to 100 % — **states remain** |
| Reset states and statistics | `/clear-states` | as above, additionally every monitor to `UNKNOWN` (next sweep is blocking) |
| Delete IaC monitors | `/maintenance/delete-monitors/managed` | delete all monitors created by the orchestrator (`owner_source` `orchestrator_service`/`iac_orchestrator`) including history; **the orchestrator recreates them on the next inventory publish** (orchestrator boot or pipeline run); manual monitors (`ui_admin`) remain |
| Delete ALL monitors | `/maintenance/delete-monitors/all` | everything gone, including manual monitors |

Before deleting, all probe writers are stopped cluster-wide (quiesce, see
*Cluster operation*). After deletion the scheduler jobs are resynchronized on the event loop and the
in-memory counters (flap dampening, host circuit breaker, view cache) are discarded. Heartbeats are
deleted in batches here too, so that no statement runs into the core's 60 s limit.

---

## Internationalization

The plugin registers the i18n namespace `monitoring`. Translation files under `locales/monitoring.<locale>.json` are automatically included in the Lyndrix catalog when loading. The React client obtains them via `GET /api/i18n/{locale}?ns=monitoring`.

---

## Development & tests

```bash
# From the plugin directory (lyndrix-plugin-monitoring/)
pip install -r requirements-dev.txt

# Run tests
pytest

# Type checking
mypy .

# Linter
ruff check .

# Check formatter
black --check .
```

The controller and model layers are testable without a running core. The helpers in `app/controller/helpers.py` are pure functions without database access and can be called directly with test data.

The tests need `core.api` on the `PYTHONPATH` — easiest in the dev container
(`tests/conftest.py` imports `core.services` before `core.api` to work around the known circular
import of the core and provides the plugin as package `monitoring_plugin`):

```bash
docker exec lyndrix-core-dev sh -c "rm -rf /tmp/t-mon && mkdir -p /tmp/t-mon"
tar --exclude node_modules --exclude .git --exclude vendor -cf - . | docker exec -i lyndrix-core-dev tar -C /tmp/t-mon -xf -
docker exec -w /tmp/t-mon -e PYTHONPATH=/app:/tmp/pytest-sm lyndrix-core-dev python -m pytest -q tests
```

`tests/test_perf_and_status.py` covers the performance model (aggregates, SQL buckets, batch prune,
single-flight cache) and the startup sequence (status messages, stop flag) against in-memory SQLite.
The maintenance windows are tested by `test_maintenance_windows.py` (bus handlers, store, cache, manual
windows/overrides), `test_store_heartbeat_maintenance.py` (state `MAINTENANCE`, aggregates,
outage clock), `test_outage_rule.py` (evaluator with frozen clock, rule cascade, kill switches,
exactly-once, recovery) and `test_inventory_ci_id.py` (`ci_id` in the inventory import and in the
config hash). `tests/test_maintenance.py` covers the plugin upkeep (settings buttons).
