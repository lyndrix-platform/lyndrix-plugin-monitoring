# Re-export everything from the canonical controller layer so existing NiceGUI
# view imports (ui/views/cards.py, split.py, table.py) continue to work unchanged.
from ..controller.helpers import (  # noqa: F401
    humanize_label,
    infer_site_and_stage,
    service_display_name,
    host_display_name,
    infer_location,
    build_grouped_overview,
    flatten_monitors,
)
