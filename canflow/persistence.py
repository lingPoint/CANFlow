from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .core import SignalKey, load_databases, signal_definition_fingerprint


PROJECT_SCHEMA_VERSION = 1
GROUP_SCHEMA_VERSION = 1


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class SignalReference:
    key: SignalKey
    fingerprint: str | None

    def to_dict(self) -> dict:
        return {
            "channel": self.key.channel,
            "frame_id": self.key.frame_id,
            "extended": self.key.extended,
            "name": self.key.name,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> SignalReference:
        return cls(
            SignalKey(
                int(payload["channel"]),
                int(payload["frame_id"]),
                bool(payload["extended"]),
                str(payload["name"]),
            ),
            str(payload["fingerprint"]) if payload.get("fingerprint") else None,
        )


@dataclass(frozen=True)
class DbcReference:
    channel: int
    relative_path: str | None
    absolute_path: str
    sha256: str

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> DbcReference:
        return cls(
            int(payload["channel"]),
            str(payload["relative_path"]) if payload.get("relative_path") else None,
            str(payload["absolute_path"]),
            str(payload["sha256"]),
        )


@dataclass(frozen=True)
class ProjectDocument:
    mappings: tuple[DbcReference, ...]
    selected: tuple[SignalReference, ...]


def make_signal_references(
    selected: Iterable[SignalKey], mapping: dict[int, Path]
) -> tuple[SignalReference, ...]:
    databases = load_databases(mapping)
    return tuple(
        SignalReference(key, signal_definition_fingerprint(key, databases))
        for key in sorted(selected, key=lambda item: item.storage_key())
    )


def save_project(
    path: Path,
    mapping: dict[int, Path],
    selected: Iterable[SignalKey],
    preserved_mappings: Iterable[DbcReference] = (),
    unresolved_selected: Iterable[SignalReference] = (),
) -> None:
    path = path.resolve()
    mappings: list[DbcReference] = []
    for channel, dbc_path in sorted(mapping.items()):
        resolved = dbc_path.resolve()
        try:
            relative = os.path.relpath(resolved, path.parent)
        except ValueError:
            relative = None
        mappings.append(DbcReference(channel, relative, str(resolved), file_sha256(resolved)))
    resolved_channels = set(mapping)
    mappings.extend(item for item in preserved_mappings if item.channel not in resolved_channels)
    selected_refs = list(make_signal_references(selected, mapping))
    resolved_keys = {item.key for item in selected_refs}
    selected_refs.extend(item for item in unresolved_selected if item.key not in resolved_keys)
    _write_json_atomic(
        path,
        {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "dbc_mappings": [item.to_dict() for item in mappings],
            "selected_signals": [item.to_dict() for item in selected_refs],
        },
    )


def load_project(path: Path) -> ProjectDocument:
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version")
    if version != PROJECT_SCHEMA_VERSION:
        raise ValueError(f"不支持的项目格式版本：{version!r}")
    return ProjectDocument(
        tuple(DbcReference.from_dict(item) for item in payload.get("dbc_mappings", [])),
        tuple(SignalReference.from_dict(item) for item in payload.get("selected_signals", [])),
    )


def resolve_dbc_reference(reference: DbcReference, project_path: Path) -> tuple[Path | None, bool]:
    candidates: list[Path] = []
    if reference.relative_path:
        candidates.append((project_path.parent / reference.relative_path).resolve())
    candidates.append(Path(reference.absolute_path))
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            return candidate, file_sha256(candidate) != reference.sha256
    return None, False


@dataclass(frozen=True)
class SignalGroup:
    name: str
    signals: tuple[SignalReference, ...]

    def to_dict(self) -> dict:
        return {"name": self.name, "signals": [item.to_dict() for item in self.signals]}

    @classmethod
    def from_dict(cls, payload: dict) -> SignalGroup:
        return cls(str(payload["name"]), tuple(SignalReference.from_dict(item) for item in payload["signals"]))


class SignalGroupStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[SignalGroup]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != GROUP_SCHEMA_VERSION:
            raise ValueError("不支持的信号组库版本")
        return [SignalGroup.from_dict(item) for item in payload.get("groups", [])]

    def save(self, groups: Iterable[SignalGroup]) -> None:
        ordered = sorted(groups, key=lambda item: item.name.casefold())
        _write_json_atomic(
            self.path,
            {"schema_version": GROUP_SCHEMA_VERSION, "groups": [item.to_dict() for item in ordered]},
        )


def export_signal_group(path: Path, group: SignalGroup) -> None:
    _write_json_atomic(path, {"schema_version": GROUP_SCHEMA_VERSION, "group": group.to_dict()})


def import_signal_group(path: Path) -> SignalGroup:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != GROUP_SCHEMA_VERSION:
        raise ValueError("不支持的信号组文件版本")
    return SignalGroup.from_dict(payload["group"])
