"""Correctness gates for V3's accelerated paths; timing lives in benchmark_v3."""
import can
import pytest

from canflow.core import SignalKey, decode_selected, load_databases
from canflow.store import connect, plot_points_with_samples, write_samples


def test_overview_preserves_spikes_real_markers_and_exact_view_edges(tmp_path):
    from canflow.store import configure_overview
    db = connect(tmp_path / "cache.sqlite")
    configure_overview(db, 1000, 1100)
    rows = [("s", 1000 + i * 0.001, float(i % 101)) for i in range(100001)]
    rows[50001] = ("s", 1050.001, 9999.)
    # Write out of timestamp order, and repeat a batch as seek/backfill does.
    for offset in range(0, len(rows), 2000):
        write_samples(db, list(reversed(rows[offset:offset + 2000])))
    write_samples(db, rows[:2000])
    for start, end in ((1000., 1100.), (1050.0011, 1099.999), (1050., 1050.002)):
        x, y, sx, sy = plot_points_with_samples(db, "s", start, end, 200)
        expected = {(ts, value) for _, ts, value in rows if start <= ts <= end}
        assert min(y) == min(value for _, value in expected)
        assert max(y) == max(value for _, value in expected)
        assert set(zip(sx, sy)) <= expected
        assert len(x) <= 402
        assert all(start <= ts <= end for ts in x)
    statements = []
    db.set_trace_callback(statements.append)
    plot_points_with_samples(db, "s", 1000, 1100)
    assert any("overview" in sql for sql in statements)
    db.close()


def test_overview_reopen_backfill_and_concurrent_reader(tmp_path):
    from canflow.store import configure_overview
    path = tmp_path / "cache.sqlite"
    reader = connect(path)
    writer = connect(path)
    configure_overview(writer, 0, 100)
    write_samples(writer, [("s", 50., 7.)])
    assert plot_points_with_samples(reader, "s", 0, 100)[1] == [7.]
    writer.close()
    writer = connect(path)
    configure_overview(writer, 0, 100)
    write_samples(writer, [("s", 49., -9.), ("s", 50., 12.)])
    _, values, sx, sy = plot_points_with_samples(reader, "s", 0, 100)
    assert min(values) == -9 and max(values) == 12
    assert set(zip(sx, sy)) <= {(49., -9.), (50., 7.), (50., 12.)}
    reader.close()
    writer.close()


def test_prepared_decoder_matches_reference_for_short_and_unmapped_frames(sample_recording):
    from canflow.core import prepare_decoders
    _, dbc = sample_recording
    databases = load_databases({channel: dbc for channel in range(1, 17)})
    selected = {SignalKey(channel, 0x64, False, name) for channel in range(1, 17)
                for name in ("BatterySOC", "MotorTorque")}
    decoders = prepare_decoders(databases, selected)
    for channel in range(1, 18):
        for length in (0, 3, 8, 16, 64):
            for extended in (False, True):
                msg = can.Message(channel=channel, arbitration_id=0x64, is_extended_id=extended,
                                  is_fd=length > 8, data=bytes(range(length)))
                decoder = decoders.get((channel, 0x64, extended))
                actual = sorted(decoder(msg.data) if decoder else [])
                expected = sorted((key.storage_key(), value) for key, value in
                                  decode_selected(msg, databases.get(channel), selected))
                assert actual == expected


def test_invalid_bin_count(tmp_path):
    db = connect(tmp_path / "cache.sqlite")
    with pytest.raises(ValueError):
        plot_points_with_samples(db, "s", 0, 1, 0)
    db.close()


@pytest.mark.parametrize("definition", [
    ' SG_ U : 3|13@1+ (0.25,-20) [0|0] "" ECU\n'
    ' SG_ S : 17|29@1- (2,-10) [0|0] "" ECU\n'
    ' SG_ Wide : 64|64@1- (1,0) [0|0] "" ECU\n',
    ' SG_ Motor : 7|16@0- (0.1,-5) [0|0] "" ECU\n',
    ' SG_ Mux M : 0|4@1+ (1,0) [0|15] "" ECU\n'
    ' SG_ Sub m1 : 8|8@1+ (2,0) [0|510] "" ECU\n',
    ' SG_ Float : 0|32@1+ (1,0) [0|0] "" ECU\nSIG_VALTYPE_ 291 Float : 1;\n',
])
def test_prepared_decoder_differential_payloads(tmp_path, definition):
    import math
    import random
    from canflow.core import available_signals, prepare_decoders
    dbc = tmp_path / "types.dbc"
    dbc.write_text('VERSION ""\nNS_ :\nBS_: \nBU_: ECU\nBO_ 291 Types: 16 ECU\n' + definition)
    databases = load_databases({1: dbc})
    keys = set(available_signals(databases))
    decoder = prepare_decoders(databases, keys)[1, 0x123, False]
    randomizer = random.Random(42)
    for _ in range(1000):
        length = randomizer.randrange(0, 25)
        data = randomizer.randbytes(length)
        msg = can.Message(channel=1, arbitration_id=0x123, is_extended_id=False, data=data)
        actual = dict(decoder(data))
        expected = {key.storage_key(): value for key, value in decode_selected(msg, databases[1], keys)}
        assert actual.keys() == expected.keys()
        for storage, value in actual.items():
            assert value == expected[storage] or (math.isnan(value) and math.isnan(expected[storage]))


def test_replay_latest_snapshot_is_bounded_and_final_even_without_ui_consumption(tmp_path, sample_recording):
    from canflow.core import inspect_file
    from canflow.workers import ReplayWorker
    blf, dbc = sample_recording
    info = inspect_file(blf)
    worker = ReplayWorker([info], {1: dbc}, {SignalKey(1, 0x64, False, "BatterySOC")},
                          tmp_path / "cache.sqlite", poll_updates=True)
    queued = []
    worker.advanced.connect(queued.append)
    worker.run()
    assert worker.succeeded
    assert queued == []
    data = worker.take_update()
    assert data[1:3] == (info.frames, info.frames)
    assert len(data[4]) == info.frames
    assert worker.take_update() is None


def test_scan_cache_reuses_unchanged_files_and_invalidates_replaced_file(sample_recording, monkeypatch):
    import os
    import canflow.workers as module
    blf, _ = sample_recording
    calls = []
    original = module.inspect_file

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(module, "inspect_file", counted)
    cache = module.ScanCache()
    first = cache.inspect(blf, lambda: False)
    assert cache.inspect(blf, lambda: False) is first
    assert len(calls) == 1
    stat = blf.stat()
    os.utime(blf, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    assert cache.inspect(blf, lambda: False) == first
    assert len(calls) == 2
    with pytest.raises(InterruptedError):
        cache.inspect(blf, lambda: True)


def test_seek_skips_whole_earlier_files_without_losing_total_progress(tmp_path, monkeypatch):
    from canflow.core import inspect_file
    from canflow.workers import ReplayWorker
    files = []
    for index in range(2):
        path = tmp_path / f"{index}.blf"
        with can.BLFWriter(path) as writer:
            for i in range(3):
                writer.on_message_received(can.Message(timestamp=1700000000 + index * 10 + i,
                    channel=1, arbitration_id=1, data=[i], is_extended_id=False))
        files.append(inspect_file(path))
    opened = []
    original = can.BLFReader

    def tracked(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(can, "BLFReader", tracked)
    worker = ReplayWorker(files, {}, set(), tmp_path / "cache.sqlite", start_at=files[1].first)
    updates = []
    worker.advanced.connect(updates.append)
    worker.run()
    assert worker.succeeded
    assert opened == [files[1].path]
    assert updates[-1][1:4] == (6, 6, files[1].last)
    assert len(updates[-1][4]) == 3


def test_stop_unblocks_paused_replay(tmp_path, sample_recording):
    from canflow.core import inspect_file
    from canflow.workers import ReplayWorker
    blf, _ = sample_recording
    worker = ReplayWorker([inspect_file(blf)], {}, set(), tmp_path / "cache.sqlite")
    worker.set_paused(True)
    worker.start()
    try:
        assert not worker.wait(50)
    finally:
        worker.stop()
        assert worker.wait(2000)
    assert not worker.succeeded


def test_overview_random_epoch_windows_match_raw_extrema(tmp_path):
    import random
    from canflow.store import configure_overview
    rng = random.Random(7)
    origin = 1700000000.
    db = connect(tmp_path / "cache.sqlite")
    configure_overview(db, origin, origin + 10)
    rows = [("s", origin + rng.random() * 10, rng.uniform(-100, 100)) for _ in range(10000)]
    write_samples(db, rows)
    for _ in range(100):
        start, end = sorted(origin + rng.random() * 10 for _ in range(2))
        max_bins = rng.choice([1, 2, 16, 200, 1600])
        expected = {(ts, value) for _, ts, value in rows if start <= ts <= end}
        x, y, sx, sy = plot_points_with_samples(db, "s", start, end, max_bins)
        assert min(y) == min(value for _, value in expected)
        assert max(y) == max(value for _, value in expected)
        assert set(zip(sx, sy)) <= expected
        assert len(x) <= 2 * (max_bins + 1)
    db.close()
