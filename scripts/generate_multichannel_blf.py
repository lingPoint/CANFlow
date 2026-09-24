"""Generate reproducible mixed CAN/CAN FD BLF data for multichannel replay tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import can


def fixture_paths(blf: Path) -> tuple[Path, Path, Path]:
    return tuple(blf.with_name(f"{blf.stem}.{suffix}") for suffix in (
        "ch1.dbc", "ch2.dbc", "manifest.json",
    ))


def generate_multichannel_blf(blf: Path, target_bytes: int) -> dict:
    """Write a BLF with two mapped channels and one unmapped channel."""
    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")
    blf.parent.mkdir(parents=True, exist_ok=True)
    ch1_dbc, ch2_dbc, manifest_path = fixture_paths(blf)
    manifest_path.unlink(missing_ok=True)
    for path, scale in ((ch1_dbc, 1), (ch2_dbc, 2)):
        path.write_text(
            'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
            f'BO_ 291 Selected: 64 ECU\n SG_ Speed : 0|8@1+ ({scale},0) [0|510] "km/h" ECU\n',
            encoding="utf-8",
        )

    fd_payload = bytes([7, *range(1, 64)])
    classic_payload = fd_payload[:8]
    channels = {1: 0, 2: 0, 3: 0}
    selected = {1: 0, 2: 0}
    unmapped_same_id = 0
    classic = 0
    frame_count = 0
    with can.BLFWriter(blf, compression_level=0) as writer:
        while writer.file.tell() < target_bytes:
            for _ in range(10_000):
                slot = frame_count % 1000
                if slot < 3:
                    channel = slot + 1
                    frame_id = 0x123
                    is_fd = True
                else:
                    channel = frame_count % 3 + 1
                    frame_id = 0x124
                    is_fd = frame_count % 10 != 0
                writer.on_message_received(can.Message(
                    timestamp=1_700_000_000 + frame_count * 0.0001,
                    channel=channel,
                    arbitration_id=frame_id,
                    data=fd_payload if is_fd else classic_payload,
                    is_fd=is_fd,
                    is_extended_id=False,
                ))
                channels[channel] += 1
                if slot < 2:
                    selected[channel] += 1
                elif slot == 2:
                    unmapped_same_id += 1
                if not is_fd:
                    classic += 1
                frame_count += 1
    manifest = {
        "blf": blf.name,
        "bytes": blf.stat().st_size,
        "frames": frame_count,
        "channels": channels,
        "selected_frames": selected,
        "unmapped_same_id_frames": unmapped_same_id,
        "classic_frames": classic,
        "dbc": {"1": ch1_dbc.name, "2": ch2_dbc.name},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="output BLF path")
    parser.add_argument("--size-mib", type=int, default=1024, help="minimum BLF size in MiB")
    args = parser.parse_args()
    summary = generate_multichannel_blf(args.output, args.size_mib << 20)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
