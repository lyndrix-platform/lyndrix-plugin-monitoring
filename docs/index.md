# Lyndrix State Monitoring

Native infrastructure and service monitoring with persistent, grouped status timelines for servers and services.

- **Repository:** [https://github.com/lyndrix-platform/lyndrix-plugin-monitoring](https://github.com/lyndrix-platform/lyndrix-plugin-monitoring)
- **Platform docs:** [Lyndrix Core](https://docs.lyndrix.eu) · [Plugin ecosystem](https://docs.lyndrix.eu/ecosystem/)

This plugin builds on the Lyndrix Core [notification router](https://docs.lyndrix.eu/core-components/notification-router/) extension point.

## Features

- Infrastructure and service state monitoring
- Passive monitoring-result ingestion with admin override
- Inventory sync from other plugins
- Modular UI with toolbar and preferences

## Installation

Install **State Monitoring** from the Lyndrix **Plugin Manager**, or declare it for
reconciliation on boot via `LYNDRIX_PLUGINS_DESIRED`:

```text
https://github.com/lyndrix-platform/lyndrix-plugin-monitoring
```

See the [Plugin Development Guide](https://docs.lyndrix.eu/plugins/) for the plugin model and
lifecycle, and [Usage](usage.md) / [Configuration](configuration.md) for details.
