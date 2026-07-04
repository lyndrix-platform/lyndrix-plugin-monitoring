from typing import List

from ..model.models import MonitorState

# CSS variable references for state accent colours.
# The actual values are defined in lyndrix-core's theme.py (:root block)
# so they can be overridden per theme without touching plugin code.
_STATE_ACCENT_VAR = {
    MonitorState.UP.value:      "var(--lx-state-up)",
    MonitorState.DOWN.value:    "var(--lx-state-down)",
    MonitorState.PAUSED.value:  "var(--lx-state-paused)",
    MonitorState.UNKNOWN.value: "var(--lx-state-unknown)",
}

# Maps MonitorState values to the shared `.lx-badge--{state}` modifier suffix
# (lowercase), defined once in lyndrix-core's theme.py <style> block and
# mirrored verbatim in lyndrix-ui/src/index.css, so BOTH GUI stacks resolve
# state colour off the SAME class family instead of each hand-rolling its own
# Tailwind palette (the old ``STATE_STYLES`` map this replaces contradicted
# ``state_color()`` above — a real drift bug, not just a style nit).
_STATE_CLASS_SUFFIX = {
    MonitorState.UP.value: "up",
    MonitorState.DOWN.value: "down",
    MonitorState.PAUSED.value: "paused",
    MonitorState.UNKNOWN.value: "unknown",
}
_DEFAULT_SUFFIX = _STATE_CLASS_SUFFIX[MonitorState.UNKNOWN.value]


def state_badge_classes(state: str) -> str:
    """Tailwind classes for a state pill.

    Combines the shared ``lx-badge--{state}`` modifier (colour/background/
    border-color driven by ``--lx-state-*`` + ``color-mix()``, defined once
    in core's theme.py / lyndrix-ui's index.css) with the ``border`` utility
    for border-width (the modifier itself only sets border-*colour*). Layout
    (padding/font-size/rounding) stays at the call site so existing pill
    chrome is unaffected.
    """
    suffix = _STATE_CLASS_SUFFIX.get(state, _DEFAULT_SUFFIX)
    return f"border lx-badge--{suffix}"


def state_color(state: str) -> str:
    """Return a CSS value for the state accent colour.

    Returns a ``var(--lx-state-*)`` reference so the colour is controlled by
    the active theme rather than being hardcoded in plugin code.
    """
    return _STATE_ACCENT_VAR.get(state, _STATE_ACCENT_VAR[MonitorState.UNKNOWN.value])


def state_card_style(state: str) -> str:
    """Inline style for a state-tinted card border + subtle glow.

    Derived from the same ``--lx-state-*`` token as ``state_color()`` via
    ``color-mix()`` (replaces the old hardcoded per-state Tailwind
    ``border-*-500/25`` + literal ``rgba()`` glow in ``STATE_STYLES``, which
    could never react to a theme change). Apply via ``.style()`` alongside
    the ``lx-card`` class so it wins over that class's default border-colour.
    """
    color = state_color(state)
    border = f"color-mix(in srgb, {color} 25%, transparent)"
    glow = f"color-mix(in srgb, {color} 8%, transparent)"
    return f"border-color:{border};box-shadow:0 0 0 1px {glow}"


def state_strip_style(state: str) -> str:
    color = state_color(state)
    # BUG FIX: was `box-shadow:0 0 18px {color}66` — appending a hex-alpha
    # suffix directly onto a `var(...)` reference is invalid CSS (var() can't
    # take a suffix). color-mix() is the correct way to apply alpha to a
    # custom-property colour.
    glow = f"color-mix(in srgb, {color} 40%, transparent)"
    return f"height:4px;width:100%;background:{color};box-shadow:0 0 18px {glow}"


def aggregate_state(states: List[str]) -> str:
    filtered = [s for s in states if s]
    if not filtered:
        return MonitorState.UNKNOWN.value
    if MonitorState.DOWN.value in filtered:
        return MonitorState.DOWN.value
    if MonitorState.UNKNOWN.value in filtered:
        return MonitorState.UNKNOWN.value
    if MonitorState.UP.value in filtered:
        return MonitorState.UP.value
    if MonitorState.PAUSED.value in filtered:
        return MonitorState.PAUSED.value
    return MonitorState.UNKNOWN.value
