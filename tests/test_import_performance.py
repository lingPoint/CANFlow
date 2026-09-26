import time
import zlib
from pathlib import Path

import can
import pytest
from PySide6 import QtCore, QtWidgets

from canflow.core import FileInfo, inspect_file


def reference(path):
    with can.BLFReader(path) as reader:
        frames = [m for m in reader if not m.is_remote_frame and not m.is_error_frame]
    payloads = {}
    for m in frames:
        key = (m.channel, m.arbitration_id, m.is_extended_id)
        payloads[key] = max(payloads.get(key, 0), len(m.data))
    return FileInfo(path, min(m.timestamp for m in frames), max(m.timestamp for m in frames),
                    len(frames), tuple(sorted({m.channel for m in frames})),
                    tuple(sorted((*key, length) for key, length in payloads.items())))


def test_preflight_does_not_allocate_messages(sample_recording, monkeypatch):
    from can.io import blf
    path, _ = sample_recording
    expected = reference(path)
    def forbidden(*args, **kwargs):
        pytest.fail("Preflight must not allocate a Message for every frame")
    monkeypatch.setattr(blf, "Message", forbidden)
    assert inspect_file(path) == expected


@pytest.mark.parametrize("compression", [0, 6])
@pytest.mark.parametrize("container_size", [127, 128 * 1024])
def test_metadata_matches_reader_across_boundaries(tmp_path, compression, container_size):
    path = tmp_path / "mixed.blf"
    with can.BLFWriter(path, compression_level=compression, max_container_size=container_size) as writer:
        for i in range(400):
            writer.on_message_received(can.Message(
                timestamp=1700000000 + (i % 17) * 0.000001,
                channel=i % 16, arbitration_id=0x123, is_extended_id=i % 2 == 0,
                is_fd=i % 3 == 0, is_remote_frame=i % 11 == 0,
                is_error_frame=i % 13 == 0, data=bytes(i % 65 if i % 3 == 0 else i % 9)))
    assert inspect_file(path) == reference(path)


def raw_blf(path, objects, split=137, compressed=True):
    from can.io import blf
    with can.BLFWriter(path):
        pass
    header = path.read_bytes()[:blf.FILE_HEADER_SIZE]
    data = b"".join(objects)
    with path.open("wb") as stream:
        stream.write(header)
        for offset in range(0, len(data), split):
            chunk = data[offset:offset + split]
            packed = zlib.compress(chunk) if compressed else chunk
            container = blf.LOG_CONTAINER_STRUCT.pack(2 if compressed else 0, len(chunk)) + packed
            size = 16 + len(container)
            stream.write(blf.OBJ_HEADER_BASE_STRUCT.pack(b"LOBJ", 16, 1, size, blf.LOG_CONTAINER))
            stream.write(container)
            stream.write(bytes(size % 4))


def raw_object(kind, payload, ticks, flags=2, version=1):
    from can.io import blf
    header = (blf.OBJ_HEADER_V1_STRUCT if version == 1 else blf.OBJ_HEADER_V2_STRUCT).pack(flags, 0, 0, ticks)
    size = 16 + len(header) + len(payload)
    return blf.OBJ_HEADER_BASE_STRUCT.pack(b"LOBJ", 16 + len(header), version, size, kind) + header + payload + bytes(size % 4)


@pytest.mark.parametrize("split", [17, 37, 137, 131072])
def test_metadata_v2_message2_fd64_padding_tick_units_and_ignored_objects(tmp_path, split):
    from can.io import blf
    path = tmp_path / "variants.blf"
    objects = []
    for i in range(45):
        # Time rollback and mixed timestamp units, EXT/STD and remote frames.
        objects.append(raw_object(blf.CAN_MESSAGE2,
            blf.CAN_MSG_STRUCT.pack(i % 17, 0x80 if i % 7 == 0 else 0, i % 9,
                                    0x80000123 if i % 2 else 0x123, bytes(8)),
            (45 - i) * 1000000000, version=2))
        members = [i % 17, 15, 64, 0, 0x456, 0, 0x1000, 0, 0, 0, 0, 0, 0, 0, 0]
        # FD64's valid length exceeds stored data; reference reader zero pads.
        objects.append(raw_object(blf.CAN_FD_MESSAGE_64,
            blf.CAN_FD_MSG_64_STRUCT.pack(*members) + bytes(12), i * 100001, flags=1))
        members[6] |= 0x10
        objects.append(raw_object(blf.CAN_FD_MESSAGE_64,
            blf.CAN_FD_MSG_64_STRUCT.pack(*members) + bytes(64), 999999999999, version=2))
        objects.append(raw_object(999, b"ignored", 99999999999999))
    raw_blf(path, objects, split)
    assert inspect_file(path) == reference(path)


def test_preflight_cancellation_ignored_records_and_truncation(tmp_path):
    from can.io import blf
    path = tmp_path / "cancel.blf"
    objects = [raw_object(999, b"event", i) for i in range(9000)]
    raw_blf(path, objects, split=1 << 20)
    calls = []
    def cancel():
        calls.append(1)
        return len(calls) >= 4
    with pytest.raises(InterruptedError):
        inspect_file(path, cancel)
    msg = raw_object(blf.CAN_MESSAGE, blf.CAN_MSG_STRUCT.pack(1, 0, 8, 1, bytes(8)), 1000)
    raw_blf(path, [msg, msg[:-3]])
    with pytest.raises(blf.BLFParseError, match="Truncated"):
        inspect_file(path)


def test_database_cache_invalidation_failure_and_capacity(sample_recording, tmp_path, monkeypatch):
    import cantools
    from canflow.core import DatabaseCache
    _, path = sample_recording
    cache = DatabaseCache(capacity=1)
    first = cache.load(path)
    assert cache.load(path) is first
    path.write_text(path.read_text().replace("(2,-50)", "(4,-50)"))
    changed = cache.load(path)
    assert changed is not first
    assert changed.messages[0].signals[0].scale == 4
    other = tmp_path / "other.dbc"
    other.write_text(path.read_text())
    cache.load(other)
    assert cache.load(path) is not changed
    path.write_text("broken")
    with pytest.raises(Exception):
        cache.load(path)
    path.write_text(other.read_text())
    original = cantools.database.load_file
    def mutating(*args, **kwargs):
        database = original(*args, **kwargs)
        path.write_text(path.read_text() + "\n")
        return database
    monkeypatch.setattr(cantools.database, "load_file", mutating)
    with pytest.raises(ValueError, match="发生变化"):
        cache.load(path)


def wait_import(app, window):
    start = time.monotonic()
    while window.dbc_loader is not None:
        app.processEvents()
        time.sleep(0.001)
        assert time.monotonic() - start < 10


def test_dbc_assignment_is_background_atomic_and_reuses_parse(tmp_path, monkeypatch, sample_recording):
    import cantools
    from canflow.app import MainWindow
    from canflow.core import SignalKey
    _, dbc = sample_recording
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    original = cantools.database.load_file
    calls = []
    def slow(*args, **kwargs):
        calls.append(QtCore.QThread.currentThread() == app.thread())
        time.sleep(0.1)
        return original(*args, **kwargs)
    monkeypatch.setattr(cantools.database, "load_file", slow)
    try:
        window._assign_dbc_path(dbc, 1)
        assert window.mapping == {}, "Mapping must commit only after successful import"
        wait_import(app, window)
        assert calls == [False]
        key = SignalKey(1, 100, False, "BatterySOC")
        window.selected = {key}
        window._populate_signals()
        window._assign_dbc_path(dbc, 2)
        wait_import(app, window)
        assert calls == [False], "Unchanged shared DBC must not be parsed again"
        assert key in window.selected
        assert len(window.signals) == 8
        broken = tmp_path / "broken.dbc"
        broken.write_text("invalid DBC")
        errors = []
        monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args))
        window._assign_dbc_path(broken, 1)
        wait_import(app, window)
        assert errors and window.mapping[1] == dbc.resolve()
        assert key in window.selected
    finally:
        window.project_dirty = False
        window.close()


def test_new_project_discards_pending_dbc_and_selection(sample_recording, monkeypatch):
    import threading
    import cantools
    from canflow.app import MainWindow
    _, dbc = sample_recording
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    gate = threading.Event()
    entered = threading.Event()
    original = cantools.database.load_file
    def waiting(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(cantools.database, "load_file", waiting)
    try:
        window._assign_dbc_path(dbc, 1)
        assert entered.wait(5)
        window._new_project()
        gate.set()
        wait_import(app, window)
        assert window.mapping == {}
        assert window.signals == []
        assert window.signal_list.count() == 0
        assert not window.project_dirty
    finally:
        gate.set()
        window.close()


def test_scan_progress_cancel_and_overlap_remain_atomic(tmp_path, monkeypatch):
    from canflow.app import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    files = []
    for i in range(2):
        path = tmp_path / f"{i}.blf"
        with can.BLFWriter(path, compression_level=0) as writer:
            for j in range(10000):
                writer.on_message_received(can.Message(timestamp=1700000000 + j * 0.001,
                    channel=i, arbitration_id=100, is_extended_id=False, data=[1]))
        files.append(path)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames", lambda *args: ([str(files[0])], ""))
    def wait_scan():
        start = time.monotonic()
        while window.scanner is not None:
            app.processEvents()
            time.sleep(0.001)
            assert time.monotonic() - start < 10
    try:
        window._add_files()
        progress = []
        window.scanner.progress.connect(lambda *values: progress.append(values))
        wait_scan()
        assert len(window.files) == 1
        assert progress and progress[-1][-1] == 100
        old = list(window.files)
        monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames", lambda *args: ([str(files[1])], ""))
        window._add_files()
        wait_scan()
        assert errors and "时间重叠" in errors[-1][-1]
        assert window.files == old
        window._add_files()
        # A cached worker may finish before its result reaches the GUI queue.
        assert window.scanner.wait(5000)
        window._stop_tasks()
        wait_scan()
        assert window.files == old
        assert len(errors) == 1
        assert window.add_files.isEnabled() and window.clear_files.isEnabled()
        window._add_files()
        window._new_project()
        wait_scan()
        assert window.files == []
    finally:
        window.close()


def test_incremental_signal_population_preserves_pending_selection_and_can_be_cleared(sample_recording):
    from canflow.app import MainWindow
    from canflow.core import SignalKey
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        window.signals = [SignalKey(1, i, False, "Value") for i in range(10000)]
        window.selected = {window.signals[-1]}
        done = []
        window._populate_signals(on_finished=lambda: done.append(True))
        assert window._populating_signals
        assert window.signal_list.count() < 10000
        assert window._selection_from_list()[0] == window.selected
        window._new_project()
        for _ in range(10):
            app.processEvents()
        assert window.signal_list.count() == 0 and window.selected == set()
        assert done == []
    finally:
        window.close()
