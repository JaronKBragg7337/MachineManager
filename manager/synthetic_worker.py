"""Small deterministic worker used for manager reliability evaluations."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def write_heartbeat(path: Path, tick: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"tick": tick, "updated": time.time()}, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("run", "stall", "crash"), required=True)
    parser.add_argument("--heartbeat-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    args = parser.parse_args()

    if args.mode == "crash":
        return 17

    tick = 0
    write_heartbeat(args.heartbeat_file, tick)
    if args.mode == "stall":
        while True:
            time.sleep(60)

    while True:
        tick += 1
        write_heartbeat(args.heartbeat_file, tick)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
