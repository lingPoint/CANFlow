"""Generate a random BLF using the frame IDs and lengths in a DBC file."""

import argparse
import json
import random
import re
import time
from pathlib import Path

import can


MESSAGE = re.compile(r"^BO_\s+(\d+)\s+(\S+):\s+(\d+)\s+", re.MULTILINE)


def generate(dbc_path: Path, output: Path, target_bytes: int, seed: int) -> dict:
    definitions = []
    for raw_id, name, raw_length in MESSAGE.findall(dbc_path.read_text(encoding="utf-8-sig")):
        dbc_id, length = int(raw_id), int(raw_length)
        if not 0 <= length <= 64:
            continue
        extended = bool(dbc_id & 0x80000000)
        frame_id = dbc_id & 0x1FFFFFFF if extended else dbc_id
        if frame_id > (0x1FFFFFFF if extended else 0x7FF):
            continue
        definitions.append((frame_id, name, length, extended))
    if not definitions:
        raise ValueError("DBC contains no usable CAN messages")

    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    counts = {name: 0 for _, name, _, _ in definitions}
    frames = 0
    started = time.time()
    with can.BLFWriter(output, compression_level=0) as writer:
        while writer.file.tell() < target_bytes:
            for _ in range(10_000):
                frame_id, name, length, extended = definitions[frames % len(definitions)]
                writer.on_message_received(can.Message(
                    timestamp=started + frames * 0.001,
                    channel=1,
                    arbitration_id=frame_id,
                    is_extended_id=extended,
                    is_fd=length > 8,
                    bitrate_switch=length > 8,
                    data=rng.randbytes(length),
                ))
                counts[name] += 1
                frames += 1
    return {
        "output": str(output), "bytes": output.stat().st_size,
        "frames": frames, "seed": seed, "dbc": str(dbc_path),
        "message_counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dbc", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size-mb", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260924)
    args = parser.parse_args()
    print(json.dumps(generate(args.dbc, args.output, args.size_mb * 1_000_000, args.seed), indent=2))


if __name__ == "__main__":
    main()
