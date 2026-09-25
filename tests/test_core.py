from pathlib import Path

import can
import cantools
import pytest

from canflow.core import (
    FileInfo, SignalKey, decode_selected, inspect_file, missing_signal_reason,
    prepare_sequence, signal_definition_fingerprint, signal_unit,
)
from canflow.store import connect, plot_points, plot_points_with_samples, write_samples
from canflow.workers import ReplayWorker, ScanWorker


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


def test_multichannel_timestamp_rollback_keeps_capture_times_and_file_bounds(tmp_path: Path) -> None:
    first = tmp_path / "mixed.blf"
    base = 1_790_336_051.127
    frames = [
        (base + 0.000010, 1, 10),
        (base + 0.000612, 1, 11),
        (base + 0.000609, 2, 12),
        (base, 2, 13),
        (base + 0.000300, 1, 14),
    ]
    with can.BLFWriter(first) as writer:
        for timestamp, channel, value in frames:
            writer.on_message_received(can.Message(
                timestamp=timestamp, channel=channel, arbitration_id=0x123,
                data=[value], is_extended_id=False,
            ))
    with can.BLFReader(first) as reader:
        recorded = [(float(message.timestamp), int(message.channel), message.data[0]) for message in reader]
    assert recorded[2][0] < recorded[1][0]  # The BLF actually retains the rollback.

    info = inspect_file(first)
    assert info.frames == len(frames)
    assert info.channels == (1, 2)
    assert info.first == min(item[0] for item in recorded)
    assert info.last == max(item[0] for item in recorded)

    scanned = []
    scan_errors = []
    scanner = ScanWorker([first])
    scanner.scanned.connect(scanned.append)
    scanner.failed.connect(scan_errors.append)
    scanner.run()
    assert scan_errors == []
    assert scanned == [[info]]

    cache = tmp_path / "samples.sqlite"
    worker = ReplayWorker([info], {}, set(), cache)
    updates = []
    outcomes = []
    worker.advanced.connect(updates.append)
    worker.completed.connect(outcomes.append)
    worker.run()
    assert outcomes == [True]
    assert updates[-1][1:4] == (len(frames), len(frames), info.last)
    assert [(row[0], row[1], int(row[4], 16)) for row in updates[-1][4]] == recorded

    second = tmp_path / "later.blf"
    make_blf(second, [info.last + 0.000001])
    assert [item.path for item in prepare_sequence([second, first])] == [first, second]
    make_blf(second, [info.last - 0.000001])
    with pytest.raises(ValueError, match="时间重叠"):
        prepare_sequence([first, second])


def test_background_scan_uses_same_sequence_rules(tmp_path: Path) -> None:
    first = tmp_path / "first.blf"
    second = tmp_path / "second.blf"
    make_blf(first, [1_700_000_000, 1_700_000_002])
    make_blf(second, [1_700_000_002, 1_700_000_003])
    worker = ScanWorker([second, first])
    scanned = []
    errors = []
    worker.scanned.connect(scanned.append)
    worker.failed.connect(errors.append)
    worker.run()
    assert errors == []
    assert [item.path for item in scanned[0]] == [first, second]

    make_blf(second, [1_700_000_001, 1_700_000_003])
    worker = ScanWorker([first, second])
    worker.failed.connect(errors.append)
    worker.run()
    assert "时间重叠" in errors[-1]


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


def test_signal_lookup_respects_standard_vs_extended_frame(tmp_path: Path) -> None:
    dbc = tmp_path / "extended.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 2147483939 Extended: 8 ECU\n'
        ' SG_ Speed : 40|8@1+ (1,0) [0|255] "km/h" ECU\n',
        encoding="utf-8",
    )
    database = cantools.database.load_file(str(dbc))
    key = SignalKey(1, 0x123, False, "Speed")
    databases = {1: database}
    info = FileInfo(tmp_path / "sample.blf", 0, 1, 1, (1,), ((1, 0x123, False, 1),))
    assert signal_definition_fingerprint(key, databases) is None
    assert signal_unit(key, databases) == ""
    assert missing_signal_reason(key, [info], databases) == "未解码出样本，请检查 DBC 与报文内容"


def test_store_preserves_extrema_in_visible_bins(tmp_path: Path) -> None:
    connection = connect(tmp_path / "cache.sqlite")
    write_samples(connection, [("speed", 1.0, 3.0), ("speed", 1.1, 9.0), ("speed", 2.0, 4.0)])
    x, y = plot_points(connection, "speed", 0, 3, max_bins=2)
    assert 3.0 in y and 9.0 in y and 4.0 in y
    assert len(x) == len(y)
    connection.close()


def test_plot_sample_markers_are_real_recorded_frames(tmp_path: Path) -> None:
    connection = connect(tmp_path / "cache.sqlite")
    samples = [("speed", 1.0, 3.0), ("speed", 1.1, 9.0), ("speed", 2.0, 4.0)]
    write_samples(connection, samples)
    _, _, marker_x, marker_y = plot_points_with_samples(connection, "speed", 0, 3, max_bins=2)
    assert marker_x
    assert set(zip(marker_x, marker_y)) <= {(timestamp, value) for _, timestamp, value in samples}
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


def test_replay_flushes_samples_without_flooding_progress(tmp_path: Path, monkeypatch) -> None:
    blf = tmp_path / "many.blf"
    make_blf(blf, [1_700_000_000 + index * 0.001 for index in range(34500)])
    dbc = tmp_path / "one.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ (1,0) [0|255] "" ECU\n',
        encoding="utf-8",
    )
    key = SignalKey(1, 0x123, False, "Speed")
    cache = tmp_path / "samples.sqlite"
    import canflow.workers as workers

    writes = []
    original_write = workers.write_samples

    def record_write(connection, rows):
        if rows:
            writes.append(len(rows))
        original_write(connection, rows)

    monkeypatch.setattr(workers, "write_samples", record_write)
    monkeypatch.setattr(workers.time, "monotonic", lambda: 100.0)
    worker = ReplayWorker(prepare_sequence([blf]), {1: dbc}, {key}, cache)
    updates = []
    worker.advanced.connect(updates.append)
    worker.run()
    assert worker.succeeded
    assert len(writes) >= 3
    assert len(updates) == 1
    assert max(writes) <= 16000
    assert updates[0][:3] == (0, 34500, 34500)
    assert updates[0][3] == pytest.approx(1_700_000_034.499)
    assert len(updates[0][4]) == 1000
    connection = connect(cache)
    assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 34500
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
