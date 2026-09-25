from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import can
import cantools


@dataclass(frozen=True)
class FileInfo:
    path: Path
    first: float
    last: float
    frames: int
    channels: tuple[int, ...]
    max_payloads: tuple[tuple[int, int, bool, int], ...] = ()


@dataclass(frozen=True)
class SignalKey:
    channel: int
    frame_id: int
    extended: bool
    name: str

    def label(self) -> str:
        kind = "EXT" if self.extended else "STD"
        return f"CH{self.channel} · {kind} 0x{self.frame_id:X} · {self.name}"

    def storage_key(self) -> str:
        return f"{self.channel}:{self.frame_id}:{int(self.extended)}:{self.name}"


def usable_frame(message: can.Message) -> bool:
    return not (message.is_error_frame or message.is_remote_frame)


def inspect_file(path: Path, cancelled: Callable[[], bool] | None = None) -> FileInfo:
    first = None
    last = None
    count = 0
    channels: set[int] = set()
    max_payloads: dict[tuple[int, int, bool], int] = {}
    with can.BLFReader(path) as reader:
        for message in reader:
            if cancelled is not None and count % 4096 == 0 and cancelled():
                raise InterruptedError("预检已取消")
            if not usable_frame(message):
                continue
            timestamp = float(message.timestamp)
            first = timestamp if first is None else min(first, timestamp)
            last = timestamp if last is None else max(last, timestamp)
            count += 1
            if message.channel is not None:
                channel = int(message.channel)
                channels.add(channel)
                frame = (channel, message.arbitration_id, message.is_extended_id)
                max_payloads[frame] = max(max_payloads.get(frame, 0), len(message.data))
    if first is None or last is None:
        raise ValueError(f"{path.name}: 没有 CAN/CAN FD 数据帧")
    payloads = tuple(sorted((*frame, length) for frame, length in max_payloads.items()))
    return FileInfo(path, first, last, count, tuple(sorted(channels)), payloads)


def prepare_sequence(paths: Iterable[Path]) -> list[FileInfo]:
    unique = list(dict.fromkeys(Path(p).resolve() for p in paths))
    if not unique:
        raise ValueError("请选择至少一个 BLF 文件")
    return validate_sequence(inspect_file(path) for path in unique)


def validate_sequence(files: Iterable[FileInfo]) -> list[FileInfo]:
    files = list(files)
    files.sort(key=lambda info: (info.first, str(info.path).lower()))
    for previous, current in zip(files, files[1:]):
        if current.first < previous.last:
            raise ValueError(
                f"时间重叠：{previous.path.name} 结束于 {previous.last:.6f}，"
                f"{current.path.name} 开始于 {current.first:.6f}"
            )
    return files


def load_databases(mapping: dict[int, Path]) -> dict[int, cantools.database.Database]:
    # Several channels often share one DBC. Parse/compile it once per operation.
    loaded = {}
    result = {}
    for channel, path in mapping.items():
        path = path.resolve()
        if path not in loaded:
            loaded[path] = cantools.database.load_file(str(path))
        result[channel] = loaded[path]
    return result


def prepare_decoders(databases: dict[int, cantools.database.Database], selected: set[SignalKey]) -> dict:
    """Bind message lookup and storage identities once, outside the frame loop.

    Common Intel integer signals use precomputed bit slices and cantools' own
    scaling conversion. Float, multiplexed and Motorola definitions retain the
    general cantools decoder. Truncation checks apply per selected signal.
    """
    groups = {}
    for key in selected:
        groups.setdefault((key.channel, key.frame_id, key.extended), []).append(key)
    result = {}
    for identity, keys in groups.items():
        database = databases.get(identity[0])
        if database is None:
            continue
        try:
            definition = database.get_message_by_frame_id(identity[1])
        except KeyError:
            continue
        if definition.is_extended_frame != identity[2]:
            continue
        names = tuple((key.name, key.storage_key()) for key in keys)
        by_name = {signal.name: signal for signal in definition.signals}
        signals = [(by_name[name], storage) for name, storage in names if name in by_name]
        if (not definition.is_container and not definition.is_multiplexed()
                and all(signal.byte_order == "little_endian" and not signal.is_float
                        for signal, _ in signals)):
            slices = tuple((signal.start // 8, (signal.start + signal.length + 7) // 8,
                            signal.start % 8, (1 << signal.length) - 1,
                            (1 << (signal.length - 1)) if signal.is_signed else 0,
                            storage, signal.conversion.raw_to_scaled) for signal, storage in signals)

            def decode_ints(data, slices=slices):
                values = []
                for begin, end, shift, mask, sign, storage, convert in slices:
                    if end > len(data):
                        continue
                    raw = (int.from_bytes(data[begin:end], "little") >> shift) & mask
                    if raw & sign:
                        raw -= mask + 1
                    values.append((storage, float(convert(raw, False))))
                return values

            result[identity] = decode_ints
            continue

        def decode(data, decode_message=definition.decode, names=names):
            try:
                values = decode_message(data, decode_choices=False, allow_truncated=True)
            except (ValueError, KeyError, cantools.database.DecodeError):
                return []
            return [(storage, float(values[name])) for name, storage in names
                    if isinstance(values.get(name), (int, float)) and not isinstance(values[name], bool)]

        result[identity] = decode
    return result


def available_signals(databases: dict[int, cantools.database.Database]) -> list[SignalKey]:
    keys = []
    for channel, database in sorted(databases.items()):
        for message in database.messages:
            for signal in message.signals:
                keys.append(SignalKey(channel, message.frame_id, message.is_extended_frame, signal.name))
    return keys


def _signal_definition(key: SignalKey, databases: dict[int, cantools.database.Database]):
    database = databases.get(key.channel)
    if database is None:
        return None
    try:
        message = database.get_message_by_frame_id(key.frame_id)
        if message.is_extended_frame != key.extended:
            return None
        signal = next(item for item in message.signals if item.name == key.name)
    except (KeyError, StopIteration):
        return None
    return message, signal


def signal_definition_fingerprint(
    key: SignalKey, databases: dict[int, cantools.database.Database]
) -> str | None:
    """Return a stable digest of the DBC definition behind a signal identity."""
    definition = _signal_definition(key, databases)
    if definition is None:
        return None
    _, signal = definition
    definition = {
        "frame_id": key.frame_id,
        "extended": key.extended,
        "name": signal.name,
        "start": signal.start,
        "length": signal.length,
        "byte_order": signal.byte_order,
        "is_signed": signal.is_signed,
        "scale": signal.scale,
        "offset": signal.offset,
        "minimum": signal.minimum,
        "maximum": signal.maximum,
        "unit": signal.unit,
        "choices": sorted((int(value), str(label)) for value, label in (signal.choices or {}).items()),
    }
    payload = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def signal_unit(key: SignalKey, databases: dict[int, cantools.database.Database]) -> str:
    definition = _signal_definition(key, databases)
    return definition[1].unit or "" if definition is not None else ""


def missing_signal_reason(
    key: SignalKey,
    files: list[FileInfo],
    databases: dict[int, cantools.database.Database],
) -> str:
    lengths = [length for info in files for channel, frame_id, extended, length in info.max_payloads
               if (channel, frame_id, extended) == (key.channel, key.frame_id, key.extended)]
    if not lengths:
        return "BLF 中没有对应报文"
    longest = max(lengths)
    definition = _signal_definition(key, databases)
    if definition is not None:
        _, signal = definition
        if signal.byte_order == "little_endian":
            required = (signal.start + signal.length + 7) // 8
            if required > longest:
                return f"DBC 需要至少 {required} 字节，BLF 该报文最长 {longest} 字节"
    return "未解码出样本，请检查 DBC 与报文内容"


def decode_selected(
    message: can.Message,
    database: cantools.database.Database | None,
    selected: set[SignalKey],
) -> list[tuple[SignalKey, float]]:
    if database is None or message.channel is None:
        return []
    channel = int(message.channel)
    try:
        definition = database.get_message_by_frame_id(message.arbitration_id)
    except KeyError:
        return []
    if definition.is_extended_frame != message.is_extended_id:
        return []
    wanted = [key for key in selected if key.channel == channel and key.frame_id == message.arbitration_id
              and key.extended == message.is_extended_id]
    if not wanted:
        return []
    try:
        # A DBC may describe a longer CAN FD payload than a recorded CAN frame.
        # Decode only signals whose bits are present in the actual frame.
        values = definition.decode(message.data, decode_choices=False, allow_truncated=True)
    except (ValueError, KeyError, cantools.database.DecodeError):
        return []
    result = []
    for key in wanted:
        value = values.get(key.name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append((key, float(value)))
    return result
