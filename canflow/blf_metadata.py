"""Full BLF preflight without allocating a Message/payload for every frame.

Container decompression and boundary buffering remain python-can's. Only the
metadata consumer differs; replay still uses its normal BLFReader. Timestamp
extrema are accumulated as integer ticks and converted with its Decimal factors.
"""
from __future__ import annotations

import struct
from decimal import Decimal

from can.io import blf


_CAN = struct.Struct("<HBBL")
_FD = struct.Struct("<HBBLLBBB")
_FD64 = struct.Struct("<BBBBLLL")


class MetadataReader(blf.BLFReader):
    def __init__(self, path, cancelled=None, progress=None):
        super().__init__(path)
        self.cancelled = cancelled
        self.progress = progress
        self.frames = 0
        self.payloads = {}
        self.ranges = {}

    def _parse_container(self, data):
        if self.cancelled and self.cancelled():
            raise InterruptedError("预检已取消")
        yield from super()._parse_container(data)
        if self.progress:
            self.progress(self.file.tell())

    def _parse_data(self, data):
        pos = 0
        end = len(data)
        unpack_base = blf.OBJ_HEADER_BASE_STRUCT.unpack_from
        unpack_v1 = blf.OBJ_HEADER_V1_STRUCT.unpack_from
        unpack_v2 = blf.OBJ_HEADER_V2_STRUCT.unpack_from
        payloads = self.payloads
        ranges = self.ranges
        count = 0
        objects = 0
        while True:
            if objects % 4096 == 0 and self.cancelled and self.cancelled():
                raise InterruptedError("预检已取消")
            objects += 1
            self._pos = pos
            try:
                pos = data.index(b"LOBJ", pos, pos + 8)
            except ValueError:
                if pos + 8 > end:
                    break
                raise blf.BLFParseError("Could not find next object") from None
            if end - pos < 16:
                break
            _, header_size, version, size, kind = unpack_base(data, pos)
            if size < 16 or header_size > size:
                raise blf.BLFParseError("Invalid BLF object size")
            next_pos = pos + size
            if next_pos > end:
                break
            body = pos + 16
            if version == 1:
                if size < 32:
                    raise blf.BLFParseError("Incomplete BLF object header")
                flags, _, _, ticks = unpack_v1(data, body)
                body += 16
            elif version == 2:
                if size < 40:
                    raise blf.BLFParseError("Incomplete BLF object header")
                flags, _, _, ticks = unpack_v2(data, body)
                body += 24
            else:
                pos = next_pos
                continue
            if kind in (blf.CAN_MESSAGE, blf.CAN_MESSAGE2):
                if next_pos - body < blf.CAN_MSG_STRUCT.size:
                    raise blf.BLFParseError("Incomplete CAN object")
                channel, frame_flags, dlc, can_id = _CAN.unpack_from(data, body)
                length = min(dlc, 8)
                remote = frame_flags & blf.REMOTE_FLAG
            elif kind == blf.CAN_FD_MESSAGE:
                if next_pos - body < blf.CAN_FD_MSG_STRUCT.size:
                    raise blf.BLFParseError("Incomplete CAN FD object")
                channel, frame_flags, _, can_id, _, _, _, valid = _FD.unpack_from(data, body)
                length = min(valid, 64)
                remote = frame_flags & blf.REMOTE_FLAG
            elif kind == blf.CAN_FD_MESSAGE_64:
                if next_pos - body < blf.CAN_FD_MSG_64_STRUCT.size:
                    raise blf.BLFParseError("Incomplete CAN FD 64 object")
                channel, _, length, _, can_id, _, frame_flags = _FD64.unpack_from(data, body)
                remote = frame_flags & 0x10
            else:
                pos = next_pos
                continue
            pos = next_pos
            if remote:
                continue
            count += 1
            # Match python-can's arbitration-id mask, including the EXT bit.
            identity = (channel << 32) | (can_id & 0x9FFFFFFF)
            if length > payloads.get(identity, -1):
                payloads[identity] = length
            scale = flags == 1
            limits = ranges.get(scale)
            if limits is None:
                ranges[scale] = [ticks, ticks]
            else:
                if ticks < limits[0]:
                    limits[0] = ticks
                if ticks > limits[1]:
                    limits[1] = ticks
        self.frames += count
        # BLFReader's buffering contract expects an iterator.
        yield from ()

    def metadata(self):
        for _ in self:
            pass
        if self._tail.strip(b"\0"):
            raise blf.BLFParseError("Truncated BLF object at end of file")
        if not self.frames:
            raise ValueError("没有 CAN/CAN FD 数据帧")
        times = [float(Decimal(ticks) * (blf.TIME_TEN_MICS_FACTOR if scale
                                       else blf.TIME_ONE_NANS_FACTOR)) + self.start_timestamp
                 for scale, limits in self.ranges.items() for ticks in limits]
        payloads = tuple(sorted((identity >> 32, identity & 0x1FFFFFFF,
                                 bool(identity & blf.CAN_MSG_EXT), length)
                                for identity, length in self.payloads.items()))
        # BLF channel numbers are one-based; python-can exposes zero-based ones.
        payloads = tuple((channel - 1, frame_id, extended, length)
                         for channel, frame_id, extended, length in payloads)
        return min(times), max(times), self.frames, tuple(sorted({p[0] for p in payloads})), payloads
