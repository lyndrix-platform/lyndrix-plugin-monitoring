# State Monitoring — Dokumentation

## Übersicht

Das State-Monitoring-Plugin ist das native Infrastruktur- und Service-Monitoring für Lyndrix. Es pflegt ein Monitor-Register mit persistenter Statushistorie, unterstützt gruppierte Übersichten (nach Site, Umgebung etc.), Timeline-Visualisierungen und Admin-Overrides. Das Inventar kommt vom IaC Orchestrator per HTTP (`POST /inventory-sync`, mit `ci_id` je Host und Service) — er ist die primäre Quelle für Infrastruktur-Monitore. Seit 1.7.0 kennt das Plugin Wartungsfenster und Overrides (Zustand `MAINTENANCE`) und meldet Ausfälle außerhalb eines Fensters als `monitoring:outage_persisted` (siehe *Wartungsfenster, Overrides und Outage-Regel*).

Das Plugin hat **keinen eigenen Benachrichtigungspfad** (kein Discord, kein Notification-Router): Zustandswechsel und Ausfälle werden auf dem Event-Bus veröffentlicht und von anderen Plugins (z. B. ITSM) konsumiert.

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
    │   ├── maintenance.py      # Wartungsfenster, Overrides, Outage-Uhr (Store, Cache, Manager)
    │   ├── helpers.py          # Reine Geschäftslogik: build_grouped_overview, flatten_monitors, …
    │   ├── scheduler.py        # Passive-Check-Scheduler
    │   └── provider/           # Aktive Check-Provider
    ├── model/
    │   └── models.py           # SQLAlchemy: Monitor, Heartbeat, Tagesaggregat, Fenster, Override, Outage
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
| `GET` | `/monitors/{monitor_id}` | `api:read` | Einzelner Monitor (inkl. `ci_id`, `in_maintenance`, `maintenance_until`, `outage_since`) |
| `GET` | `/history/{monitor_id}` | `api:read` | Statushistorie |
| `POST` | `/monitors` | `api:write` | Monitor anlegen oder aktualisieren (Upsert; `ci_id`/`host_ci_id` sind Teil des Config-Hashes) |
| `POST` | `/passive` | `api:write` | Passives Check-Ergebnis empfangen (`MAINTENANCE` wird abgelehnt — der Zustand ist abgeleitet) |
| `POST` | `/inventory-sync` | `api:write` | Bulk-Upsert aus dem IaC Orchestrator (Hosts und Services, mit `ci_id`/`host_ci_id`) |
| `POST` | `/admin-override` | `api:write` | Admin-Override `pause` / `resume` / `update` |
| `POST` | `/clear-states` | `api:write` | Zustände und Statistiken zurücksetzen (destruktiv) |
| `GET` | `/windows?ci_id=&monitor_id=&source=` | `api:read` | Gespeicherte Wartungsfenster (publiziert + manuell) mit `effective` (`active`, `until`, `source`, `next_start`) |
| `POST` | `/windows` | `api:write` | Manuelles Fenster `{ci_id \| monitor_id, windows: [...], note}` — Eingabe- oder Normalform; Validierungsfehler → `400` mit Meldung je Fenster |
| `DELETE` | `/windows/{id}` | `api:write` | Nur manuelle Zeilen (`source == "monitoring"`), sonst `409` |
| `GET` | `/overrides?active=true` | `api:read` | Overrides mit `active`, `until_effective`, `own` plus `defaults` (Minuten-Vorgaben aus den Settings) |
| `POST` | `/overrides` | `api:write` | Manuelles Override `{ci_ids \| monitor_ids, minutes, reason}` → `override_id = "mon-<uuid>"`; `minutes` wird auf `override_max_minutes` begrenzt |
| `DELETE` | `/overrides/{id}` | `api:write` | Beendet ein eigenes Override jetzt (`ended_at`, Nachlauf `override_linger_minutes`); fremde → `409` |
| `GET` | `/outages` | `api:read` | Offene und gemeldete Ausfälle mit Regel, `effective_since`, `effective_duration_s`, `in_maintenance` |

Die Wartungsaktionen unter `/maintenance/*` (Prune, Rebuild, Delete …) sind die Plugin-Pflege (siehe *Wartungsaktionen*), nicht die Wartungsfenster.

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

Alle Topics stehen in `permissions.subscribe` bzw. `permissions.emit` des Manifests (der Core
lehnt nicht deklarierte Emits mit `PermissionError` ab).

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `db:connected` | Datenbank bereit — Tabellen initialisieren (unter dem Bootstrap-Lock), Wartungs-Cache laden |
| subscribe | `vault:ready_for_data` | Plugin-Einstellungen (Retention, Worker, Cache, `probe_roles`, Outage-Regel, Override-Grenzen) aus Vault anwenden |
| subscribe | `cluster:changed` | Core ≥ 1.6: Mitgliedschaft geändert → im verteilten Modus den eigenen Anteil neu berechnen |
| subscribe | `plugin:settings_changed` | Core ≥ 1.5.1, gebridgt: Operator hat die Einstellungen gespeichert → bei passender `plugin_id` neu laden |
| subscribe | `monitoring:config_upsert` | `MonitorUpsert` über den Bus (wie `POST /monitors`) |
| subscribe | `monitoring:passive_result` | `PassiveResult` über den Bus (wie `POST /passive`) |
| subscribe | `monitoring:admin_override` | `AdminOverride` über den Bus (wie `POST /admin-override`) |
| subscribe | `maintenance:windows_published` | Core-API ≥ 1.13: Fenster eines Publishers (Orchestrator, künftig weitere). `replaces: true` ersetzt alle Zeilen dieser `source` in einer Transaktion; Items ohne `ci_id`, mit ungültigem Kind oder ungültigem Fenster werden einzeln übersprungen. `source: "monitoring"` (eigene Fenster) wird ignoriert |
| subscribe | `maintenance:override_started` / `override_ended` | Override eines Publishers (laufender Deploy); Upsert je `override_id`, Ende mit `ended_at` + `linger_minutes`. Eigene (`source: "monitoring"`) werden ignoriert |
| subscribe / emit | `monitoring:monitors_changed` | Monitor-Satz geändert (Inventar, Upsert, Admin-Override, Wartung) — mit `cluster=True` an alle Instanzen; der Prober lädt seine Jobs neu, bei `reason: probe_all` startet er einen vollständigen Sweep; **`reason: maintenance_windows`** lädt nur den Wartungs-Cache neu (jede Instanz, kein Job-Resync) |
| subscribe / emit | `monitoring:quiesce` | `{"on": bool, "ttl"}` mit `cluster=True` — destruktive Wartung auf einer anderen Instanz, der Prober pausiert alle Probes |
| emit | `monitoring:state_changed` | Zustandswechsel eines Monitors `{monitor_id, previous_state, new_state, timestamp, error_message, ci_id, maintenance}` — lokal, vor dem Commit (nur Cache-/UI-Signal) |
| emit | `maintenance:windows_published` | Manuelle Fenster dieses Plugins `{source: "monitoring", published_at, replaces: true, items}` — **lokal**, nach jedem Anlegen/Löschen; nur Zeilen mit `ci_id` |
| emit | `maintenance:override_started` / `override_ended` | Manuelle Overrides aus der UI/API, `source: "monitoring"` — **lokal** |
| emit | `monitoring:outage_persisted` | Einmal je Ausfall, nach `offline_minutes` außerhalb jeder Wartung — **lokal**, nur von der probenden Instanz (Payload siehe unten) |
| emit | `monitoring:outage_recovered` | `{outage_id, ci_id, since, recovered_at, duration_s}` — **lokal**, nach dem Commit des Heartbeats, der den Monitor wieder `UP` setzt |

Transportregel (Vertrag `00-ergebnis-p0.md` §3): `maintenance:*` und `monitoring:outage_*` werden
**ohne** `cluster=True` emittiert — Plugins mit `placement: any` laufen auf jeder Instanz, der
lokale Konsument schreibt in die gemeinsame Datenbank, genau eine Instanz verarbeitet jedes Event.
Peers erfahren von geänderten Fenstern über das eigene `monitoring:monitors_changed`
(`reason: maintenance_windows`). Die Handler sind trotzdem idempotent (Unique-Key je
`(source, ci_id, monitor_id)`, Upsert je `override_id`), damit ein späterer Wechsel auf
`cluster=True` nichts kaputt macht.

### Inventar-Synchronisation

Der IaC Orchestrator **postet** sein Inventar mit dem System-API-Key an `POST /inventory-sync`
(beim Boot und nach jeder `NativeGenerateStage`), nicht über den Bus. Je Host entsteht
`host:<name>` (Typ `server`), je Service `service:<host>:<name>` (`http` bei URL-Ziel, sonst
`service`). Seit Orchestrator 1.6.0 tragen Hosts `ci_id` und Services `ci_id` + `host_ci_id`
(Entity-Registry-UUIDs); sie landen am Monitor und sind Teil des Config-Hashes. Monitore dieser
Quelle, die im Payload fehlen, werden samt Historie, Outage-Uhr und daran gepinnten manuellen
Fenstern gelöscht. Ein Service-Monitor ohne eigene `ci_id` übernimmt für Fenster, Overrides und
Outage-Meldungen die `ci_id` seines Host-Monitors.

---

## Wartungsfenster, Overrides und Outage-Regel (1.7.0)

Grundlage ist `core.api.maintenance` (Core-API ≥ 1.13): Fensterformate `weekly` / `once` /
`cron + duration` (Normalform `{"type": …}`), DST-sichere Auswertung, Override-Semantik
(`until` + `linger_minutes`) und der Bus-Vertrag. Der Core hält **keine** Daten — das Plugin
speichert, was es empfängt, und wertet beim Lesen aus.

### Ablage und Cache

| Tabelle | Inhalt |
|---|---|
| `monitoring_maintenance_windows` | eine Zeile je `(source, ci_id, monitor_id)`: `kind`, `name`, `host_ci_id`, `windows_json` (Normalform), `inherited_from`, `incidents_json` (Regel des Publishers), `deploy_policy`, `note`. Publizierte Zeilen haben `ci_id`, manuelle (`source = monitoring`) `ci_id` und/oder `monitor_id` |
| `monitoring_overrides` | `override_id` (PK), `source`, `ci_ids_json`, `monitor_ids_json` (nur manuelle), `until`, `started_at`, `ended_at`, `linger_minutes`, `reason` |
| `monitoring_outages` | die Outage-Uhr je Monitor (siehe unten) |

Jede Instanz hält einen **In-Memory-Cache** (`MaintenanceCache`): Fenster je `ci_id` und je
`monitor_id`, aktive Overrides, Regeln je `ci_id`, Kind je `ci_id`, Zuordnung Host-Name →
Host-`ci_id`. Der Probe-Hot-Path liest nur diesen Cache (kein DB-Zugriff pro Heartbeat). Neu
geladen wird er nach jedem eigenen Schreibvorgang, bei `monitoring:monitors_changed`
(`reason: maintenance_windows`) eines Peers, nach Inventar-Import, beim Bootstrap und spätestens
alle 5 Minuten durch den Outage-Job. Zeiten liegen in den Tabellen naiv in UTC; der Controller
konvertiert.

### Zustand `MAINTENANCE`

Jeder Heartbeat (aktive Probe, passives Ergebnis) läuft durch `_store_heartbeat`: ist der
gemessene Zustand `DOWN` und für den Monitor ein Fenster oder Override aktiv
(`effective_state(...)` über Fenster/Overrides am `monitor_id` und an der effektiven `ci_id`),
wird `MAINTENANCE` gespeichert — **nur `DOWN` wird ersetzt**, eine erfolgreiche Probe bleibt `UP`,
`latest_error` bleibt zur Diagnose erhalten. Die Flap-Dämpfung (drei Fehlschläge) greift davor.
Folgen:

- Tagesaggregat: eigene Spalte `maintenance_samples`; der Uptime-Nenner ist
  `total − paused − maintenance` (Aggregate, Heartbeat-Fenster 24 h, Rollups).
- Rangfolge für Hosts und Gruppen: `DOWN > UNKNOWN > MAINTENANCE > UP > PAUSED`.
- Farbe `--lx-state-maintenance` (Core ≥ 1.11 / Shell ≥ 1.8), Fallback `--lx-state-paused`;
  Badge `lx-badge--maintenance`; im React-UI „Wartung · bis hh:mm“, eigener Zähler in der
  Kopfkarte, Widget und NiceGUI-Seite.
- Der Probe-Job läuft weiter (Recovery wird erkannt); `MAINTENANCE`-Monitore werden im Sweep wie
  `DOWN` behandelt. `PassiveResult` mit `MAINTENANCE` wird abgelehnt (Validierungsfehler).
- `GET /overview`, `/dashboard`, `/monitors/{id}` liefern je Monitor `ci_id`, `in_maintenance`,
  `maintenance_until`, `maintenance_source` (`window` | `override`), `outage_since`; Hosts
  zusätzlich `monitor_ids`, `ci_ids`, `in_maintenance`, `maintenance_until`.

### Outage-Uhr und Outage-Regel

`monitoring_outages` wird **in derselben Transaktion** wie der Heartbeat geschrieben (keine
Phantome):

- `DOWN`/`MAINTENANCE` ohne Zeile → `state = open`, `down_since = jetzt`, `suppressed_until =
  Fensterende` (falls Wartung aktiv). Jeder weitere Heartbeat in Wartung schreibt
  `suppressed_until = max(…, Fensterende)` fort; `down_since` bleibt.
- `UP` und `state = open` → Zeile gelöscht (nie gemeldet). `UP` und `state = persisted` →
  bedingtes `UPDATE … WHERE state='persisted'`, bei Treffer **nach dem Commit**
  `monitoring:outage_recovered` (`duration_s = recovered_at − down_since`), Zeile gelöscht.
- `UNKNOWN` ändert nichts; `pause`, `clear-states`, `delete-monitors` und der Inventar-Abgleich
  löschen die Zeilen mit.

Der Interval-Job `monitoring:outages` (60 s, nur auf der probenden Instanz; im Shard-Modus die
Besitzerin des Monitors, verwaiste Zeilen übernimmt der `housekeeping`-Worker) prüft jede
offene Zeile: läuft Wartung → `suppressed_until` fortschreiben. Sonst
`effektive Ausfallzeit = jetzt − max(down_since, suppressed_until)`; erreicht sie
`offline_minutes` der Regel, wird die Zeile per bedingtem `UPDATE … WHERE state='open'` auf
`persisted` gesetzt — `rowcount == 1` gewinnt, auch bei Lease-Wechsel oder zwei gleichzeitigen
Instanzen — und **einmal** emittiert:

```json
{"outage_id": "<ci_id>:<since ISO-UTC>", "ci_id": "…", "kind": "host", "name": "<Monitor-Name>",
 "host_ci_id": null, "monitor_id": "host:docker-hydra", "since": "…Z", "persisted_at": "…Z",
 "duration_s": 600, "rule": {"offline_minutes": 10, "severity": "high", "auto_resolve": true},
 "error": "connection refused"}
```

`outage_id` ist deterministisch (`make_outage_id`) und dient ITSM als `external_id`. Ohne
`ci_id` am Monitor (auch nicht über den Host-Monitor) gibt es **kein Event**, nur einen
Log-Eintrag je Monitor; die Zeile bleibt für `GET /outages` sichtbar.

**Regelkaskade** (`rule_for`): `incidents` des publizierten Items der Service-`ci_id` → der
Host-`ci_id` → Settings-Default (`outage_default_*`). `enabled: false` oder `offline_minutes: 0`
= keine Meldung. **Notschalter:** `outage_rules_enabled` global; die bekannten Kinds
`hypervisor`, `host`, `service`, `container` haben eigene Schalter; jeder andere Kind (die Kinds
sind offen, Entscheidung A) ist stumm, wenn er in `outage_rules_disabled_kinds` steht — sonst
gilt die Regel. Der Kind kommt aus dem publizierten Item, sonst `server`-Monitor → `host`, alles
andere → `service`.

### Manuelle Fenster und Overrides (UI/API)

Im React-UI öffnet „Wartung“ (Detailkarte eines Hosts bzw. Toolbar mit Host-Auswahl) den
Dialog: **Override jetzt** (30/60/120/240 min oder frei, max. `override_max_minutes`, Grund) oder
**Fenster** (wöchentlich mit Tagen/Beginn/Ende/Zeitzone, oder einmalig). Overrides gelten für die
Monitore des Hosts und die daraus aufgelösten `ci_id`s; Fenster werden je Monitor gespeichert
(zweiter Eintrag für dasselbe Ziel hängt die Fenster an). Das Panel „Aktive Wartungs-Overrides“
zeigt eigene und fremde Overrides; eigene lassen sich beenden (Nachlauf
`override_linger_minutes`). Jede Änderung wird **lokal** als `maintenance:windows_published`
(`source: "monitoring"`) bzw. `override_started`/`override_ended` emittiert, damit Entity
Registry und Server Manager sie sehen; die eigenen Handler ignorieren diese Quelle.

---

## Datenbankschema

Alle Modelle verwenden `extend_existing=True`, um `InvalidRequestError` beim Plugin-Reload zu vermeiden.

| Tabelle | Beschreibung |
|---|---|
| `monitoring_monitors` | Monitor-Register (ID, Name, Typ, Gruppe, letzter Status, Uptime-Fenster 24h/7d/30d, `ci_id` (Index) und `host_ci_id`) |
| `monitoring_heartbeats` | Rohe Probe-Ergebnisse (Zeitstempel, Status, Latenz, Quelle); Retention `heartbeat_retention_days`; Indizes `monitor_id`, `created_at`, `(monitor_id, created_at)` |
| `monitoring_daily_aggregates` | Tagesaggregate je Monitor (Samples gesamt/up/down/paused/maintenance, Latenz-Mittel, `latency_samples`) — Quelle für `uptime_all`/7d/30d; eindeutiger Schlüssel `uq_monitoring_daily_aggregates_monitor_day` auf `(monitor_id, day)` |
| `monitoring_maintenance_windows` | Wartungsfenster je `(source, ci_id, monitor_id)` (eindeutig), publiziert oder manuell — siehe *Wartungsfenster* |
| `monitoring_overrides` | Overrides je `override_id` |
| `monitoring_outages` | Outage-Uhr je Monitor (`open` → `persisted`, bei Recovery gelöscht) |

Schemaänderungen (Index, neue Spalte) werden in `MonitoringService._ensure_schema` idempotent
nachgezogen (`CREATE INDEX IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`; 1.7.0:
`ci_id`, `host_ci_id`, `maintenance_samples`); neue Tabellen legt `create_all` unter dem
Bootstrap-Lock `lyndrix_bootstrap` an.
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
| `outage_rules_enabled` | `true` | Kategorie *Incidents*: globaler Notschalter für `monitoring:outage_persisted` / `outage_recovered` |
| `outage_rules_enabled_hypervisor` / `_host` / `_service` / `_container` | `true` / `true` / `true` / `false` | Schalter je bekanntem Kind |
| `outage_rules_disabled_kinds` | leer | Komma-Liste weiterer Kinds ohne eigenen Schalter, die stumm sind (z. B. `network,generic`); nicht gelistete unbekannte Kinds werden gemeldet |
| `outage_default_offline_minutes` | `10` | Minuten außerhalb der Wartung bis zur Meldung, wenn weder CI noch Host eine Regel publiziert haben (`0` = Default-Regel aus) |
| `outage_default_severity` | `high` | `critical` / `high` / `medium` / `low` im Payload der Default-Regel |
| `outage_default_auto_resolve` | `true` | darf der Konsument (ITSM) bei `outage_recovered` automatisch lösen (Default-Regel) |
| `override_default_minutes` | `60` | Kategorie *Maintenance windows*: Vorauswahl der Dauer eines manuellen Overrides |
| `override_max_minutes` | `240` | Obergrenze eines manuellen Overrides (längere Anfragen werden gekappt) |
| `override_linger_minutes` | `15` | Nachlauf nach dem Ende eines eigenen Overrides, bevor `DOWN` wieder zählt |

Die Einstellungen werden beim Laden des Plugins, nach `vault:ready_for_data` und bei
`plugin:settings_changed` (Core ≥ 1.5.1) angewendet — auch die Outage- und Override-Werte,
ohne Reload.

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
Die Wartungsfenster testen `test_maintenance_windows.py` (Bus-Handler, Store, Cache, manuelle
Fenster/Overrides), `test_store_heartbeat_maintenance.py` (Zustand `MAINTENANCE`, Aggregate,
Outage-Uhr), `test_outage_rule.py` (Evaluator mit eingefrorener Uhr, Regelkaskade, Notschalter,
Exactly-once, Recovery) und `test_inventory_ci_id.py` (`ci_id` im Inventar-Import und im
Config-Hash). `tests/test_maintenance.py` behandelt die Plugin-Pflege (Settings-Buttons).
