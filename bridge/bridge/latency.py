"""Measure the PC-to-Switch round trip in frames (SSB-7).

    python -m bridge.latency --seconds 60 --bot-port 2

Runs a trivial policy (wiggles the stick every 30 frames, so you can see it's
alive), collects the plugin's acks, and prints:

- round trip: ready_frame - for_frame. ready_frame is the first frame the
  input could have taken effect on, given when it reached the Switch. This
  is the real latency and doesn't depend on --delay.
- applied delay: applied_frame - for_frame. The plugin drops late input, so
  this always equals --delay for inputs that made it; it's a sanity check.
- late (the Switch dropped it, apply_at had passed) and lost (sent, never
  acked: dropped on the way there, late, or the ack got lost).

The 99th percentile round trip is what sets the training delay. The default
--delay is big on purpose so nothing is late and every input gets measured.

Needs a match running (training mode is fine). Stop udp_listen and
bridge.run first, they use the same port.
"""
from __future__ import annotations

import argparse
import statistics
import time
from collections import Counter
from typing import Sequence

from .protocol import Frame, Inputs
from .run import Bridge, percentile


class WigglePolicy:
    """Tilts the stick a little left then right every 30 frames."""

    def act(self, history: Sequence[Frame]) -> Inputs:
        f = history[-1].frame
        return Inputs(stick_x=30 if (f // 30) % 2 else -30)


def histogram(values: list[int], width: int = 40) -> list[str]:
    counts = Counter(values)
    top = max(counts.values())
    return [f"  {v:>3} frames  {counts[v]:>6}  {'#' * max(1, round(width * counts[v] / top))}"
            for v in sorted(counts)]


def report(b: Bridge, seconds: float) -> str:
    lines = [f"[latency] {seconds:.0f} s, delay {b.delay}, bot port {b.bot_port}"]
    rt = b.acks.round_trips
    lines.append(f"  inputs sent    {b.inputs_sent}")
    lines.append(f"  acks           {len(rt)}")
    # The plugin's counters run since boot; only count what changed during this run.
    # They come with each heartbeat, so they can trail by up to 500 ms.
    s, s0 = b.status, b.first_status
    late = s.late - s0.late if s and s0 else 0
    # Inputs answering frames after the last ack were still in flight when we
    # stopped (their apply_at hadn't come yet), so they don't count as lost.
    last = max(b.acks.seen) if b.acks.seen else -1
    settled = sum(1 for f in b.sent_frames if f <= last)
    lost = max(0, settled - len(rt) - late)
    lines.append(f"  late (Switch)  {late}" + ("" if s else "  (no control status received, so unknown)"))
    lines.append(f"  lost           {lost}  (sent, never acked, not late: dropped on the way or ack lost)")
    lines.append(f"  state lost     {b.seqs.missing} of {b.seqs.received + b.seqs.missing} packets ({b.seqs.loss_pct:.2f}%)")
    if not rt:
        lines.append("  no acks: is a match running, and is the bridge's --bot-port the plugin's bot_port?")
        return "\n".join(lines)
    lines.append(
        f"  round trip     median {statistics.median_low(rt)}  p99 {percentile(rt, 99)}  "
        f"min {min(rt)}  max {max(rt)} frames"
    )
    d = b.acks.delays
    lines.append(f"  applied delay  median {statistics.median_low(d)}  p99 {percentile(d, 99)} frames (should equal --delay)")
    lines.append("  round trip histogram:")
    lines.extend(histogram(rt))
    p99 = percentile(rt, 99)
    lines.append(f"  => train with delay up to {p99} frames; run the bot with --delay {p99} or more")
    return "\n".join(lines)


def measure(port: int, seconds: float, delay: int, bot_port: int, bind: str = "0.0.0.0",
            quiet: bool = False) -> Bridge:
    log = (lambda _m: None) if quiet else print
    b = Bridge(port, WigglePolicy(), delay, bot_port, bind=bind, log=log)
    b.sent_frames = []
    log(f"[latency] measuring for {seconds:.0f} s on UDP :{b.port}, delay {delay}")
    start = time.monotonic()
    first_ack_warned = False
    try:
        while time.monotonic() - start < seconds:
            b.poll(0.1)
            if not first_ack_warned and b.inputs_sent > 300 and not b.acks.round_trips:
                first_ack_warned = True
                log("[latency] 300 inputs sent and no acks yet. Check --bot-port and that the input hook is installed.")
    except KeyboardInterrupt:
        log("[latency] stopped early")
    finally:
        b.close()
    return b


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--delay", type=int, default=10, help="apply_at = state frame + this")
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2))
    args = ap.parse_args()
    t0 = time.monotonic()
    b = measure(args.port, args.seconds, args.delay, args.bot_port)
    print(report(b, time.monotonic() - t0))


if __name__ == "__main__":
    main()
