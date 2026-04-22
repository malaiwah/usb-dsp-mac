"""Snapshot & diff semantic state of all 8 channels via MQTT raw/read.

Talks to the dsp408-mqtt bridge over its `raw/read` topic. Useful for
regression testing after a string of writes — if nothing was mutated
outside the bands you touched, ``--diff`` prints "NO DIFFERENCES".

Usage:
    # Live print
    python state_snapshot.py --broker 10.21.0.138 --device 4e9d357f5700

    # Save before/after, compare
    python state_snapshot.py --broker 10.21.0.138 --device 4e9d357f5700 \\
        --save before.json
    # ... do some writes ...
    python state_snapshot.py --broker 10.21.0.138 --device 4e9d357f5700 \\
        --save after.json
    python state_snapshot.py --diff before.json after.json

Used during the 2026-04-22 tornado-blend stress test:
    ~1500 routing-level writes over 2 min, zero diffs on active
    channels (ch0/1/6/7).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import paho.mqtt.client as mqtt


LABELS = {0: "FR?", 1: "FL?", 2: "SubR?", 3: "SubL?",
          4: "--", 5: "--", 6: "RearR?", 7: "RearL?"}


def snapshot_channel(blob: bytes) -> dict:
    """Extract semantic fields from the 296-byte channel-state blob.

    Offsets follow the post-2026-04-22 parse_frame fix (fields ≥48
    moved +2).
    """
    out = {
        "mute":       blob[248],
        "vol_raw":    blob[250] | (blob[251] << 8),
        "spk_type":   blob[255],
        "hpf_freq":   blob[256] | (blob[257] << 8),
        "hpf_filter": blob[258],
        "hpf_slope":  blob[259],
        "lpf_freq":   blob[260] | (blob[261] << 8),
        "lpf_filter": blob[262],
        "lpf_slope":  blob[263],
        "mixer":      list(blob[264:272]),
        "eq":         [],
    }
    for band in range(10):
        o = band * 8
        out["eq"].append({
            "freq":     blob[o] | (blob[o+1] << 8),
            "gain_raw": blob[o+2] | (blob[o+3] << 8),
            "b4":       blob[o+4],
        })
    return out


def take_snapshot(host: str, port: int, base: str) -> dict:
    replies: dict = {}

    def on_msg(c, u, m):
        d = json.loads(m.payload.decode())
        replies[d["cmd"]] = d

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_msg
    client.connect(host, port, 10)
    client.subscribe(f"{base}/raw/read/reply")
    client.loop_start()
    time.sleep(0.5)
    for ch in range(8):
        client.publish(f"{base}/raw/read",
                        json.dumps({"cmd": 0x7700 | ch, "cat": 0x04}))
        time.sleep(0.4)
    time.sleep(0.5)
    client.loop_stop()
    client.disconnect()

    out: dict = {}
    for ch in range(8):
        key = hex(0x7700 | ch)
        r = replies.get(key)
        if r is None:
            out[ch] = None
            continue
        blob = bytes.fromhex(r["payload_hex"])
        out[ch] = snapshot_channel(blob)
    return out


def diff_snapshots(a: dict, b: dict) -> list:
    diffs = []
    for ch in range(8):
        if a.get(ch) is None or b.get(ch) is None:
            diffs.append((ch, "READ_FAILED", a.get(ch), b.get(ch)))
            continue
        for field in ("mute", "vol_raw", "spk_type",
                      "hpf_freq", "hpf_filter", "hpf_slope",
                      "lpf_freq", "lpf_filter", "lpf_slope", "mixer"):
            if a[ch][field] != b[ch][field]:
                diffs.append((ch, field, a[ch][field], b[ch][field]))
        for band in range(10):
            for sub in ("freq", "gain_raw", "b4"):
                va = a[ch]["eq"][band][sub]
                vb = b[ch]["eq"][band][sub]
                if va != vb:
                    diffs.append((ch, f"eq[{band}].{sub}", va, vb))
    return diffs


def fmt_state(s: dict | None) -> str:
    if s is None:
        return "  <READ FAILED>"
    eq = " ".join(f"b{i}:{e['freq']}/{(e['gain_raw']-600)/10:+.1f}/{e['b4']}"
                  for i, e in enumerate(s["eq"])
                  if 0.05 < abs((e['gain_raw']-600)/10) < 50)
    return (f"mute={s['mute']} vol_raw={s['vol_raw']} spk={s['spk_type']} "
            f"HPF=f{s['hpf_filter']}/s{s['hpf_slope']}/{s['hpf_freq']}Hz "
            f"LPF=f{s['lpf_filter']}/s{s['lpf_slope']} "
            f"mix={s['mixer']} EQ:{eq or '(none)'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--device", help="Device slug (e.g. 4e9d357f5700)")
    ap.add_argument("--base-topic", default="dsp408")
    ap.add_argument("--save", help="Save snapshot to JSON file")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE.json", "AFTER.json"),
                    help="Compare two saved snapshots (no MQTT needed)")
    args = ap.parse_args()

    if args.diff:
        with open(args.diff[0]) as f:
            before = {int(k): v for k, v in json.load(f).items()}
        with open(args.diff[1]) as f:
            after = {int(k): v for k, v in json.load(f).items()}
        diffs = diff_snapshots(before, after)
        if not diffs:
            print("✓ NO DIFFERENCES — state preserved across the run.")
        else:
            print(f"✗ {len(diffs)} differences found:")
            for ch, field, b, a in diffs:
                print(f"  ch{ch} ({LABELS[ch]}): {field}: {b} → {a}")
        return

    if not args.broker or not args.device:
        sys.exit("Live mode requires --broker and --device")

    base = f"{args.base_topic}/{args.device}"
    snap = take_snapshot(args.broker, args.port, base)
    if args.save:
        with open(args.save, "w") as f:
            json.dump(snap, f, indent=2)
        print(f"Snapshot saved to {args.save}")
    else:
        for ch in range(8):
            print(f"ch{ch} ({LABELS[ch]:>6}): {fmt_state(snap[ch])}")


if __name__ == "__main__":
    main()
