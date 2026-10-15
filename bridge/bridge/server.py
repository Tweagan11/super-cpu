"""TCP bridge (SSB-5). The plugin connects, sends one state message per
frame, and gets one input message back.

    python -m bridge.server --port 4242 --policy walk
"""
from __future__ import annotations

import argparse
import socket
import time

from .policy import History, IdlePolicy, Policy, WalkTowardPolicy
from .protocol import FRAME_SIZE, decode_frame, encode_inputs

POLICIES: dict[str, type] = {"idle": IdlePolicy, "walk": WalkTowardPolicy}


def recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def serve(port: int, policy: Policy) -> None:
    with socket.create_server(("0.0.0.0", port)) as srv:
        print(f"[bridge] listening on :{port}")
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"[bridge] plugin connected from {addr[0]}")
            history = History()
            frames, t0 = 0, time.perf_counter()
            with conn:
                while (raw := recv_exact(conn, FRAME_SIZE)) is not None:
                    frame = decode_frame(raw)
                    inputs = policy.act(history.push(frame))
                    conn.sendall(encode_inputs(inputs, frame.frame))
                    frames += 1
                    if frames % 600 == 0:
                        rate = frames / (time.perf_counter() - t0)
                        print(f"[bridge] {rate:.1f} msg/s, last frame {frame.frame}")
            print("[bridge] plugin disconnected, waiting for reconnect")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--policy", choices=sorted(POLICIES), default="idle")
    args = ap.parse_args()
    serve(args.port, POLICIES[args.policy]())


if __name__ == "__main__":
    main()
