from pathlib import Path

from canflow.core import SignalKey, inspect_file
from canflow.store import connect
from canflow.workers import ReplayWorker
from scripts.generate_multichannel_blf import fixture_paths, generate_multichannel_blf


def test_mixed_channels_use_their_own_dbc_and_keep_unmapped_raw(tmp_path: Path) -> None:
    blf = tmp_path / "mixed.blf"
    manifest = generate_multichannel_blf(blf, 2 << 20)
    first_dbc, second_dbc, _ = fixture_paths(blf)
    info = inspect_file(blf)
    assert info.frames == manifest["frames"]
    assert info.channels == (1, 2, 3)
    assert manifest["classic_frames"] > 0
    assert manifest["unmapped_same_id_frames"] > 0
    keys = {channel: SignalKey(channel, 0x123, False, "Speed") for channel in (1, 2)}
    cache = tmp_path / "samples.sqlite"
    worker = ReplayWorker([info], {1: first_dbc, 2: second_dbc}, set(keys.values()), cache)
    latest = []
    worker.advanced.connect(lambda update: latest.__setitem__(slice(None), [update]))
    worker.run()
    assert worker.succeeded
    assert latest[-1][1:3] == (manifest["frames"], manifest["frames"])
    assert {row[1] for row in latest[-1][4]} == {1, 2, 3}
    assert {row[3] for row in latest[-1][4]} == {"CAN", "FD"}
    db = connect(cache)
    try:
        for channel, expected_value in ((1, 7.0), (2, 14.0)):
            count, low, high = db.execute(
                "SELECT COUNT(*), MIN(value), MAX(value) FROM samples WHERE signal = ?",
                (keys[channel].storage_key(),),
            ).fetchone()
            assert count == manifest["selected_frames"][channel]
            assert (low, high) == (expected_value, expected_value)
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == sum(
            manifest["selected_frames"].values()
        )
    finally:
        db.close()
