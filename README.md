# lyndrix-plugin-monitoring

Native infrastructure and service monitoring plugin for the Lyndrix platform.

Provides persistent, grouped status timelines for servers and services, with
optional IaC inventory sync, passive result ingestion, and an admin override API.

## Features

- Active HTTP, ICMP, TCP, and Docker probes
- DNS, MariaDB, and PostgreSQL health checks via optional providers
- Grouped overview UI with 24-hour status timelines
- Passive result ingestion via the event bus
- IaC inventory sync from `lyndrix-plugin-iac-orchestrator`
- Dashboard widget and settings UI

## Project structure

```
.
├── entrypoint.py             # Manifest + lifecycle hooks (pure wiring layer)
├── requirements.txt          # Runtime dependencies
├── requirements-dev.txt      # Development toolchain
├── CHANGELOG.md
├── README.md
├── .gitignore
├── app/
│   ├── __init__.py
│   ├── model/
│   │   ├── __init__.py
│   │   └── models.py         # SQLAlchemy models, Pydantic schemas, DB helpers
│   ├── controller/
│   │   ├── __init__.py
│   │   ├── service.py        # MonitoringService — single shared service object
│   │   ├── scheduler.py      # SimpleAsyncScheduler
│   │   ├── api.py            # FastAPI router (build_router, register_api_routes)
│   │   └── provider/         # Probe implementations (http, icmp, tcp, docker, …)
│   └── ui/
│       ├── __init__.py
│       ├── page.py           # render_monitoring_page() — full /monitoring page body
│       ├── overview.py       # render_overview_ui()
│       ├── settings.py       # render_settings_ui()
│       ├── widget.py         # render_dashboard_widget()
│       ├── timeline.py       # timeline_html(), timeline_scale_html()
│       ├── helpers.py        # build_grouped_overview() and layout helpers
│       └── styles.py         # State badge/card/strip CSS helpers
└── tests/
    ├── __init__.py
    └── test_service.py       # Smoke tests
```

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/
```
