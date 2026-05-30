from nicegui import ui

from ..model.models import MonitorType, MonitorUpsert
from ..controller.service import MonitoringService
from typing import Dict, Any


def render_settings_ui(ctx, service: MonitoringService):
    form_state = {
        "monitor_id": "",
        "name": "",
        "monitor_type": MonitorType.HTTP.value,
        "target": "",
        "address": "",
        "host_name": "",
        "service_name": "",
        "logical_group": "manual",
        "interval_seconds": 60,
        "timeout_seconds": 10,
        "enabled": True,
        "force": False,
    }

    def reset_form():
        form_state.update({
            "monitor_id": "",
            "name": "",
            "monitor_type": MonitorType.HTTP.value,
            "target": "",
            "address": "",
            "host_name": "",
            "service_name": "",
            "logical_group": "manual",
            "interval_seconds": 60,
            "timeout_seconds": 10,
            "enabled": True,
            "force": False,
        })
        mode_label.set_text("Create")

    def load_monitor(row: Dict[str, Any]):
        form_state.update({
            "monitor_id": row["monitor_id"],
            "name": row["name"],
            "monitor_type": row["monitor_type"],
            "target": row.get("target") or "",
            "address": row.get("address") or "",
            "host_name": row.get("host_name") or "",
            "service_name": row.get("service_name") or "",
            "logical_group": row.get("logical_group") or "manual",
            "interval_seconds": row.get("interval_seconds") or 60,
            "timeout_seconds": row.get("timeout_seconds") or 10,
            "enabled": row.get("enabled", True),
            "force": False,
        })
        mode_label.set_text("Edit")
        dialog.open()

    def refresh_rows():
        table.rows = service.list_monitors()
        table.update()

    def save_form():
        try:
            payload = MonitorUpsert(
                monitor_id=form_state["monitor_id"],
                name=form_state["name"],
                monitor_type=MonitorType(form_state["monitor_type"]),
                owner_source="ui_admin",
                target=form_state["target"] or None,
                address=form_state["address"] or None,
                host_name=form_state["host_name"] or None,
                service_name=form_state["service_name"] or None,
                logical_group=form_state["logical_group"],
                interval_seconds=int(form_state["interval_seconds"]),
                timeout_seconds=int(form_state["timeout_seconds"]),
                enabled=bool(form_state["enabled"]),
                force=bool(form_state["force"]),
            )
            service.upsert_monitor(payload)
            refresh_rows()
            ui.notify("Monitor saved.", type="positive")
            dialog.close()
            reset_form()
        except Exception as exc:
            ui.notify(str(exc), type="negative")

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl p-0 overflow-hidden bg-zinc-950 border border-zinc-800"):
        ui.element("div").classes("h-1 w-full bg-gradient-to-r from-cyan-400 via-emerald-400 to-lime-400")
        with ui.column().classes("w-full p-6 gap-4"):
            with ui.row().classes("w-full justify-between items-center"):
                with ui.column().classes("gap-0"):
                    ui.label("Monitor Editor").classes("text-xl font-black text-zinc-50")
                    ui.label("Create or update HTTP and ICMP checks.").classes("text-sm text-zinc-400")
                mode_label = ui.label("Create").classes("text-xs uppercase tracking-widest text-emerald-300 bg-emerald-500/10 px-3 py-1 rounded-full border border-emerald-500/20")
            with ui.grid(columns=2).classes("w-full gap-4"):
                ui.input("Monitor ID").bind_value(form_state, "monitor_id").props("outlined dark").classes("w-full")
                ui.input("Display Name").bind_value(form_state, "name").props("outlined dark").classes("w-full")
                ui.select([t.value for t in MonitorType], value=MonitorType.HTTP.value, label="Probe Type").bind_value(form_state, "monitor_type").props("outlined dark").classes("w-full")
                ui.input("Logical Group").bind_value(form_state, "logical_group").props("outlined dark").classes("w-full")
                ui.input("Target URL / Host").bind_value(form_state, "target").props("outlined dark").classes("w-full")
                ui.input("Address / IP").bind_value(form_state, "address").props("outlined dark").classes("w-full")
                ui.input("Host Name").bind_value(form_state, "host_name").props("outlined dark").classes("w-full")
                ui.input("Service Name").bind_value(form_state, "service_name").props("outlined dark").classes("w-full")
                ui.number("Interval (s)", min=10, max=3600, value=60).bind_value(form_state, "interval_seconds").props("outlined dark").classes("w-full")
                ui.number("Timeout (s)", min=1, max=60, value=10).bind_value(form_state, "timeout_seconds").props("outlined dark").classes("w-full")
            with ui.row().classes("w-full items-center justify-between mt-2"):
                with ui.row().classes("gap-4 items-center"):
                    ui.switch("Enabled", value=True).bind_value(form_state, "enabled").props("color=positive")
                    ui.switch("Force owner override", value=False).bind_value(form_state, "force").props("color=warning")
                with ui.row().classes("gap-2"):
                    ui.button("Reset", on_click=reset_form, icon="ink_eraser").props("outline")
                    ui.button("Save Monitor", on_click=save_form, icon="save", color="primary").props("unelevated")

    with ui.column().classes("w-full gap-5 pt-2"):
        with ui.card().classes("w-full p-0 overflow-hidden bg-gradient-to-br from-zinc-950 to-zinc-900 border border-zinc-800"):
            ui.element("div").classes("h-1 w-full bg-gradient-to-r from-cyan-400 via-sky-400 to-blue-500")
            with ui.column().classes("w-full p-6 gap-4"):
                with ui.row().classes("w-full justify-between items-end"):
                    with ui.column().classes("gap-0"):
                        ui.label("Monitor Registry").classes("text-xl font-black text-zinc-50")
                        ui.label("All monitors with live state and scheduled probes.").classes("text-sm text-zinc-400")
                    ui.button("New Monitor", on_click=lambda: (reset_form(), dialog.open()), icon="add_circle", color="positive").props("unelevated")

                table = ui.table(
                    columns=[
                        {"name": "name", "label": "Name", "field": "name"},
                        {"name": "monitor_type", "label": "Type", "field": "monitor_type"},
                        {"name": "logical_group", "label": "Group", "field": "logical_group"},
                        {"name": "latest_state", "label": "State", "field": "latest_state"},
                        {"name": "uptime_24h", "label": "24h %", "field": "uptime_24h"},
                        {"name": "action", "label": "", "field": "action"},
                    ],
                    rows=service.list_monitors(),
                    row_key="monitor_id",
                ).classes("w-full bg-transparent")
                table.add_slot(
                    "body-cell-latest_state",
                    '<q-td :props="props">'
                    '<q-badge :color="props.value === &quot;UP&quot; ? &quot;positive&quot; : (props.value === &quot;PAUSED&quot; ? &quot;warning&quot; : (props.value === &quot;DOWN&quot; ? &quot;negative&quot; : &quot;grey-7&quot;))">'
                    "{{ props.value }}</q-badge></q-td>",
                )
                table.add_slot(
                    "body-cell-action",
                    '<q-td :props="props"><q-btn flat round size="sm" icon="edit" color="primary" @click="() => $parent.$emit(&quot;edit&quot;, props.row)" /></q-td>',
                )
                table.on("edit", lambda event: load_monitor(event.args))

        # ── Danger Zone ────────────────────────────────────────────────────
        with ui.card().classes("w-full p-0 overflow-hidden bg-gradient-to-br from-zinc-950 to-zinc-900 border border-rose-900/40"):
            ui.element("div").classes("h-1 w-full bg-gradient-to-r from-rose-500 via-red-500 to-orange-500")
            with ui.column().classes("w-full p-6 gap-4"):
                with ui.row().classes("w-full items-center gap-3"):
                    ui.icon("warning", size="22px").classes("text-rose-400")
                    with ui.column().classes("gap-0"):
                        ui.label("Danger Zone").classes("text-lg font-black text-zinc-50")
                        ui.label("Irreversible actions — proceed with caution.").classes("text-sm text-zinc-400")

                with ui.row().classes("w-full items-center justify-between gap-4 flex-wrap "
                                      "p-4 bg-rose-950/20 border border-rose-900/30 rounded-xl"):
                    with ui.column().classes("gap-0.5"):
                        ui.label("Clear states database").classes("text-sm font-semibold text-zinc-200")
                        ui.label(
                            "Deletes all heartbeat records and daily aggregates. "
                            "Resets every monitor back to UNKNOWN state. "
                            "New probe results will repopulate the history."
                        ).classes("text-xs text-zinc-400 max-w-lg")

                    with ui.dialog() as confirm_dialog, ui.card().classes(
                        "w-full max-w-md p-0 overflow-hidden bg-zinc-950 border border-rose-900/60"
                    ):
                        ui.element("div").classes("h-1 w-full bg-gradient-to-r from-rose-500 to-red-500")
                        with ui.column().classes("w-full p-6 gap-4"):
                            with ui.row().classes("items-center gap-3"):
                                ui.icon("delete_forever", size="28px").classes("text-rose-400")
                                ui.label("Confirm: Clear Database").classes("text-lg font-black text-zinc-50")
                            ui.label(
                                "This will permanently delete ALL heartbeat records, daily aggregates, "
                                "and reset every monitor to UNKNOWN. This cannot be undone."
                            ).classes("text-sm text-zinc-400")
                            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                                ui.button("Cancel", on_click=confirm_dialog.close).props("flat").classes("text-zinc-400")
                                def _do_clear(d=confirm_dialog):
                                    try:
                                        deleted = service.clear_states_db()
                                        ui.notify(f"Cleared {deleted} heartbeat records. All monitors reset to UNKNOWN.", type="positive", timeout=5000)
                                    except Exception as exc:
                                        ui.notify(f"Error: {exc}", type="negative")
                                    d.close()
                                ui.button("Clear Database", on_click=_do_clear, icon="delete_forever").props("unelevated color=negative")

                    ui.button(
                        "Clear States Database",
                        icon="delete_sweep",
                        on_click=confirm_dialog.open,
                    ).props("outline color=negative")

