"""Place fighters by hand: the hardware check for drill resets (SSB-31/32).

    python -m bridge.reset --p1 "120,-30,air,dmg=60,jumps=1,face=left"
    python -m bridge.reset --p1 "-40,0,ground" --p2 "40,0,ground,face=left" --repeat 5 --every 3

Each spec is `x,y` then any of: `air` or `ground` (default air), `dmg=N`
(percent; default keep), `jumps=N` (jumps left; default keep),
`face=left|right` (default keep). Positions are stage units; on Final
Destination the ledges are at about x = +-80, the stage top at y = 0.

Needs `drills = true` in the plugin config and the game in training mode
(or `drills_in_versus = true`). Prints the reset-done reply and where each
fighter actually is 1, 10 and 30 frames later, plus the plugin's log lines.
Runs instead of bridge.run (same UDP port), with no inputs sent.
"""
from __future__ import annotations

import argparse
import sys
import time

from .matches import fighters
from .run import Bridge
from .policy import POLICIES
from .wire import PortReset, ResetDone


def parse_spec(spec: str) -> PortReset:
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(f"{spec!r}: needs at least x,y")
    try:
        x, y = float(parts[0]), float(parts[1])
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{spec!r}: x and y must be numbers") from e
    air, dmg, jumps, face = True, -1.0, -1, 0
    for p in parts[2:]:
        k, _, v = p.partition("=")
        k = k.lower()
        if k in ("air", "ground") and not v:
            air = k == "air"
        elif k in ("dmg", "damage"):
            dmg = float(v)
        elif k == "jumps":
            jumps = int(v)
        elif k == "face":
            face = {"left": -1, "l": -1, "right": 1, "r": 1}.get(v.lower(), 0)
        else:
            raise argparse.ArgumentTypeError(f"{spec!r}: don't know {p!r}")
    return PortReset(x, y, air=air, facing=face, jumps_left=jumps, damage=dmg)


def describe(f) -> str:
    return (f"x {f.pos_x:7.1f} y {f.pos_y:7.1f} dmg {f.damage:5.1f}% jumps {f.jumps_left} "
            f"face {'R' if f.facing > 0 else 'L'} status {f.status_kind}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--p1", type=parse_spec, default=None, help="where to put port 1")
    ap.add_argument("--p2", type=parse_spec, default=None, help="where to put port 2")
    ap.add_argument("--repeat", type=int, default=1, help="send the reset this many times")
    ap.add_argument("--every", type=float, default=3.0, help="seconds between repeats")
    ap.add_argument("--wait", type=float, default=30.0, help="give up waiting for the Switch after this long")
    # "--p1 -40,0" would read -40,0 as an option: glue the value on
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1):
        if argv[i] in ("--p1", "--p2"):
            argv[i:i + 2] = [f"{argv[i]}={argv[i + 1]}", ""]
    args = ap.parse_args([a for a in argv if a != ""])
    if args.p1 is None and args.p2 is None:
        ap.error("give --p1 and/or --p2")

    b = Bridge(args.port, POLICIES["watch"](), delay=3, bot_port=2)
    done: list[tuple[float, ResetDone]] = []
    b.on_reset_done = lambda d: done.append((time.monotonic(), d))
    print(f"[reset] waiting for the Switch on UDP :{args.port}")
    try:
        t0 = time.monotonic()
        while b.switch is None or not b.history._frames:
            b.poll(0.1)
            if time.monotonic() - t0 > args.wait:
                print("[reset] no state from the Switch: is a match or training mode running, and is the IP right?")
                return 1
        for i in range(args.repeat):
            seq = b.send_reset(args.p1, args.p2)
            sent_at = time.monotonic()
            print(f"[reset] #{seq} sent")
            reply = None
            checks = {1: None, 10: None, 30: None}
            first_frame = None
            while time.monotonic() - sent_at < max(args.every, 1.5):
                b.poll(0.02)
                if reply is None and done and done[-1][1].reset_seq == seq:
                    reply = done[-1][1]
                    ms = 1000 * (done[-1][0] - sent_at)
                    if reply.refused:
                        print(f"[reset] #{seq} REFUSED ({'training mode' if reply.training else 'not training mode'}); see the [switch] line above")
                        break
                    print(f"[reset] #{seq} placed in {ms:.0f} ms, ports mask {reply.placed_mask:#04b}, "
                          f"{'training mode' if reply.training else 'versus'}")
                if reply is not None and b.history._frames:
                    fr = b.history._frames[-1]
                    if first_frame is None:
                        first_frame = fr.frame
                    k = fr.frame - first_frame + 1
                    for n in checks:
                        if checks[n] is None and k >= n:
                            checks[n] = fr
                            for port, (want, f) in enumerate(zip((args.p1, args.p2), fighters(fr)), start=1):
                                if want is not None:
                                    print(f"    +{n:2d} frames P{port}: {describe(f)}")
            if reply is None:
                print(f"[reset] #{seq}: no reply. Rebuilt plugin with drills, and drills = true in config.toml?")
            elif not reply.refused and args.p1 is not None and checks[1] is not None:
                f = fighters(checks[1])[0]
                off = abs(f.pos_x - args.p1.x) + abs(f.pos_y - args.p1.y)
                print(f"    P1 landed {off:.1f} units from where it was asked" + (" (OK)" if off < 5 else " (check this)"))
    except KeyboardInterrupt:
        pass
    finally:
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
