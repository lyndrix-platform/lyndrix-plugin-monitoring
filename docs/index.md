# Lyndrix State Monitoring

Native infrastructure and service monitoring with persistent, grouped status timelines for servers and services.

- **Repository:** [https://github.com/lyndrix-platform/lyndrix-plugin-monitoring](https://github.com/lyndrix-platform/lyndrix-plugin-monitoring)
- **Platform docs:** [Lyndrix Core](https://docs.lyndrix.eu) · [Plugin ecosystem](https://docs.lyndrix.eu/ecosystem/)

The plugin has no notification path of its own: state changes are published on the event bus
(`monitoring:state_changed`, `monitoring:outage_persisted` / `outage_recovered`) and consumed by
other plugins (e.g. ITSM). It speaks the shared maintenance-window contract of
`core.api.maintenance` (core API ≥ 1.13).

## Features

- Infrastructure and service state monitoring
- Passive monitoring-result ingestion with admin override
- Inventory sync from the IaC orchestrator (`POST /inventory-sync`, incl. `ci_id`)
- Maintenance windows and overrides (consumed from the bus, manual ones in the UI), state `MAINTENANCE`
- Outage rule: `monitoring:outage_persisted` after `offline_minutes` outside maintenance
- Modular UI with toolbar and preferences

## Installation

Install **State Monitoring** from the Lyndrix **Plugin Manager**, or declare it for
reconciliation on boot via `LYNDRIX_PLUGINS_DESIRED`:

```text
https://github.com/lyndrix-platform/lyndrix-plugin-monitoring
```

See the [Plugin Development Guide](https://docs.lyndrix.eu/plugins/) for the plugin model and
lifecycle, and [Usage](usage.md) / [Configuration](configuration.md) for details.
