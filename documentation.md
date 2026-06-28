# State Monitoring — Dokumentation

## Übersicht

Das State-Monitoring-Plugin ist das native Infrastruktur- und Service-Monitoring für Lyndrix. Es pflegt ein Monitor-Register mit persistenter Statushistorie, unterstützt gruppierte Übersichten (nach Site, Umgebung etc.), Timeline-Visualisierungen und Admin-Overrides. Inventar-Synchronisation mit dem IaC Orchestrator erfolgt über das Event `monitoring:inventory_sync` — der IaC Orchestrator ist die primäre Quelle für Infrastruktur-Monitore.

---

## Architektur

```
lyndrix-plugin-monitoring/
├── entrypoint.py               # Manifest + Lifecycle-Hooks
├── locales/
│   └── monitoring.<locale>.json  # i18n-Übersetzungen (Namespace: monitoring)
└── app/
    ├── controller/
    │   ├── api.py              # FastAPI-Router (auth + permission-gated)
    │   ├── service.py          # MonitoringService: Register, Historie, Stats
    │   ├── helpers.py          # Reine Geschäftslogik: build_grouped_overview, flatten_monitors, …
    │   ├── scheduler.py        # Passive-Check-Scheduler
    │   └── provider/           # Aktive Check-Provider
    ├── model/
    │   └── models.py           # SQLAlchemy: Monitor, MonitorHistory, AdminOverride
    └── ui/
        ├── nicegui/            # NiceGUI-UI: overview.py, timeline.py, cards.py, split.py, table.py, styles.py
        │   └── helpers.py      # Re-Export-Shim → controller/helpers.py (Abwärtskompatibilität)
        └── react/              # React-UI (kanonisches Migrationsziel)
```

**Wichtiger Architekturhinweis:** Die Geschäftslogik-Helfer (`build_grouped_overview`, `flatten_monitors`, `infer_site_and_stage`, `humanize_label` usw.) liegen in `app/controller/helpers.py`. Die Datei `app/ui/nicegui/helpers.py` ist ein reiner Re-Export-Shim für bestehende NiceGUI-Views — neue Aufrufer importieren direkt aus `controller/helpers.py`. Dies entspricht der kanonischen Anatomie-Regel: keine Geschäftslogik in der UI-Schicht.

---

## API-Referenz

Alle Routen sind unter `/api/plugins/lyndrix.plugin.state_monitoring/` erreichbar. Sie erfordern eine gültige Authentifizierung sowie die passenden Berechtigungen.

| Methode | Pfad | Permission | Beschreibung |
|---|---|---|---|
| `GET` | `/dashboard` | `api:read` | Monitor-Liste + Statistiken |
| `GET` | `/overview` | `api:read` | Gruppierte + gefilterte Übersicht für den React-Client |
| `GET` | `/monitors/{monitor_id}` | `api:read` | Einzelner Monitor |
| `GET` | `/history/{monitor_id}` | `api:read` | Statushistorie |
| `POST` | `/monitors` | `api:write` | Monitor anlegen oder aktualisieren (Upsert) |
| `DELETE` | `/monitors/{monitor_id}` | `api:write` | Monitor entfernen |
| `POST` | `/passive` | `api:write` | Passives Check-Ergebnis empfangen |
| `POST` | `/inventory-sync` | `api:write` | Bulk-Upsert aus dem IaC Orchestrator |
| `PUT` | `/admin-override/{monitor_id}` | `api:write` | Admin-Statusüberschreibung setzen |

### `/overview`-Endpunkt

```
GET /overview?group_by=site&hours=24&include_paused=true&include_unknown=true
```

Liefert dieselben gruppierten und gefilterten Daten, die auch das NiceGUI-Dashboard anzeigt — die Geschäftslogik ist identisch, weil der React-Client `controller/helpers.py` über diesen Endpunkt nutzt, nicht über eigene Frontend-Logik.

---

## Events

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `db:connected` | Datenbank bereit — Tabellen initialisieren |
| subscribe | `monitoring:inventory_sync` | Inventar-Sync vom IaC Orchestrator empfangen |
| emit | `monitoring:status_changed` | Statusänderung eines Monitors |
| emit | `system:notify` | Plattform-Benachrichtigung bei Statusänderungen |

### Inventar-Synchronisation

Der IaC Orchestrator emittiert `monitoring:inventory_sync` mit einer Liste von Hosts und Services. Das Monitoring-Plugin führt daraufhin einen Bulk-Upsert aus und aktualisiert alle bekannten Monitore. Hosts, die aus dem Inventar verschwunden sind, werden auf `unknown` gesetzt, nicht gelöscht.

---

## Datenbankschema

Alle Modelle verwenden `extend_existing=True`, um `InvalidRequestError` beim Plugin-Reload zu vermeiden.

| Tabelle | Beschreibung |
|---|---|
| `monitoring_monitors` | Monitor-Register (ID, Name, Typ, Site, Stage, Status) |
| `monitoring_history` | Statushistorie pro Monitor (Zeitstempel, Status, Nachricht) |
| `monitoring_admin_overrides` | Admin-Statusüberschreibungen (bis wann, Begründung) |

---

## Konfiguration

**`auto_enable_on_install=False`** — das Plugin benötigt eine laufende Datenbankverbindung und idealerweise einen konfigurierten IaC Orchestrator als Inventarquelle, bevor es aktiviert werden sollte.

---

## Internationaliserung

Das Plugin registriert den i18n-Namespace `monitoring`. Übersetzungsdateien unter `locales/monitoring.<locale>.json` werden automatisch beim Laden in den Lyndrix-Katalog aufgenommen. Der React-Client bezieht sie über `GET /api/i18n/{locale}?ns=monitoring`.

---

## Entwicklung & Tests

```bash
# Aus dem Plugin-Verzeichnis (lyndrix-plugin-monitoring/)
pip install -r requirements-dev.txt

# Tests ausführen
pytest

# Typprüfung
mypy .

# Linter
ruff check .

# Formatter prüfen
black --check .
```

Die Controller- und Model-Schicht sind ohne laufenden Core testbar. Die Helfer in `app/controller/helpers.py` sind reine Funktionen ohne Datenbankzugriff und können direkt mit Testdaten aufgerufen werden.
