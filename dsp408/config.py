"""dsp408.config — user-facing configuration (device aliases).

The DSP-408 reports a flat 12-char hex identifier like `4EAA4B964C00` as
its serial number — fine for machines, unfriendly for humans. A small
TOML config maps those identifiers to friendly names that surface in:

  * `dsp408 list` output
  * The Gradio web UI device dropdown
  * Home Assistant MQTT discovery (`dev.name` field — replaces the
    generic "DSP-408 <id>" label)
  * `--device SEL` selector (you can pass the friendly name)

Config file format (~/.config/dsp408/aliases.toml):

    # Keys are matched against a device's serial_number, display_id,
    # or stringified hidapi path in that order.
    [aliases]
    "4EAA4B964C00"    = "Living Room Subs"
    "dsp408-cf594b63" = "Garage Amp"

Search order (later wins, so system-wide defaults get overridden by user):

  1. /etc/dsp408/aliases.toml
  2. $XDG_CONFIG_HOME/dsp408/aliases.toml  (default: ~/.config/dsp408/aliases.toml)
  3. ./dsp408-aliases.toml  (current working directory)

You can also pass `--aliases PATH` on any dsp408 CLI subcommand to use
an explicit file instead (skips the search entirely).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

# Prefer stdlib tomllib (3.11+); fall back to tomli on 3.10.
try:
    import tomllib as _toml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 3.10 path
    try:
        import tomli as _toml  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        _toml = None  # type: ignore[assignment]


DEFAULT_FILENAME = "aliases.toml"
LOCAL_FILENAME = "dsp408-aliases.toml"


def _xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def default_search_paths() -> list[Path]:
    """Return the default config-file search order (lowest → highest priority)."""
    return [
        Path("/etc/dsp408") / DEFAULT_FILENAME,
        _xdg_config_home() / "dsp408" / DEFAULT_FILENAME,
        Path.cwd() / LOCAL_FILENAME,
    ]


def _parse_toml(path: Path) -> dict:
    if _toml is None:
        log.warning(
            "device aliases: no TOML parser available "
            "(install tomli on py<3.11); ignoring %s",
            path,
        )
        return {}
    try:
        with path.open("rb") as f:
            return _toml.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("device aliases: could not parse %s: %s", path, e)
        return {}


def load_aliases(paths: Iterable[Path] | Path | None = None) -> dict[str, str]:
    """Load the `[aliases]` table as a plain dict[id → friendly_name].

    Args:
        paths: If None, uses `default_search_paths()`. If a single Path,
            loads only that file (for explicit `--aliases PATH`). If an
            iterable, loads each in order (later overrides earlier).

    Returns:
        Flat mapping of device id → friendly name. Missing files and
        missing `[aliases]` tables silently yield an empty dict.
    """
    if paths is None:
        paths_list: list[Path] = default_search_paths()
    elif isinstance(paths, Path):
        paths_list = [paths]
    else:
        paths_list = list(paths)

    merged: dict[str, str] = {}
    for p in paths_list:
        if not p.is_file():
            continue
        doc = _parse_toml(p)
        section = doc.get("aliases") or {}
        if not isinstance(section, dict):
            log.warning("device aliases: [aliases] in %s is not a table", p)
            continue
        for k, v in section.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                merged[k] = v.strip()
    return merged


def friendly_name_for(info: dict, aliases: dict[str, str]) -> str | None:
    """Return the configured friendly name for a device info dict, else None.

    Match order: serial_number → display_id → stringified hidapi path.
    Returns None if no alias is configured; the caller decides what to
    fall back to (typically `display_id`).
    """
    if not aliases:
        return None
    candidates = [
        (info.get("serial_number") or "").strip(),
        (info.get("display_id") or "").strip(),
    ]
    path = info.get("path")
    if isinstance(path, bytes):
        try:
            candidates.append(path.decode(errors="replace"))
        except Exception:
            pass
    elif isinstance(path, str):
        candidates.append(path)
    for c in candidates:
        if c and c in aliases:
            return aliases[c]
    return None
