"""Capture the real CANFlow window with generated, non-sensitive CAN data."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PySide6 import QtCore, QtGui, QtWidgets

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from canflow.core import SignalKey, available_signals, load_databases, prepare_sequence


def make_demo(folder: Path) -> tuple[Path, Path]:
    dbc = folder / "demo_signals.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 256 VehicleStatus: 8 ECU\n'
        ' SG_ BatterySOC : 0|8@1+ (1,0) [0|100] "%" ECU\n'
        ' SG_ InverterTemp : 8|8@1+ (1,0) [0|150] "degC" ECU\n'
        ' SG_ BrakePressure : 16|8@1+ (1,0) [0|100] "bar" ECU\n',
        encoding="utf-8",
    )
    blf = folder / "demo_capture.blf"
    start = datetime(2026, 9, 20, 9, 0, 0).timestamp()
    with can.BLFWriter(blf) as writer:
        for index in range(301):
            seconds = index / 10
            soc = round(56 + 12 * seconds / 30)
            temperature = round(35 + 9 * math.sin(seconds / 3))
            pressure = round(12 + 28 * ((seconds % 7) / 7))
            writer.on_message_received(can.Message(
                timestamp=start + seconds,
                channel=1,
                arbitration_id=0x100,
                data=[soc, temperature, pressure, 0, 0, 0, 0, 0],
                is_extended_id=False,
            ))
    return blf, dbc


def capture(output: Path, *, time_mode: str = "relative") -> None:
    from scripts.capture_redesign import capture as capture_workspace
    capture_workspace(output.parent)


if __name__ == "__main__":
    capture(PROJECT / "docs" / "images" / "canflow-screenshot.png")
