"""dsp408.cli — command-line tool for live DSP-408 experiments.

Usage:
    dsp408 list                     # enumerate attached DSP-408s
    dsp408 [--device SEL] info      # CONNECT + GET_INFO + preset name
    dsp408 [--device SEL] snapshot  # full startup handshake dump
    dsp408 [--device SEL] read <cmd_hex> [--cat 09|04]
    dsp408 [--device SEL] read-channel <0..7>
    dsp408 [--device SEL] write <cmd_hex> <hex_payload> [--cat 09|04]
    dsp408 [--device SEL] write-param <ch> <sub_idx_hex> <value_int>
    dsp408 [--device SEL] poll [--interval 1.0]
    dsp408 [--device SEL] flash <firmware.bin>
    dsp408 mqtt --broker HOST [--port 1883] [...]  # HA discovery bridge

`--device SEL` accepts:
    * an integer index ("0", "1", ...) into `dsp408 list`
    * a serial number ("MYDW-AV1234")
    * a display_id ("dsp408-a1b2c3d4")
    * a hidapi path ("/dev/hidraw0")

Omit it to target the first device found.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import Device, DeviceNotFound, ProtocolError, resolve_selector
from .config import load_aliases
from .flasher import flash_firmware
from .protocol import category_hint


def _resolve_category(cmd: int, cat_str: str) -> int:
    s = (cat_str or "").strip().lower()
    if s in ("", "auto"):
        return category_hint(cmd)
    return int(s, 16)


def _p(label: str, value) -> None:
    print(f"  {label:<16} {value}")


def _aliases_from_args(args) -> dict[str, str]:
    """Resolve the alias map from the global `--aliases PATH` flag
    (explicit file) or the default search paths (config)."""
    path = getattr(args, "aliases", None)
    if path:
        return load_aliases(Path(path))
    return load_aliases()


def _open_device(args) -> Device:
    """Open the device selected by the global `--device` flag."""
    sel: str | None = getattr(args, "device", None)
    aliases = _aliases_from_args(args)
    # enumerate_devices() already applies aliases; Device.open() picks
    # through those. We pass the aliases via a fresh enumerate so that
    # Device.open(selector=<friendly name>) resolves correctly.
    from .device import enumerate_devices
    from .device import resolve_selector as _rs
    devs = enumerate_devices(aliases=aliases)
    if getattr(args, "device", None) is None:
        chosen = _rs(None, devs) if devs else None
    else:
        chosen = _rs(sel, devs)
    if chosen is None:
        raise DeviceNotFound("No DSP-408 attached")
    return Device.open(path=chosen["path"])


def cmd_list(args) -> int:
    from .device import enumerate_devices
    aliases = _aliases_from_args(args)
    devs = enumerate_devices(aliases=aliases)
    if not devs:
        print("(no DSP-408 found)")
        return 1
    for d in devs:
        fname = d.get("friendly_name") or d["display_id"]
        if fname != d["display_id"]:
            print(f"[{d['index']}] {fname}  ({d['display_id']})")
        else:
            print(f"[{d['index']}] {d['display_id']}")
        _p("vendor", hex(d["vid"]))
        _p("product", hex(d["pid"]))
        _p("serial", d.get("serial_number", ""))
        _p("product_str", d.get("product_string", ""))
        _p("manufacturer", d.get("manufacturer", ""))
        _p("path", d["path"].decode("utf-8", errors="replace"))
    print(f"\n{len(devs)} DSP-408 device(s) found.")
    if aliases:
        print(f"(aliases loaded: {len(aliases)})")
    return 0


def cmd_info(args) -> int:
    with _open_device(args) as dev:
        status = dev.connect()
        identity = dev.get_info()
        preset = dev.read_preset_name()
        label = dev.friendly_name
        if label != dev.display_id:
            print(f"Device:         {label}  ({dev.display_id})")
        else:
            print(f"Device:         {dev.display_id}")
        print(f"CONNECT status: 0x{status:02x}")
        print(f"GET_INFO:       {identity!r}")
        print(f"Preset name:    {preset!r}")
    return 0


def cmd_snapshot(args) -> int:
    with _open_device(args) as dev:
        info = dev.snapshot()
        _p("device",      dev.display_id)
        _p("identity",    info.identity)
        _p("preset name", info.preset_name)
        _p("status byte", f"0x{info.status_byte:02x}")
        _p("state 0x13",  info.state_13.hex(" "))
        _p("global 0x02", info.global_02.hex(" "))
        _p("global 0x05", info.global_05.hex(" "))
        _p("global 0x06", info.global_06.hex(" "))
    return 0


def cmd_read(args) -> int:
    cmd = int(args.cmd_hex, 16)
    cat = _resolve_category(cmd, args.cat)
    with _open_device(args) as dev:
        dev.connect()
        reply = dev.read_raw(cmd=cmd, category=cat, timeout_ms=3000)
    print(f"cmd=0x{reply.cmd:04x} cat=0x{reply.category:02x} "
          f"dir=0x{reply.direction:02x} seq={reply.seq} "
          f"len={reply.payload_len} chk_ok={reply.checksum_ok}")
    print(f"payload ({len(reply.payload)} bytes):")
    print(reply.payload.hex(" "))
    return 0


def cmd_read_channel(args) -> int:
    ch = int(args.channel)
    with _open_device(args) as dev:
        dev.connect()
        data = dev.read_channel_state(ch)
    print(f"channel {ch}: {len(data)} bytes")
    print(data.hex(" "))
    return 0


def cmd_write(args) -> int:
    cmd = int(args.cmd_hex, 16)
    cat = _resolve_category(cmd, args.cat)
    payload = bytes.fromhex(args.hex_payload.replace(" ", ""))
    with _open_device(args) as dev:
        dev.connect()
        reply = dev.write_raw(cmd=cmd, data=payload, category=cat)
    print(f"ack dir=0x{reply.direction:02x} cat=0x{reply.category:02x} "
          f"seq={reply.seq} len={reply.payload_len}")
    return 0


def cmd_write_param(args) -> int:
    ch = int(args.channel)
    sub = int(args.sub_idx_hex, 16)
    val = int(args.value)
    with _open_device(args) as dev:
        dev.connect()
        dev.write_channel_param(channel=ch, value=val, sub_index=sub)
    print(f"wrote ch={ch} sub=0x{sub:02x} value={val}")
    return 0


def cmd_poll(args) -> int:
    interval = float(args.interval)
    with _open_device(args) as dev:
        dev.connect()
        try:
            while True:
                state = dev.read_state_0x13()
                preset = dev.read_preset_name()
                print(f"{time.strftime('%H:%M:%S')}  preset={preset!r:16}  "
                      f"state_0x13={state.hex(' ')}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
    return 0


def cmd_flash(args) -> int:
    fw = Path(args.firmware)
    if not fw.exists():
        print(f"firmware not found: {fw}", file=sys.stderr)
        return 1

    # Resolve the device path up-front so we flash the right one even
    # without reopening on re-enumeration hiccups. The flasher bypasses
    # the normal CONNECT handshake: it drives the bootloader directly
    # via hidraw path, so we only need the path here (not a live
    # Device connection).
    sel = getattr(args, "device", None)
    if sel is not None:
        devs = Device.enumerate()
        path = resolve_selector(sel, devs)["path"]
    else:
        path = None

    def progress(cur: int, total: int, label: str) -> None:
        if total:
            pct = cur * 100 // total
            print(f"\r  [{label:>22}] {cur}/{total} ({pct}%)", end="", flush=True)
        else:
            print(f"\r  [{label}]", end="", flush=True)

    try:
        flash_firmware(fw, progress=progress, device_path=path)
    finally:
        print()
    print("Flash complete. Unplug and replug after reboot (~20 s).")
    return 0


def cmd_mqtt(args) -> int:
    """Run the MQTT / Home Assistant discovery bridge (foreground)."""
    try:
        from .mqtt import MqttBridge
    except ImportError as e:
        print(f"error: {e}", file=sys.stderr)
        print("install with: uv sync --extra mqtt   (or pip install paho-mqtt)",
              file=sys.stderr)
        return 1

    bridge = MqttBridge(
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        base_topic=args.topic_prefix,
        discovery_prefix=args.discovery_prefix,
        poll_interval=float(args.poll_interval),
        selector=args.device,
        aliases=_aliases_from_args(args),
    )

    # Clean shutdown on SIGTERM (systemd, `kill PID`, container stop) and
    # SIGHUP. Without this, the process dies mid-run and libusb never
    # releases the USB interface — the kernel `usbhid` driver doesn't
    # re-attach, `/dev/hidraw0` disappears, and nothing can open the
    # device until the bridge is forcibly killed AND the next process
    # starts (or the device is unplugged). Caught live on a Pi.
    import signal

    def _shutdown(signum, _frame):
        print(f"\nreceived signal {signum}, stopping...", file=sys.stderr)
        bridge.stop()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _shutdown)
        except (OSError, ValueError):
            # Not supported on Windows / not main thread
            pass

    try:
        bridge.run()
    except KeyboardInterrupt:
        print()
        bridge.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dsp408")
    ap.add_argument(
        "--device",
        default=None,
        metavar="SEL",
        help="device selector: index, serial, display_id, friendly name, or hidraw path",
    )
    ap.add_argument(
        "--aliases",
        default=None,
        metavar="PATH",
        help="explicit device-aliases TOML (default: ~/.config/dsp408/aliases.toml)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="enumerate attached DSP-408s").set_defaults(
        func=cmd_list
    )
    sub.add_parser("info", help="CONNECT + GET_INFO + preset name").set_defaults(
        func=cmd_info
    )
    sub.add_parser("snapshot", help="full startup handshake dump").set_defaults(
        func=cmd_snapshot
    )

    p = sub.add_parser("read", help="raw READ by command code")
    p.add_argument("cmd_hex", help="command code, e.g. 0x04 or 7700")
    p.add_argument("--cat", default="auto",
                   help="category byte hex (auto|09=state|04=param)")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("read-channel", help="read 296-byte channel state")
    p.add_argument("channel", type=int, help="channel 0..7")
    p.set_defaults(func=cmd_read_channel)

    p = sub.add_parser("write", help="raw WRITE by command code")
    p.add_argument("cmd_hex", help="command code, e.g. 1f07")
    p.add_argument("hex_payload", help="payload bytes, e.g. '01 00 96 01 00 00 00 12'")
    p.add_argument("--cat", default="auto",
                   help="category byte hex (auto|04=param|09=state)")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("write-param", help="write one channel parameter")
    p.add_argument("channel", type=int, help="channel 0..7")
    p.add_argument("sub_idx_hex", help="sub-index hex, e.g. 0x12")
    p.add_argument("value", type=int, help="u32 value")
    p.set_defaults(func=cmd_write_param)

    p = sub.add_parser("poll", help="print preset name + state_0x13 on a loop")
    p.add_argument("--interval", default="1.0", help="seconds between polls")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("flash", help="flash a .bin firmware image")
    p.add_argument("firmware", help="path to .bin")
    p.set_defaults(func=cmd_flash)

    p = sub.add_parser(
        "mqtt",
        help="run Home Assistant MQTT discovery bridge for all connected DSP-408s",
    )
    p.add_argument("--broker", required=True, help="MQTT broker host / IP")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--topic-prefix", default="dsp408",
                   help="base topic tree (default: dsp408)")
    p.add_argument("--discovery-prefix", default="homeassistant",
                   help="HA discovery topic prefix (default: homeassistant)")
    p.add_argument("--poll-interval", default="2.0",
                   help="seconds between state polls (default: 2.0)")
    p.set_defaults(func=cmd_mqtt)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except DeviceNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ProtocolError as e:
        print(f"protocol error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
