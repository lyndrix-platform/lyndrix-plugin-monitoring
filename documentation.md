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

## Cluster-Betrieb (Core-API ≥ 1.7)

Laufen mehrere Core-Instanzen hinter einem Load Balancer (`LYNDRIX_CLUSTER_MODE=cluster`, siehe
Core-Doku *High Availability*), gilt für dieses Plugin:

- **Jede Instanz bedient die API** (`/overview`, `/dashboard`, Wartungsaktionen …) aus der
  gemeinsamen Datenbank. Das Plugin hat `placement: any`.
- **Genau eine Instanz probt.** Nach dem Laden der Einstellungen meldet das Plugin
  `ctx.on_lease("monitoring:prober", acquired=start_probing, lost=stop_probing)`. Nur der Lease-Halter startet Scheduler, Startup-Sweep
  und Housekeeping (Prune, Rollups, Uptime-Fenster). Stirbt er, übernimmt eine andere Instanz
  innerhalb der Lease-TTL (Standard 30 s). Im Single-Modus wird die Lease sofort gewährt — ein
  Code-Pfad für beide Betriebsarten. Auf einem älteren Core ohne `on_lease` startet das Plugin
  wie bisher direkt.
- **Standby-Instanzen** melden in `health()` `prober: false` und keinen Scheduler-Fehler; sie
  legen nur die Tabellen an (unter dem Core-Bootstrap-Lock, damit N Instanzen das DDL nicht
  gegeneinander fahren).
- **Änderungen am Monitor-Satz wandern als Cluster-Event.** Inventar-Import (kommt dort an, wo
  der Orchestrator läuft), Einzel-Upserts und die Wartungsaktionen senden
  `monitoring:monitors_changed` mit `cluster=True`; der Prober lädt daraufhin seine Jobs neu und
  probt die neuen Monitore.
- **Probe-Standort (`probe_roles`).** Ist die Einstellung gesetzt (Komma-Liste von Rollen aus
  `LYNDRIX_INSTANCE_ROLES`, z. B. `runner`), bewirbt sich nur eine Instanz mit einer dieser Rollen
  um die Prober-Lease — so lässt sich das Probing auf Docker-Hosts mit ICMP/`NET_RAW` und
  passendem Netzzugang festlegen. Leer (Standard) = jede Instanz. Trägt keine lebende Instanz eine
  der Rollen, probt niemand (Warnung im Log, `health()` zeigt `probe_eligible: false`). Änderungen
  wirken sofort (`plugin:settings_changed`): eine **Erweiterung** lässt die Instanz um die Lease
  bewerben, eine **Einschränkung** gibt eine gehaltene Lease per `ctx.release_lease` (Core-API
  ≥ 1.9) sofort ab — `stop_probing` läuft, eine berechtigte Instanz übernimmt beim nächsten
  Heartbeat. Auf älteren Cores greift eine Einschränkung erst nach Plugin-Reload/Neustart.
- **Verteiltes Probing (`probe_nodes` > 1, Core-API ≥ 1.8).** Statt einer Lease teilen sich
  bis zu `probe_nodes` Instanzen (gefiltert nach `probe_roles`) die Monitore per
  Rendezvous-Hashing (`ctx.my_shard("probe", monitor_ids, count=probe_nodes, roles=…)`): jede
  Instanz berechnet lokal dieselbe Aufteilung, jeder Monitor hat genau einen Besitzer. Neu
  berechnet wird bei `cluster:changed` (Mitgliedschaft) und bei jeder Änderung des Monitor-Satzes
  (`monitoring:monitors_changed`, Inventar-Import, Upsert); dabei werden nur Jobs für neu
  übernommene Monitore gestartet und für abgegebene gestoppt — laufende Jobs bleiben unangetastet.
  Verlässt eine Instanz den Cluster, wandern nur ihre Monitore. Während sich die Sicht auf die
  Mitglieder für wenige Sekunden unterscheidet, kann ein Monitor doppelt geprobt werden
  (at-least-once) — für Probes unkritisch. Globale Aufgaben (Prune, Rollups, Uptime-Fenster)
  übernimmt genau ein Worker (`ctx.is_worker("housekeeping")`). „Alle Monitore jetzt prüfen“
  lässt jeden Worker seinen eigenen Anteil prüfen, Quiesce pausiert alle Worker. Ohne
  `ctx.my_shard` (älterer Core) fällt das Plugin mit Warnung auf den einzelnen Prober zurück.
  `health()` meldet `probe_mode` (`lease` | `sharded`), `probe_nodes` und `shard_size`.
- **Einstellungen gelten clusterweit.** Speichert der Operator die Plugin-Einstellungen, sendet
  der Core (≥ 1.5.1) das gebridgte Event `plugin:settings_changed`; jede Instanz liest die Werte
  dann neu aus Vault.
- **„Alle Monitore jetzt prüfen“ auf einem Standby** setzt nichts lokal, sondern sendet
  `monitoring:monitors_changed` mit `{"reason": "probe_all"}` (`cluster=True`); der Prober startet
  beim Empfang den vollständigen Sweep. Die Antwort enthält `prober_here`.
- **Destruktive Wartung stoppt den Prober clusterweit (Quiesce).** Löschen von Statistiken,
  Zuständen oder Monitoren läuft auf der Instanz, die den Request erhält. Vorher sendet sie
  `monitoring:quiesce` `{"on": true, "ttl": 120}` (`cluster=True`); der Prober überspringt
  daraufhin alle Probes. Ist die ausführende Instanz selbst nicht Prober, wartet sie zusätzlich
  3 s (Bus-Relay + Abarbeitung laufender Probes). Danach `{"on": false}` und
  `monitoring:monitors_changed` (`maintenance`) → der Prober lädt seine Jobs neu. Geht das
  „off“-Event verloren, endet der Quiesce nach der TTL von selbst. Die Löschungen laufen
  zusätzlich in READ-COMMITTED-Batches mit Retry auf 1020/1205/1213, ein verspäteter
  Probe-Schreibzugriff bricht sie also nicht ab. Das blockierende Wartungsfenster (503) gilt nur
  auf der ausführenden Instanz. Gewählt wurde das Event statt `ctx.lease("monitoring:prober")`,
  weil die One-Shot-Lease auf einem Standby nie gewährt würde (der Prober hält sie dauerhaft).
- **Inventar-Import per REST** (`POST /inventory-sync`) synchronisiert danach ebenfalls die Jobs,
  stößt den Sweep an und meldet die Änderung clusterweit — wie der Bus-Pfad.
- **Hintergrundjobs überleben Fehler.** Wirft ein Scheduler-Lauf (z. B. DB-Fehler), wird das
  geloggt und der Job läuft weiter; nach wiederholten Fehlern mit kurzer zusätzlicher Pause
  (2, 4, 8 … max. 60 s). Rollups und Uptime-Fenster schreiben in Häppchen à 200 Monitore
  (READ COMMITTED, Retry).
- **Verlorene Schreibvorgänge sind kein Fehler.** Schlägt das Speichern eines Probe-Ergebnisses
  mit MariaDB 1020/1205/1213 fehl (Wartung auf einer anderen Instanz, Lease-Übergabe), wird die
  Stichprobe verworfen und der nächste Intervall-Lauf probt erneut — der Scheduler-Job lebt weiter.

---

## Events

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `db:connected` | Datenbank bereit — Tabellen initialisieren |
| subscribe | `vault:ready_for_data` | Plugin-Einstellungen (Retention, Worker, Cache, `probe_roles`) aus Vault anwenden |
| subscribe | `cluster:changed` | Core ≥ 1.6: Mitgliedschaft geändert → im verteilten Modus den eigenen Anteil neu berechnen |
| subscribe | `plugin:settings_changed` | Core ≥ 1.5.1, gebridgt: Operator hat die Einstellungen gespeichert → bei passender `plugin_id` neu laden |
| subscribe | `monitoring:inventory_sync` | Inventar-Sync vom IaC Orchestrator empfangen |
| subscribe / emit | `monitoring:monitors_changed` | Monitor-Satz geändert (Inventar, Upsert, Admin-Override, Wartung) — mit `cluster=True` an alle Instanzen; der Prober lädt seine Jobs neu, bei `reason: probe_all` startet er einen vollständigen Sweep |
| subscribe / emit | `monitoring:quiesce` | `{"on": bool, "ttl"}` mit `cluster=True` — destruktive Wartung auf einer anderen Instanz, der Prober pausiert alle Probes |
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
| `monitoring_daily_aggregates` | Tagesaggregate je Monitor (Samples gesamt/up/down/paused, Latenz-Mittel, `latency_samples`) — Quelle für `uptime_all`/7d/30d; eindeutiger Schlüssel `uq_monitoring_daily_aggregates_monitor_day` auf `(monitor_id, day)` |

Schemaänderungen (Index, neue Spalte) werden in `MonitoringService._ensure_schema` idempotent
nachgezogen (`CREATE INDEX IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`).
`_ensure_daily_aggregate_unique` führt vorhandene Duplikate je `(monitor_id, day)` zusammen
(Zähler summiert, Latenz-Mittel nach `latency_samples` gewichtet, Zeile mit kleinster `id` bleibt)
und legt dann den eindeutigen Index an (1061 „Duplicate key name“ wird toleriert). Auf MariaDB
schreibt jede Probe ihr Tagesaggregat atomar per `INSERT … ON DUPLICATE KEY UPDATE`.

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
| `probe_roles` | leer | nur Cluster: Instanz-Rollen, die proben dürfen (Komma-Liste, z. B. `runner`); leer = jede Instanz — siehe *Cluster-Betrieb*. Im verteilten Modus sofort wirksam |
| `probe_nodes` | `1` | nur Cluster (Core-API ≥ 1.8): Anzahl Instanzen, die sich das Probing teilen (1–64). `1` = einzelner Prober per Lease (bisheriges Verhalten). Änderungen innerhalb von > 1 wirken sofort; der Wechsel zwischen `1` und > 1 erst nach Plugin-Reload/Neustart |

Die Einstellungen werden beim Laden des Plugins, nach `vault:ready_for_data` und bei
`plugin:settings_changed` (Core ≥ 1.5.1) angewendet.

### Wartungsaktionen

Im selben Settings-Dialog (Kategorie **Maintenance**, Manifest-Felder mit `kind: "action"`,
Core-API ≥ 1.6) stehen Aufräum-Jobs als Buttons. Jeder ruft eine schreibgeschützte Route des
Plugins (`POST /api/plugins/lyndrix.plugin.state_monitoring/maintenance/…`, Berechtigung
`api:write`) und zeigt die Antwort (`message` + Zähler) an. Destruktive Aktionen fragen nach.

| Aktion | Route | Wirkung |
|---|---|---|
| Alte Heartbeats jetzt bereinigen | `/maintenance/prune` | führt den stündlichen Retention-Job sofort aus (Batches à 20 000) |
| Statistiken neu berechnen | `/maintenance/rebuild-stats` | Tagesaggregate aus den Roh-Heartbeats neu aufbauen + Uptime-Fenster auffrischen (Reparatur bei falschen Zählern) |
| Alle Monitore jetzt prüfen | `/maintenance/probe-all` | Sweep über **alle** aktiven Monitore (nicht nur stale/unbekannt), nicht blockierend, Fortschritt im Banner — läuft immer auf der Prober-Instanz (siehe *Cluster-Betrieb*) |
| Statistiken löschen | `/maintenance/reset-stats` | alle Heartbeats + Tagesaggregate löschen, Uptime auf 100 % — **Zustände bleiben** |
| Zustände und Statistiken zurücksetzen | `/clear-states` | wie oben, zusätzlich jeder Monitor auf `UNKNOWN` (nächster Sweep ist blockierend) |
| IaC-Monitore löschen | `/maintenance/delete-monitors/managed` | alle vom Orchestrator erzeugten Monitore (`owner_source` `orchestrator_service`/`iac_orchestrator`) samt Historie löschen; **der Orchestrator legt sie beim nächsten Inventar-Publish neu an** (Boot des Orchestrators oder Pipeline-Lauf); manuelle Monitore (`ui_admin`) bleiben |
| ALLE Monitore löschen | `/maintenance/delete-monitors/all` | alles weg, inklusive manueller Monitore |

Vor dem Löschen werden alle Probe-Schreiber clusterweit angehalten (Quiesce, siehe
*Cluster-Betrieb*). Nach dem Löschen werden die Scheduler-Jobs auf dem Event-Loop neu synchronisiert und die
In-Memory-Zähler (Flap-Dämpfung, Host-Circuit-Breaker, View-Cache) verworfen. Heartbeats werden
auch hier in Batches gelöscht, damit kein Statement in die 60-s-Grenze des Cores läuft.

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
