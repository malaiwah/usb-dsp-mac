"""Level-balance analysis: extract per-speaker SPL from existing sweep
captures using a band-limited RMS (500-2000 Hz by default).

Computes the DSP-408 per-channel volume each speaker should be set to
in order to flatten levels at the mic position. The DSP-408 only
attenuates (0 to -60 dB, no boost), so we equalize DOWN to the
quietest speaker — louder ones get cut, the quietest stays at 0 dB.
"""
import argparse
import math


def load(path):
    pts = []
    for l in open(path):
        if l.startswith('*') or not l.strip():
            continue
        f, s, _ = l.split()
        pts.append((float(f), float(s)))
    return pts


def band_rms_db(pts, f_lo, f_hi):
    """Energy-average SPL over the band [f_lo, f_hi] Hz."""
    lin = [10 ** (s / 10) for f, s in pts if f_lo <= f <= f_hi]
    if not lin:
        return float("nan")
    mean_lin = sum(lin) / len(lin)
    return 10 * math.log10(mean_lin)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", required=True,
                    help="Filename prefix (e.g. 'P5_EQon_1M')")
    ap.add_argument("--band-lo", type=float, default=500.0)
    ap.add_argument("--band-hi", type=float, default=2000.0)
    ap.add_argument("--speakers", default=None,
                    help="Override as 'label:suffix' comma-separated "
                         "(default: FR/FL/RearR/RearL)")
    args = ap.parse_args()

    if args.speakers:
        speakers = [(p.split(":")[0], f"{args.prefix}_{p.split(':')[1]}.txt")
                     for p in args.speakers.split(",")]
    else:
        speakers = [
            ("FR",    f"{args.prefix}_fr.txt"),
            ("FL",    f"{args.prefix}_fl.txt"),
            ("RearR", f"{args.prefix}_rear_r.txt"),
            ("RearL", f"{args.prefix}_rear_l.txt"),
        ]

    print(f"Level balance — band-limited RMS {args.band_lo:.0f}-{args.band_hi:.0f} Hz")
    print(f"(prefix: {args.prefix})\n")

    results = []
    for label, path in speakers:
        try:
            pts = load(path)
        except FileNotFoundError:
            print(f"  {label}: file not found: {path}")
            continue
        spl = band_rms_db(pts, args.band_lo, args.band_hi)
        results.append((label, spl))

    if not results:
        return
    loudest = max(r[1] for r in results)
    quietest = min(r[1] for r in results)

    # DSP can only cut (0..-60 dB). Equalize DOWN to the quietest:
    # quietest gets 0 dB; louder ones get cut by (their_spl - quietest).
    print(f"{'Speaker':<10}  {'SPL':>7}   {'Cut to':>8}   {'DSP volume':>11}")
    print(f"{'':<10}  {'band':>7}   {'match':>8}   {'(0..-60)':>11}")
    print("-" * 54)
    for label, spl in results:
        cut = spl - quietest    # always >= 0
        dsp_volume = -cut       # always <= 0
        bar = "▔" * int(cut * 2)
        marker = "  ← reference" if abs(cut) < 0.05 else ""
        print(f"{label:<10}  {spl:+6.1f}   -{cut:5.1f} dB   "
              f"{dsp_volume:+7.1f} dB  {bar}{marker}")

    spread = loudest - quietest
    print()
    print(f"Spread (loudest − quietest): {spread:.1f} dB")
    print(f"Loudest: {max(results, key=lambda r: r[1])[0]}   "
          f"Quietest: {min(results, key=lambda r: r[1])[0]}")
    print()
    print("Set each speaker's DSP-408 per-channel volume to the right column to flatten.")
    print("(DSP volume can only attenuate — 0 dB max, -60 dB min.)")
    print("Note: bipolar room-fill setups often prefer rears 3-6 dB quieter than fronts;")
    print("flattening fully may not be subjectively desirable.")


if __name__ == "__main__":
    main()
