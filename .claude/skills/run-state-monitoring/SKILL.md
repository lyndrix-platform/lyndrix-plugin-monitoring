---
name: run-state-monitoring
description: Run, launch, and screenshot the State Monitoring (/monitoring) dashboard plugin. This plugin has NO standalone run path — it only runs mounted inside the lyndrix-core dev stack; use this to start/screenshot the State Monitoring host/service grid or verify a monitoring UI change in the actually-running app.
---

# Run the State Monitoring (/monitoring) plugin

This repo is a **lyndrix-core plugin**, not an app — no `main`, no server of its own.
It only runs by booting the **lyndrix-core** dev stack (FastAPI + NiceGUI + Vault +
MariaDB) with this repo **volume-mounted** as `/app/plugins/state_monitoring` and the
plugin **enabled**, then opening its UI route **`/monitoring`** on core (`:8081`).

It renders as a **NiceGUI** page over a websocket — `curl` only sees the empty shell,
so you need a real browser to *see* it. There is **no in-shell React route**: although
`entrypoint.py` declares `react_ui=True`, the running plugin reports `react_ui:false` /
`react_routes:[]` (verify: `curl -s …/api/plugins`), so the lyndrix-ui shell shows it as
an **external link** to `/monitoring`, and a deep-link to `/apps/lyndrix-plugin-state_monitoring/monitoring`
just falls back to the Dashboard. Drive the NiceGUI route instead.

Same harness as `run-lyndrix-core` / `run-server-manager` — same compose stack, same
shared venv, same Playwright driver (reused from the core skill; this skill ships no
driver of its own). Paths below are relative to this plugin repo root
(`lyndrix-plugin-monitoring/`); the core repo is its sibling `../lyndrix-core/`.

## Prerequisites

The driver runtime (Python venv + Chromium) lives in the **core** repo and is shared —
see `../lyndrix-core/.claude/skills/run-lyndrix-core/SKILL.md` (Prerequisites). Create it
once if `../lyndrix-core/.dev/run-venv` is missing:

```bash
python3 -m venv ../lyndrix-core/.dev/run-venv
. ../lyndrix-core/.dev/run-venv/bin/activate
pip install playwright
python -m playwright install chromium
sudo $(which python) -m playwright install-deps chromium   # system libs (libnspr4/libnss3/…)
```

## Bring up the stack (with this plugin mounted)

The plugin is already wired into `../lyndrix-core/docker/docker-compose.dev.yml`:

```
- ../../lyndrix-plugin-monitoring:/app/plugins/state_monitoring
```

If `docker ps` already shows `lyndrix-core-dev` on `:8081`, skip this:

```bash
docker compose -f ../lyndrix-core/docker/docker-compose.dev.yml up -d --build
```

## Ensure the plugin is enabled

The manifest sets `auto_enable_on_install=False` (`entrypoint.py`), so on a fresh DB the
plugin is discovered but starts **inactive** and `/monitoring` will not render. Confirm
it is active — it appears in `/api/health` when enabled:

```bash
curl -s http://localhost:8081/api/health | python3 -m json.tool | grep state_monitoring
# "lyndrix.plugin.state_monitoring": {
```

## Run (agent path) — screenshot the UI

Uses the core skill's Playwright driver with `--routes /monitoring`. The admin password
comes from `../lyndrix-core/docker/.env.dev` (never hardcode it; `tr -d '\r'` strips the
CRLF the dev file carries):

```bash
export LYNDRIX_ADMIN_PASSWORD="$(grep -E '^LYNDRIX_ADMIN_PASSWORD=' ../lyndrix-core/docker/.env.dev | cut -d= -f2- | tr -d '\r')"
../lyndrix-core/.dev/run-venv/bin/python \
  ../lyndrix-core/.claude/skills/run-lyndrix-core/driver.py \
  --routes /monitoring --no-mobile --outdir /tmp/mon-shots
```

Output (verified this session, core v0.2.2):

```
core_version=0.2.2 api_version=1.2.0 plugins=6
login: ok
shot: /tmp/mon-shots/login.desktop.png
shot: /tmp/mon-shots/monitoring.desktop.png
wrote 2 screenshots to /tmp/mon-shots/
```

`/tmp/mon-shots/monitoring.desktop.png` shows the **State Monitoring** dashboard: a
header KPI row (up/down/total counts) above a multi-column grid of host/service cards,
each service color-coded green (up) / red (down) / cyan. **Open the PNG and look** — a
login card means the WS-auth race lost (just re-run); the Dashboard means you hit the
React `/apps/...` path instead of the NiceGUI `/monitoring` route.

Useful flags: `--health-only` (no browser, prints `/api/health`), drop `--no-mobile` for
an extra 390px shot, `--base/--user` to override target.

## Gotchas

- **No standalone run.** No `python -m state_monitoring`, no dev server. It only renders
  inside core at `/monitoring`. `curl /monitoring` returns the empty NiceGUI shell — use
  the browser driver.
- **NiceGUI, not React (at runtime).** The manifest says `react_ui=True` but the running
  plugin reports `react_ui:false`/`react_routes:[]`, so it is an **external-link** tile in
  the lyndrix-ui shell, not an in-app `/apps/...` route. Driving `/apps/lyndrix-plugin-state_monitoring/monitoring`
  lands on the Dashboard, not the monitor. Always drive the core NiceGUI route `/monitoring`.
- **Plugin must be enabled.** `auto_enable_on_install=False` → enable it in the Plugin
  Manager (`/plugins`), else it's absent from `/api/health` and `/monitoring` 302s to `/login`.
- **Password, not hardcoded.** The driver reads `LYNDRIX_ADMIN_PASSWORD` from the env; the
  dev value lives in `../lyndrix-core/docker/.env.dev` (CRLF — pipe `| tr -d '\r'`). Without
  it the driver exits 2.
- **NiceGUI WS auth timing.** Auth hydrates over the websocket after navigation; the driver
  sleeps ~2 s post-login and retries once if a page bounces to `/login`. A login-card
  screenshot = the retry still lost; re-run.
- **Shared venv lives in core.** `../lyndrix-core/.dev/run-venv`; this plugin repo has no
  venv of its own. `chromium-cli` is intentionally not used (absent on this host).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: set LYNDRIX_ADMIN_PASSWORD …` | Export it from `../lyndrix-core/docker/.env.dev` (command above, with `tr -d '\r'`). |
| Screenshot is the Dashboard, not the monitor grid | You used the React `/apps/...` path — drive the NiceGUI `/monitoring` route via the core driver as shown. |
| Screenshot shows the login card | NiceGUI WS-auth race — re-run; if persistent, the stack may still be booting (`docker logs lyndrix-core-dev`). |
| `state_monitoring` absent from `/api/health` | Plugin not enabled — toggle it on in `/plugins`. |
| `libnspr4.so: cannot open shared object file` | `sudo $(which python) -m playwright install-deps chromium`. |
