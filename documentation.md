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
| `GET` | `/dashboard` | `api:read` | Monitor-Liste + Statistiken (gecacht, s. Performance-Modell) |
| `GET` | `/overview` | `api:read` | Gruppierte + gefilterte Übersicht für den React-Client (gecacht) |
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

Liefert dieselben gruppierten und gefilterten Daten, die auch das NiceGUI-Dashboard anzeigt — die Geschäftslogik ist identisch, weil der React-Client `controller/helpers.py` über diesen Endpunkt nutzt, nicht über eigene Frontend-Logik. Die Antwort enthält `generated_at`; `hours` ist auf 1–168 begrenzt.

Während der Startsequenz (s. u.) beantwortet der **Core** jede Route des Plugins mit `503` und
`Retry-After` — der Client muss das nicht selbst abfangen, der React-Client zeigt den Fortschritt an
und fragt kurz darauf erneut.

---

## Performance-Modell

Ausgangslage (2026-10): `/overview` brauchte bei 200 Monitoren und 1,7 Mio. Heartbeats über
200 s, weil jede Leseoperation die **gesamte** Heartbeat-Tabelle aggregierte (`uptime_all`),
der React-Client alle 20 s abfragte, nach 15 s aufgab (die Server-Abfrage lief weiter) und der
Core `health()` bei jedem Dashboard-Aufruf dieselbe Aggregation anstieß. Seit v1.2.0 gilt:

| Lesepfad | Woher die Daten kommen |
|---|---|
| `uptime_all` (Liste, Stats) | rollierend 30 Tage aus `monitoring_daily_aggregates` — eine kleine Abfrage über ≤ 30 × Monitore Zeilen |
| `uptime_24h` / `7d` / `30d` | stündlicher Job (`monitoring:uptime_hourly`) in einem Durchlauf für alle Monitore, nicht mehr pro Heartbeat |
| Timeline (24 h-Balken) | von der Datenbank in Stunden-Buckets gezählt (`_hourly_state_counts`, ≤ 24 × Monitore Zeilen) statt alle Heartbeats des Tages als ORM-Objekte zu laden; Ergebnis identisch zu `timeline_from_history` |
| `/overview`, `/dashboard` | Single-Flight-Cache: gleichzeitige Aufrufer teilen sich eine Berechnung, Ergebnis gilt `overview_cache_seconds` (Standard 10 s); ein Statuswechsel invalidiert |
| `health()` | nur `status_counts()` (ein `GROUP BY` auf der Monitor-Tabelle) |

Schreibpfad: pro Heartbeat wird der Tagesaggregat-Datensatz **inkrementell** fortgeschrieben
(Zähler + laufender Latenz-Mittelwert über `latency_samples`); der tägliche Rollup-Job rechnet
ihn aus den Rohdaten nach. Der Index `(monitor_id, created_at)` macht die 24-h-Fenster zu
Index-Range-Scans.

**Retention:** Heartbeats älter als `heartbeat_retention_days` (Standard 7) werden gelöscht —
eine Minute nach der Startsequenz, stündlich und täglich, immer in Batches von 20 000 Zeilen
(jede Anweisung bleibt weit unter dem 60-s-Statement-Limit des Cores). Der frühere reine
Mitternachts-Job lief nie, wenn der Prozess vorher neu gestartet wurde.

Zum Messen: `curl -w %{time_total}` gegen `/overview` und `SHOW PROCESSLIST` auf der Datenbank —
es dürfen keine langlaufenden `monitoring_heartbeats`-Abfragen mehr auftauchen.

---

## Startsequenz und Wartungsfenster

Nach dem Start (und nach einem Reload) prüft das Plugin alle Monitore, die unbekannt, down oder
veraltet sind, mit `initial_probe_workers` parallelen Probes. Dabei meldet es dem Core
(API ≥ 1.5) `ctx.set_status("starting", progress=(done, total), blocking=True)`:

- der Core beantwortet alle Plugin-Routen mit `503 Retry-After`,
- die React-Shell zeigt statt der Plugin-UI eine Warm-up-Ansicht mit Fortschrittsbalken und
  schaltet von selbst um, sobald `ready` gemeldet wird (SSE `plugin:runtime_status`),
- `/api/health` fragt `health()` währenddessen nicht ab.

Blockierend ist das Fenster nur, wenn **unbekannte oder veraltete** Monitore dabei sind — das erneute Prüfen eines Satzes dauerhaft ausgefallener Monitore nach jedem Neustart läuft als nicht blockierende Startmeldung. Bei einem Warmstart ohne veraltete Monitore gibt es kein Fenster. Inventar-Import und
„Zustände löschen“ laufen als **nicht blockierendes** Wartungsfenster (Banner in der Shell).
`stop()` setzt ein Stop-Flag und wartet bis zu 5 s auf laufende Probes, damit ein Reload keine
zweite Sweep-Runde gegen die Datenbank startet. Auf einem älteren Core ohne `ctx.set_status`
läuft alles unverändert, nur ohne Anzeige.

---

## Events

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `db:connected` | Datenbank bereit — Tabellen initialisieren |
| subscribe | `vault:ready_for_data` | Plugin-Einstellungen (Retention, Worker, Cache) aus Vault anwenden |
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
| `monitoring_monitors` | Monitor-Register (ID, Name, Typ, Gruppe, letzter Status, Uptime-Fenster 24h/7d/30d) |
| `monitoring_heartbeats` | Rohe Probe-Ergebnisse (Zeitstempel, Status, Latenz, Quelle); Retention `heartbeat_retention_days`; Indizes `monitor_id`, `created_at`, `(monitor_id, created_at)` |
| `monitoring_daily_aggregates` | Tagesaggregate je Monitor (Samples gesamt/up/down/paused, Latenz-Mittel, `latency_samples`) — Quelle für `uptime_all`/7d/30d |

Schemaänderungen (Index, neue Spalte) werden in `MonitoringService._ensure_schema` idempotent
nachgezogen (`CREATE INDEX IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`).

---

## Konfiguration

**`auto_enable_on_install=False`** — das Plugin benötigt eine laufende Datenbankverbindung und idealerweise einen konfigurierten IaC Orchestrator als Inventarquelle, bevor es aktiviert werden sollte.

Plugin-Einstellungen (Settings-Dialog des Cores, in Vault gespeichert, angewendet nach
`vault:ready_for_data`):

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `heartbeat_retention_days` | `7` | Aufbewahrung roher Heartbeats (1–365) |
| `initial_probe_workers` | `8` | parallele Probes während der Startsequenz (1–64) |
| `overview_cache_seconds` | `10` | Gültigkeit einer berechneten Übersicht für gleichzeitige Betrachter (0 = immer neu) |

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

Die Tests brauchen `core.api` auf dem `PYTHONPATH` — am einfachsten im Dev-Container
(`tests/conftest.py` importiert `core.services` vor `core.api`, um den bekannten Zirkelimport
des Cores zu umgehen, und stellt das Plugin als Paket `monitoring_plugin` bereit):

```bash
docker exec lyndrix-core-dev sh -c "rm -rf /tmp/t-mon && mkdir -p /tmp/t-mon"
tar --exclude node_modules --exclude .git --exclude vendor -cf - . | docker exec -i lyndrix-core-dev tar -C /tmp/t-mon -xf -
docker exec -w /tmp/t-mon -e PYTHONPATH=/app:/tmp/pytest-sm lyndrix-core-dev python -m pytest -q tests
```

`tests/test_perf_and_status.py` deckt das Performance-Modell (Aggregate, SQL-Buckets, Batch-Prune,
Single-Flight-Cache) und die Startsequenz (Statusmeldungen, Stop-Flag) gegen In-Memory-SQLite ab.
