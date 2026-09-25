"""Opt-in end-to-end test: CANFLOW_TEST_1GB=1 pytest tests/test_large_blf.py -s."""

import os
import json
import threading
import time
from pathlib import Path

import can
import pytest
from PySide6 import QtCore, QtWidgets

from canflow.app import MainWindow
from canflow.core import SignalKey, available_signals, inspect_file, load_databases
from canflow.store import connect
from canflow.workers import ReplayWorker
from scripts.generate_multichannel_blf import fixture_paths, generate_multichannel_blf


@pytest.mark.skipif(os.environ.get("CANFLOW_TEST_16CH") != "1", reason="opt-in dense 16-channel 1 GiB benchmark")
def test_dense_sixteen_channel_gib_replay_and_responsive_ui(large_workspace: Path) -> None:
    from scripts.benchmark_v3 import benchmark
    result = benchmark(large_workspace, 1024, "dense16", ui=True)
    assert result["bytes"] >= 1 << 30
    assert result["decoded_samples"] == result["frames"]
    assert result["ui_visible_tracks"] == 16
    assert result["rss_growth_mib"] < 512


def _working_set_bytes() -> int:
    """Read Windows process RSS without adding a benchmark-only dependency."""
    import ctypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
    if not get_memory(ctypes.c_void_p(process), ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return counters.WorkingSetSize


@pytest.fixture
def large_workspace(tmp_path: Path):
    yield tmp_path
    for path in tmp_path.iterdir():
        if path.is_file():
            path.unlink()


@pytest.mark.skipif(os.environ.get("CANFLOW_TEST_1GB") != "1", reason="requires 1 GiB of temporary disk space")
def test_one_gib_blf_scans_and_replays_with_bounded_memory(large_workspace: Path) -> None:
    tmp_path = large_workspace
    blf = tmp_path / "one-gib.blf"
    payload = bytes(range(64))
    target_size = 1 << 30
    frame_count = 0
    started = time.monotonic()
    with can.BLFWriter(blf, compression_level=0) as writer:
        while writer.file.tell() < target_size:
            for _ in range(10_000):
                frame_count += 1
                writer.on_message_received(can.Message(
                    timestamp=1_700_000_000 + frame_count * 0.0001,
                    channel=1,
                    arbitration_id=0x123 if frame_count % 1000 == 0 else 0x124,
                    data=payload,
                    is_fd=True,
                    is_extended_id=False,
                ))
    generation_seconds = time.monotonic() - started
    assert blf.stat().st_size >= target_size

    dbc = tmp_path / "one.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Selected: 64 ECU\n SG_ Speed : 0|8@1+ (1,0) [0|255] "" ECU\n',
        encoding="utf-8",
    )
    base_rss = _working_set_bytes()
    peak_rss = [base_rss]
    done = threading.Event()

    def watch_memory() -> None:
        while not done.wait(0.05):
            peak_rss[0] = max(peak_rss[0], _working_set_bytes())

    monitor = threading.Thread(target=watch_memory, daemon=True)
    monitor.start()
    try:
        scan_started = time.monotonic()
        info = inspect_file(blf)
        scan_seconds = time.monotonic() - scan_started
        assert info.frames == frame_count
        assert info.channels == (1,)
        key = SignalKey(1, 0x123, False, "Speed")
        cache = tmp_path / "samples.sqlite"
        worker = ReplayWorker([info], {1: dbc}, {key}, cache)
        updates = []
        worker.advanced.connect(lambda data: updates.__setitem__(slice(None), [data]))
        replay_started = time.monotonic()
        worker.run()
        replay_seconds = time.monotonic() - replay_started
        assert worker.succeeded
        assert updates[-1][1:3] == (frame_count, frame_count)
        assert len(updates[-1][4]) <= 1000
        connection = connect(cache)
        try:
            assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == frame_count // 1000
        finally:
            connection.close()

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = MainWindow()
        try:
            window._scan_done([info])
            window.mapping = {1: dbc}
            window.signals = available_signals(load_databases(window.mapping))
            window._populate_signals()
            window.signal_list.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
            ui_started = time.monotonic()
            window._start()
            deadline = ui_started + 180
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
            ui_seconds = time.monotonic() - ui_started
            assert window.worker is None, "1 GiB UI replay did not finish within 180 seconds"
            assert window.progress.value() == 1000
            assert window.raw_table.rowCount() == 1000
            assert window.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == frame_count // 1000
        finally:
            window.close()
    finally:
        done.set()
        monitor.join()
    memory_growth = peak_rss[0] - base_rss
    print(
        f"BLF={blf.stat().st_size / (1 << 30):.2f} GiB, frames={frame_count:,}, "
        f"generate={generation_seconds:.1f}s, scan={scan_seconds:.1f}s, "
        f"replay={replay_seconds:.1f}s, UI replay={ui_seconds:.1f}s, "
        f"peak RSS growth={memory_growth / (1 << 20):.1f} MiB"
    )
    assert memory_growth < 512 * (1 << 20)


@pytest.mark.skipif(os.environ.get("CANFLOW_TEST_1GB") != "1", reason="requires 1 GiB of temporary disk space")
def test_one_gib_multichannel_blf_maps_each_channel_and_replays_ui(large_workspace: Path) -> None:
    configured = os.environ.get("CANFLOW_MULTICHANNEL_BLF")
    blf = Path(configured) if configured else large_workspace / "multichannel-1g.blf"
    generation_started = time.monotonic()
    if configured:
        _, _, manifest_path = fixture_paths(blf)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = generate_multichannel_blf(blf, 1 << 30)
    generation_seconds = time.monotonic() - generation_started
    assert blf.stat().st_size >= 1 << 30
    assert blf.stat().st_size == manifest["bytes"]
    first_dbc, second_dbc, _ = fixture_paths(blf)
    mapping = {1: first_dbc, 2: second_dbc}
    keys = {channel: SignalKey(channel, 0x123, False, "Speed") for channel in mapping}
    base_rss = _working_set_bytes()
    peak_rss = [base_rss]
    done = threading.Event()

    def watch_memory() -> None:
        while not done.wait(0.05):
            peak_rss[0] = max(peak_rss[0], _working_set_bytes())

    monitor = threading.Thread(target=watch_memory, daemon=True)
    monitor.start()
    try:
        scan_started = time.monotonic()
        info = inspect_file(blf)
        scan_seconds = time.monotonic() - scan_started
        assert info.frames == manifest["frames"]
        assert info.channels == (1, 2, 3)
        assert all((channel, 0x123, False, 64) in info.max_payloads for channel in (1, 2, 3))
        assert manifest["unmapped_same_id_frames"] > 0
        assert manifest["classic_frames"] > 0

        cache = large_workspace / "multi-samples.sqlite"
        worker = ReplayWorker([info], mapping, set(keys.values()), cache)
        latest = []
        worker.advanced.connect(lambda update: latest.__setitem__(slice(None), [update]))
        replay_started = time.monotonic()
        worker.run()
        replay_seconds = time.monotonic() - replay_started
        assert worker.succeeded
        assert latest[-1][1:3] == (manifest["frames"], manifest["frames"])
        assert {row[1] for row in latest[-1][4]} == {1, 2, 3}
        assert {row[3] for row in latest[-1][4]} == {"CAN", "FD"}
        connection = connect(cache)
        try:
            for channel, expected_value in ((1, 7.0), (2, 14.0)):
                count, minimum, maximum = connection.execute(
                    "SELECT COUNT(*), MIN(value), MAX(value) FROM samples WHERE signal = ?",
                    (keys[channel].storage_key(),),
                ).fetchone()
                assert count == manifest["selected_frames"][str(channel)]
                assert (minimum, maximum) == (expected_value, expected_value)
            assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == sum(
                manifest["selected_frames"].values()
            )
        finally:
            connection.close()

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = MainWindow()
        try:
            window._scan_done([info])
            window.mapping = mapping
            window._refresh_channel_table()
            window.signals = available_signals(load_databases(mapping))
            window._populate_signals()
            for row in range(window.signal_list.count()):
                item = window.signal_list.item(row)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in keys.values():
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
            ui_started = time.monotonic()
            window._start()
            deadline = ui_started + 180
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
            ui_seconds = time.monotonic() - ui_started
            assert window.worker is None, "multichannel 1 GiB UI replay did not finish within 180 seconds"
            assert window.selected == set(keys.values())
            assert window.progress.value() == 1000
            assert window.raw_table.rowCount() == 1000
            assert {window.raw_table.item(row, 1).text() for row in range(1000)} == {"1", "2", "3"}
            assert {window.raw_table.item(row, 3).text() for row in range(1000)} == {"CAN", "FD"}
            assert any(
                window.raw_table.item(row, 1).text() == "3"
                and window.raw_table.item(row, 2).text() == "0x123"
                for row in range(1000)
            )
            assert window.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == sum(
                manifest["selected_frames"].values()
            )
            for channel, expected_value in ((1, 7.0), (2, 14.0)):
                values = window.db.execute(
                    "SELECT MIN(value), MAX(value) FROM samples WHERE signal = ?",
                    (keys[channel].storage_key(),),
                ).fetchone()
                assert values == (expected_value, expected_value)
            window._refresh_plot(all_visible=True)
            assert all(len(window.curves[key].xData) > 0 for key in keys.values())
        finally:
            window.close()
    finally:
        done.set()
        monitor.join()
    memory_growth = peak_rss[0] - base_rss
    print(
        f"Multichannel BLF={blf.stat().st_size / (1 << 30):.2f} GiB, "
        f"frames={manifest['frames']:,}, generate={generation_seconds:.1f}s, "
        f"scan={scan_seconds:.1f}s, replay={replay_seconds:.1f}s, "
        f"UI replay={ui_seconds:.1f}s, peak RSS growth={memory_growth / (1 << 20):.1f} MiB"
    )
    assert memory_growth < 512 * (1 << 20)
