"""Solo-cycle measurement orchestrator — drives dsp408-mqtt bridge to mute
all-but-one speaker in turn and runs measure.py for each.

Produces one .txt per channel + one for the all-4 sum, all in the same
REW-compatible format.

Channels assumed (1-indexed, matching the bridge's HA convention):
    ch1 = Front Right    ch2 = Front Left
    ch7 = Rear Right     ch8 = Rear Left

Override with --speakers if your assignments differ.

Usage:
    python iterate_all.py \\
        --broker 10.21.0.138 --device 4e9d357f5700 \\
        --prefix "P4 EQon" --sweep-length 524288 \\
        --also-all-four

Dependencies: paho-mqtt + measure.py's deps (sounddevice, numpy, scipy).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import paho.mqtt.client as mqtt


DEFAULT_SPEAKERS = [
    # (label, MQTT 1-indexed channel, filename suffix)
    ("FR",    1, "fr"),
    ("FL",    2, "fl"),
    ("RearR", 7, "rear_r"),
    ("RearL", 8, "rear_l"),
]


class MuteCoordinator:
    def __init__(self, host, port, base):
        self.base = base
        self.state: dict[int, str] = {}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(host, port, 10)
        self.client.loop_start()
        time.sleep(1.0)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        for n in range(1, 9):
            client.subscribe(f"{self.base}/ch{n}_mute/state")

    def _on_message(self, client, userdata, msg):
        if msg.topic.endswith("_mute/state"):
            name = msg.topic.split("/")[-2]  # "ch3_mute"
            try:
                ch = int(name[2:].split("_")[0])
                self.state[ch] = msg.payload.decode()
            except ValueError:
                pass

    def set_mute(self, ch: int, muted: bool):
        self.client.publish(f"{self.base}/ch{ch}_mute/set",
                             "ON" if muted else "OFF")

    def await_state(self, expected: dict[int, str], timeout: float = 5.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if all(self.state.get(ch) == want for ch, want in expected.items()):
                return True
            time.sleep(0.1)
        return False

    def solo(self, solo_ch: int) -> bool:
        expected = {}
        for n in range(1, 9):
            want_muted = (n != solo_ch)
            self.set_mute(n, want_muted)
            expected[n] = "ON" if want_muted else "OFF"
        return self.await_state(expected, timeout=6.0)

    def unmute_all(self):
        for n in range(1, 9):
            self.set_mute(n, False)
        self.await_state({n: "OFF" for n in range(1, 9)}, timeout=6.0)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def run_measurement(measure_script: str, title: str, output: str,
                     sweep_length: int, cal_file: str | None,
                     output_device: str | None, input_device: str | None) -> bool:
    cmd = [sys.executable, measure_script,
            "--title", title,
            "--output", output,
            "--sweep-length", str(sweep_length)]
    if cal_file:
        cmd += ["--cal-file", cal_file]
    if output_device:
        cmd += ["--output-device", output_device]
    if input_device:
        cmd += ["--input-device", input_device]
    print(f"    running: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    FAIL: {r.stderr}")
        return False
    for line in r.stdout.splitlines():
        print(f"    {line}")
    return True


def parse_speakers(spec: str | None):
    """Parse --speakers like 'FR:1:fr,FL:2:fl,RearR:7:rear_r,RearL:8:rear_l'."""
    if not spec:
        return DEFAULT_SPEAKERS
    out = []
    for entry in spec.split(","):
        parts = entry.split(":")
        if len(parts) != 3:
            sys.exit(f"--speakers entry must be 'label:mqtt_ch:suffix', got {entry!r}")
        out.append((parts[0], int(parts[1]), parts[2]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", required=True, help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--device", required=True, help="Device slug (e.g. 4e9d357f5700)")
    ap.add_argument("--base-topic", default="dsp408")
    ap.add_argument("--prefix", required=True,
                    help="Filename prefix (e.g. 'P4 EQon')")
    ap.add_argument("--sweep-length", type=int, default=262144)
    ap.add_argument("--cal-file", default=None)
    ap.add_argument("--output-device", default=None)
    ap.add_argument("--input-device", default=None)
    ap.add_argument("--out-dir", default=".",
                    help="Directory for output .txt files (default: cwd)")
    ap.add_argument("--measure-script", default=None,
                    help="Path to measure.py (default: same dir as this script)")
    ap.add_argument("--speakers", default=None,
                    help="Override speakers as 'label:mqtt_ch:suffix' "
                         "comma-separated (default: FR/FL/RearR/RearL)")
    ap.add_argument("--also-all-four", action="store_true",
                    help="Add a final all-channels-unmuted measurement")
    args = ap.parse_args()

    speakers = parse_speakers(args.speakers)
    base = f"{args.base_topic}/{args.device}"
    measure_script = args.measure_script or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "measure.py")
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    safe_prefix = args.prefix.replace(" ", "_")

    coord = MuteCoordinator(args.broker, args.port, base)
    try:
        for label, mqtt_ch, suffix in speakers:
            print(f"\n=== Solo {label} (MQTT ch{mqtt_ch}) ===")
            coord.solo(mqtt_ch)
            time.sleep(0.5)
            output = os.path.join(out_dir, f"{safe_prefix}_{suffix}.txt")
            run_measurement(measure_script, f"{args.prefix} {label}", output,
                              args.sweep_length, args.cal_file,
                              args.output_device, args.input_device)

        if args.also_all_four:
            print("\n=== All speakers ===")
            coord.unmute_all()
            time.sleep(0.5)
            output = os.path.join(out_dir, f"{safe_prefix}_all4.txt")
            run_measurement(measure_script, f"{args.prefix} all-4", output,
                              args.sweep_length, args.cal_file,
                              args.output_device, args.input_device)
    finally:
        print("\n=== Unmuting all ===")
        coord.unmute_all()
        coord.close()


if __name__ == "__main__":
    main()
