from pathlib import Path

import can
import pytest


@pytest.fixture
def sample_recording(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a short CAN recording with a longer DBC message definition."""
    dbc = tmp_path / "sample.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 100 INV_Data: 64 ECU\n'
        ' SG_ InvTemp : 16|7@1+ (2,-50) [-50|150] "degC" ECU\n'
        ' SG_ BatterySOC : 24|8@1+ (1,0) [0|255] "%" ECU\n'
        ' SG_ MotorTorque : 64|32@1- (1,0) [0|0] "N" ECU\n'
        ' SG_ MotorSpeed : 96|16@1- (1,0) [-32768|32767] "rpm" ECU\n',
        encoding="utf-8",
    )
    blf = tmp_path / "sample.blf"
    with can.BLFWriter(blf) as writer:
        for index in range(20):
            writer.on_message_received(can.Message(
                timestamp=1_700_000_000 + index * 0.1,
                channel=1,
                arbitration_id=0x64,
                data=[0, 0, index, index + 1, 0, 0, 0, 0],
                is_extended_id=False,
            ))
    return blf, dbc
