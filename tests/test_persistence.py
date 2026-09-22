from pathlib import Path

from canflow.core import SignalKey
from canflow.persistence import (
    SignalGroup,
    SignalGroupStore,
    export_signal_group,
    import_signal_group,
    load_project,
    make_signal_references,
    resolve_dbc_reference,
    save_project,
)


def make_dbc(path: Path, scale: float = 1.0) -> None:
    path.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        f'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ ({scale},0) [0|255] "km/h" ECU\n',
        encoding="utf-8",
    )


def test_project_round_trip_uses_relative_dbc_and_selected_fingerprint(tmp_path: Path) -> None:
    dbc = tmp_path / "dbc" / "vehicle.dbc"
    dbc.parent.mkdir()
    make_dbc(dbc)
    project = tmp_path / "demo.canflow.json"
    key = SignalKey(1, 0x123, False, "Speed")

    save_project(project, {1: dbc}, {key})
    document = load_project(project)

    assert document.mappings[0].relative_path == "dbc\\vehicle.dbc"
    assert document.selected[0].key == key
    assert document.selected[0].fingerprint
    resolved, changed = resolve_dbc_reference(document.mappings[0], project)
    assert resolved == dbc
    assert changed is False


def test_project_detects_external_dbc_change(tmp_path: Path) -> None:
    dbc = tmp_path / "vehicle.dbc"
    make_dbc(dbc)
    project = tmp_path / "demo.canflow.json"
    save_project(project, {1: dbc}, {SignalKey(1, 0x123, False, "Speed")})
    document = load_project(project)
    make_dbc(dbc, scale=2.0)

    resolved, changed = resolve_dbc_reference(document.mappings[0], project)
    assert resolved == dbc
    assert changed is True


def test_signal_group_store_and_export_round_trip(tmp_path: Path) -> None:
    dbc = tmp_path / "vehicle.dbc"
    make_dbc(dbc)
    key = SignalKey(1, 0x123, False, "Speed")
    group = SignalGroup("动力", make_signal_references({key}, {1: dbc}))
    store = SignalGroupStore(tmp_path / "groups.json")
    store.save([group])
    assert store.load() == [group]

    exported = tmp_path / "power.canflow-signals.json"
    export_signal_group(exported, group)
    assert import_signal_group(exported) == group
