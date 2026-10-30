import os
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import can
from PySide6 import QtCore, QtWidgets

from canflow.compare import CompareDialog, CompareWorker
from canflow.core import FileInfo, SignalKey, load_databases
from canflow.store import connect, nearest_sample


def test_compare_worker_isolates_sources_and_reads_timestamp_rollback(tmp_path, sample_recording):
    _, dbc = sample_recording
    files = []
    for channel, values in ((1, (3, 4, 5)), (2, (9, 10, 11))):
        path = tmp_path / f'{channel}.blf'
        with can.BLFWriter(path) as writer:
            for offset, value in zip((0, 3, 1), values):
                writer.on_message_received(can.Message(timestamp=1700000000 + offset, channel=channel,
                    arbitration_id=100, is_extended_id=False, data=[0, 0, value, 0, 0, 0, 0, 0]))
        files.append(path)
    paths = [tmp_path / 'a.sqlite', tmp_path / 'b.sqlite']
    sides = []
    for index, path in enumerate(files):
        channel = index + 1
        key = SignalKey(channel, 100, False, 'InvTemp')
        sides.append(dict(file=FileInfo(path, 1700000000, 1700000003, 3, (channel,)),
                          mapping={channel: dbc}, start=1700000000, end=1700000001, selected={key}))
    worker = CompareWorker(sides, paths)
    results = []
    worker.completed.connect(results.append)
    worker.run()
    assert results == [True]
    for index, side in enumerate(sides):
        db = connect(paths[index])
        key = next(iter(side['selected']))
        assert db.execute('select count(*) from samples').fetchone()[0] == 2
        assert nearest_sample(db, key.storage_key(), side['end']) == (side['end'], (5 if index == 0 else 11) * 2 - 50)
        db.close()


def test_compare_ui_plots_unequal_units_and_invalidates(tmp_path, sample_recording):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    blf, dbc = sample_recording
    dialog = CompareDialog()
    keys = (SignalKey(1, 100, False, 'InvTemp'), SignalKey(1, 100, False, 'BatterySOC'))
    try:
        for index, (side, key) in enumerate(zip(dialog.sides, keys)):
            side.info = FileInfo(blf, 1700000000, 1700000001.9, 20, (1,))
            side.mapping = {1: dbc}
            side.databases = load_databases(side.mapping)
            side.start.setText('1700000000')
            side.end.setText('1700000001' if index == 0 else '1700000000.6')
            side.signal.addItem(key.name, key)
        dialog.add_pair()
        assert '单位不同' in dialog.pair_list.item(0).text()
        dialog.start_compare()
        deadline = time.monotonic() + 10
        while dialog.worker and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        app.processEvents()
        assert dialog.worker is None
        assert len(dialog.plots) == 1
        assert dialog.plots[0][1] is not None
        assert all(len(curve.xData) for curve in dialog.plots[0][2])
        dialog.show()
        app.processEvents()
        plot = dialog.plots[0][0]
        low, high = plot.vb.viewRange()[1]
        pos = plot.vb.mapViewToScene(QtCore.QPointF(.5, (low + high) / 2))
        dialog.mouse_moved(pos)
        assert 'A ' in dialog.readout.text() and 'B ' in dialog.readout.text()
        assert '2023-' in dialog.readout.text()
        pos = plot.vb.mapViewToScene(QtCore.QPointF(.8, (low + high) / 2))
        dialog.mouse_moved(pos)
        assert 'B —' in dialog.readout.text()
        dialog.sides[0].end.setText('1700000000.5')
        assert dialog.sides_snapshot is None
        assert len(dialog.pairs) == 1
    finally:
        dialog.close()
        app.processEvents()


def test_compare_cancel_and_invalid_range(tmp_path, sample_recording):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    blf, dbc = sample_recording
    key = SignalKey(1, 100, False, 'InvTemp')
    info = FileInfo(blf, 1700000000, 1700000001.9, 20, (1,))
    side = dict(file=info, mapping={1: dbc}, start=info.first, end=info.last, selected={key})
    worker = CompareWorker([side, side], [tmp_path / 'a.sqlite', tmp_path / 'b.sqlite'])
    result = []
    worker.completed.connect(result.append)
    worker.stop()
    worker.run()
    assert result == [False]
    dialog = CompareDialog()
    try:
        dialog.sides[0].info = info
        dialog.sides[0].start.setText('1700000002')
        dialog.sides[0].end.setText('1700000003')
        import pytest
        with pytest.raises(ValueError, match='片段须在文件采集范围内'):
            dialog.sides[0].snapshot({key})
    finally:
        dialog.close()
        app.processEvents()


def test_same_signal_key_in_separate_caches_and_bad_dbc(tmp_path, sample_recording):
    blf, dbc = sample_recording
    key = SignalKey(1, 100, False, 'InvTemp')
    info = FileInfo(blf, 1700000000, 1700000001.9, 20, (1,))
    sides = [dict(file=info, mapping={1: dbc}, start=info.first + offset,
                  end=info.first + offset + .2, selected={key}) for offset in (0, 1)]
    paths = [tmp_path / 'a.sqlite', tmp_path / 'b.sqlite']
    worker = CompareWorker(sides, paths)
    worker.run()
    values = []
    for side, path in zip(sides, paths):
        db = connect(path)
        values.append(nearest_sample(db, key.storage_key(), side['start'])[1])
        db.close()
    assert values == [-50, -30]
    errors = []
    sides[0]['mapping'] = {1: tmp_path / 'missing.dbc'}
    worker = CompareWorker(sides, [tmp_path / 'c.sqlite', tmp_path / 'd.sqlite'])
    worker.failed.connect(errors.append)
    worker.run()
    assert errors
