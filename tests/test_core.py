from pathlib import Path

import can
import cantools
import pytest

from canflow.core import SignalKey, decode_selected, inspect_file, prepare_sequence
from canflow.store import connect, plot_points, write_samples
from canflow.workers import ReplayWorker


def make_blf(path: Path, timestamps: list[float]) -> None:
    with can.BLFWriter(path) as writer:
        for timestamp in timestamps:
            writer.on_message_received(
                can.Message(timestamp=timestamp, channel=1, arbitration_id=0x123, data=[10], is_fd=False,
                            is_extended_id=False)
            )


def test_sequence_sorts_and_allows_equal_boundary(tmp_path: Path) -> None:
    first = tmp_path / "z.blf"
    second = tmp_path / "a.blf"
    make_blf(first, [1_700_000_000, 1_700_000_001])
    make_blf(second, [1_700_000_001, 1_700_000_003])
    files = prepare_sequence([second, first])
    assert [item.path for item in files] == [first, second]
    assert [item.frames for item in files] == [2, 2]
    assert files[0].channels == (1,)


def test_sequence_rejects_overlap(tmp_path: Path) -> None:
    first = tmp_path / "first.blf"
    second = tmp_path / "second.blf"
    make_blf(first, [1_700_000_000, 1_700_000_002])
    make_blf(second, [1_700_000_001, 1_700_000_003])
    with pytest.raises(ValueError, match="时间重叠"):
        prepare_sequence([first, second])


def test_can_fd_frame_is_scanned(tmp_path: Path) -> None:
    path = tmp_path / "fd.blf"
    with can.BLFWriter(path) as writer:
        writer.on_message_received(can.Message(
            timestamp=1_700_000_000, channel=2, arbitration_id=0x456,
            data=bytes(range(12)), is_fd=True, is_extended_id=False,
        ))
    info = inspect_file(path)
    assert info.frames == 1
    assert info.channels == (2,)
    with can.BLFReader(path) as reader:
        assert next(iter(reader)).is_fd


def test_dbc_decode_only_selected_signal(tmp_path: Path) -> None:
    dbc = tmp_path / "one.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ (0.5,0) [0|127.5] "km/h" ECU\n',
        encoding="utf-8",
    )
    database = cantools.database.load_file(str(dbc))
    key = SignalKey(1, 0x123, False, "Speed")
    message = can.Message(timestamp=1_700_000_000, channel=1, arbitration_id=0x123, data=[10], is_extended_id=False)
    assert decode_selected(message, database, {key}) == [(key, 5.0)]
    assert decode_selected(message, None, {key}) == []


def test_store_preserves_extrema_in_visible_bins(tmp_path: Path) -> None:
    connection = connect(tmp_path / "cache.sqlite")
    write_samples(connection, [("speed", 1.0, 3.0), ("speed", 1.1, 9.0), ("speed", 2.0, 4.0)])
    x, y = plot_points(connection, "speed", 0, 3, max_bins=2)
    assert 3.0 in y and 9.0 in y and 4.0 in y
    assert len(x) == len(y)
    connection.close()


def test_replay_worker_decodes_to_disk_and_reports_raw_frames(tmp_path: Path) -> None:
    blf = tmp_path / "frames.blf"
    make_blf(blf, [1_700_000_000, 1_700_000_001])
    dbc = tmp_path / "one.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ (0.5,0) [0|127.5] "km/h" ECU\n',
        encoding="utf-8",
    )
    key = SignalKey(1, 0x123, False, "Speed")
    cache = tmp_path / "samples.sqlite"
    worker = ReplayWorker(prepare_sequence([blf]), {1: dbc}, {key}, cache)
    updates = []
    outcomes = []
    worker.advanced.connect(updates.append)
    worker.completed.connect(outcomes.append)
    worker.run()
    assert outcomes == [True]
    assert updates[-1][1:3] == (2, 2)
    assert len(updates[-1][4]) == 2
    connection = connect(cache)
    assert connection.execute("SELECT COUNT(*) FROM samples WHERE signal = ?", (key.storage_key(),)).fetchone()[0] == 2
    connection.close()


def test_short_can_frames_decode_signals_present_in_dbc_fd_message(
    tmp_path: Path, sample_recording: tuple[Path, Path]
) -> None:
    blf, dbc = sample_recording
    files = prepare_sequence([blf])
    key = SignalKey(1, 0x64, False, "BatterySOC")
    cache = tmp_path / "samples.sqlite"
    worker = ReplayWorker(files, {1: dbc}, {key}, cache)
    errors = []
    worker.failed.connect(errors.append)
    worker.run()
    assert errors == []
    connection = connect(cache)
    count = connection.execute("SELECT COUNT(*) FROM samples WHERE signal = ?", (key.storage_key(),)).fetchone()[0]
    connection.close()
    assert count > 0
