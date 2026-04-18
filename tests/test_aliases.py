"""Unit tests for dsp408.config — alias loading + friendly name lookup."""
from __future__ import annotations

from pathlib import Path

from dsp408.config import (
    _xdg_config_home,
    default_search_paths,
    friendly_name_for,
    load_aliases,
)


def test_load_aliases_missing_file(tmp_path: Path) -> None:
    """Missing file → empty dict (not an error)."""
    assert load_aliases(tmp_path / "nonexistent.toml") == {}


def test_load_aliases_basic(tmp_path: Path) -> None:
    p = tmp_path / "aliases.toml"
    p.write_text(
        '[aliases]\n'
        '"4EAA4B964C00" = "Living Room Subs"\n'
        '"dsp408-abc123" = "Garage Amp"\n',
        encoding="utf-8",
    )
    a = load_aliases(p)
    assert a == {
        "4EAA4B964C00": "Living Room Subs",
        "dsp408-abc123": "Garage Amp",
    }


def test_load_aliases_empty_section(tmp_path: Path) -> None:
    """Empty [aliases] is valid and yields {}."""
    p = tmp_path / "aliases.toml"
    p.write_text("[aliases]\n", encoding="utf-8")
    assert load_aliases(p) == {}


def test_load_aliases_strips_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "aliases.toml"
    p.write_text('[aliases]\n"abc" = "  Alpha  "\n', encoding="utf-8")
    assert load_aliases(p) == {"abc": "Alpha"}


def test_load_aliases_rejects_empty_values(tmp_path: Path) -> None:
    p = tmp_path / "aliases.toml"
    p.write_text('[aliases]\n"abc" = ""\n"def" = "D"\n', encoding="utf-8")
    assert load_aliases(p) == {"def": "D"}


def test_load_aliases_merges_in_order(tmp_path: Path) -> None:
    """Later paths override earlier ones (user > system)."""
    system = tmp_path / "system.toml"
    user = tmp_path / "user.toml"
    system.write_text('[aliases]\n"a" = "from-system"\n"b" = "b"\n', encoding="utf-8")
    user.write_text('[aliases]\n"a" = "from-user"\n', encoding="utf-8")
    a = load_aliases([system, user])
    assert a == {"a": "from-user", "b": "b"}


def test_load_aliases_invalid_toml(tmp_path: Path) -> None:
    """Malformed TOML is logged and treated as no-aliases, not fatal."""
    p = tmp_path / "aliases.toml"
    p.write_text("this is not TOML ::: {{{{", encoding="utf-8")
    assert load_aliases(p) == {}


def test_friendly_name_matches_serial() -> None:
    info = {
        "serial_number": "4EAA4B964C00",
        "display_id": "4EAA4B964C00",
        "path": b"1-1.3:1.0",
    }
    aliases = {"4EAA4B964C00": "Living Room"}
    assert friendly_name_for(info, aliases) == "Living Room"


def test_friendly_name_matches_display_id() -> None:
    """Fallback when serial is empty but a path-hashed display_id is aliased."""
    info = {
        "serial_number": "",
        "display_id": "dsp408-abc12345",
        "path": b"1-1.4:1.0",
    }
    aliases = {"dsp408-abc12345": "Garage"}
    assert friendly_name_for(info, aliases) == "Garage"


def test_friendly_name_matches_path_string() -> None:
    """Last-resort match against the hidapi path decoded as a string."""
    info = {
        "serial_number": "",
        "display_id": "dsp408-aaaa",
        "path": b"1-1.4:1.0",
    }
    aliases = {"1-1.4:1.0": "Bench Unit"}
    assert friendly_name_for(info, aliases) == "Bench Unit"


def test_friendly_name_no_match_returns_none() -> None:
    info = {
        "serial_number": "XYZ",
        "display_id": "dsp408-foo",
        "path": b"1-1",
    }
    assert friendly_name_for(info, {"other": "x"}) is None


def test_friendly_name_empty_aliases() -> None:
    info = {"serial_number": "X", "display_id": "Y", "path": b"Z"}
    assert friendly_name_for(info, {}) is None


def test_default_search_paths_shape() -> None:
    """Sanity: the default search list is ordered system → user → cwd."""
    paths = default_search_paths()
    assert len(paths) == 3
    assert str(paths[0]).startswith("/etc/")
    # user path should be under the xdg config home
    assert _xdg_config_home() in paths[1].parents
    # last should be cwd-relative
    assert paths[2].parent == Path.cwd()
