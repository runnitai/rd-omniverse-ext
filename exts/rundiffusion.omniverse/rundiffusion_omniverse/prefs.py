"""Small, non-secret settings that outlive a Kit session.

Deliberately NOT `token_store`. That one seals its contents with DPAPI because
it holds a refresh token; a display preference is neither secret nor worth a
file, and putting it there would mean a user who signs out loses it.

Kit already has the right place: a setting under `/persistent/` is saved with
the app's own user config and comes back on the next launch, which is the same
mechanism every other extension's preferences use. So this is a thin wrapper
over `carb.settings` with one job beyond it, an in-memory fallback for when
`carb` is not importable at all. That happens in the test suite, where the
whole point is to run without a host, and it means a caller can read a
preference without asking whether it is inside Kit.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Whether the status line under BILLED TO is shown.
#:
#: Off by default. It is a running commentary ("Downloading...", "Copied
#: abc-123.", "Reused the inputs from 14:05.") that was useful while the panel
#: was being built and is noise to someone using it. Failures ignore this and
#: are always shown: see `RunDiffusionPanel._set_error`.
SHOW_STATUS_LOG = "/persistent/exts/rundiffusion.omniverse/showStatusLog"

#: Where the per-surface "is it in its own window" preferences live.
#:
#: Remembered because it is a statement about how someone works rather than a
#: choice they make per session: a person with the room for the library beside
#: the Create form wants it there every time they open Kit, and being handed
#: the tabbed layout again each morning is the same as not having the feature.
_TORN_OUT_PREFIX = "/persistent/exts/rundiffusion.omniverse/"


#: The tools most recently picked in the tool browser, newest first.
#:
#: Per machine rather than per account, and kept across a sign-out. It is a
#: shortcut to what this person reaches for, which is a fact about how they
#: work in this app rather than about whose tokens pay; a tool the next account
#: cannot run simply does not appear, because the row only shows tools the
#: catalogue in hand actually lists.
RECENT_TOOLS = "/persistent/exts/rundiffusion.omniverse/recentTools"

#: How many recent tools are remembered, and shown.
RECENT_TOOLS_LIMIT = 2


def torn_out_key(surface: str) -> str:
    """The preference naming whether `surface` belongs in a window of its own.

    Built from the surface name rather than listed, so a third browsing surface
    needs nothing here. The library's key is unchanged by that construction
    (`library` -> `libraryTornOut`), which matters: it is already saved in the
    user configs of everyone on the beta, and a renamed key would silently hand
    them back the tabbed layout they had moved away from.
    """
    return f"{_TORN_OUT_PREFIX}{surface}TornOut"

#: Used when there is no `carb` to ask. Values set here are lost when Kit
#: closes, which is correct for a process that has no Kit to persist into.
_FALLBACK: dict[str, object] = {}


def _settings():
    """Kit's settings interface, or None outside a host."""
    try:
        import carb.settings

        return carb.settings.get_settings()
    except Exception:  # noqa: BLE001 - no carb means no persistence, not a crash
        return None


def get_bool(key: str, default: bool = False) -> bool:
    store = _settings()
    if store is None:
        return bool(_FALLBACK.get(key, default))
    value = store.get(key)
    # A key that was never written reads as None rather than as the default,
    # so the default is applied here rather than assumed of the store.
    return default if value is None else bool(value)


def get_string(key: str, default: str = "") -> str:
    store = _settings()
    if store is None:
        return str(_FALLBACK.get(key, default))
    value = store.get(key)
    return default if value is None else str(value)


def set_string(key: str, value: str) -> None:
    store = _settings()
    if store is None:
        _FALLBACK[key] = str(value)
        return
    try:
        store.set_string(key, str(value))
    except Exception:  # noqa: BLE001 - a preference that will not save is not fatal
        logger.info("RunDiffusion: could not save the %s preference.", key)
        _FALLBACK[key] = str(value)


def recent_tool_ids() -> list[str]:
    """The tools picked most recently, newest first."""
    raw = get_string(RECENT_TOOLS)
    return [tool_id for tool_id in raw.split("\n") if tool_id][:RECENT_TOOLS_LIMIT]


def remember_tool(tool_id: str) -> None:
    """Put `tool_id` at the front of the recent list, once."""
    if not tool_id:
        return
    ids = [tool_id] + [other for other in recent_tool_ids() if other != tool_id]
    # A line break cannot appear in an id, which is what makes it a safe
    # separator in a single string setting.
    set_string(RECENT_TOOLS, "\n".join(ids[:RECENT_TOOLS_LIMIT]))


def set_bool(key: str, value: bool) -> None:
    store = _settings()
    if store is None:
        _FALLBACK[key] = bool(value)
        return
    try:
        store.set_bool(key, bool(value))
    except Exception:  # noqa: BLE001 - a preference that will not save is not fatal
        logger.info("RunDiffusion: could not save the %s preference.", key)
        _FALLBACK[key] = bool(value)
