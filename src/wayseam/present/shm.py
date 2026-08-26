# SPDX-License-Identifier: MIT
"""Host-side reader for Wayseam's IVSHMEM shared-memory frame ring.

The guest writes the same ``WSD1`` wire blobs the HTTP path carries into a
seqlocked slot ring inside the 64 MiB IVSHMEM backing file (ADR 0004). This
module maps that file and turns published slots into ``WayseamFrame`` objects
via :func:`wayseam.guest.agent.decode_wayseam_delta`. Torn or garbage data is
never fatal here — ``poll`` returns ``None`` and the caller falls back to
HTTP; only programmer errors (bad slot index, invalid ring at open) raise.
"""

from __future__ import annotations

import mmap
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wayseam.guest.agent import AgentError, WayseamFrame, decode_wayseam_delta

if TYPE_CHECKING:
    from wayseam.config import Config

RING_MAGIC = b"WSRING1\0"
RING_VERSION = 1
RING_FILE_NAME = "wayseam-ivshmem.bin"
HEADER_SLOT_DESC_OFFSET = 64
SLOT_DESC_STRIDE = 32
SLOT_DATA_OFFSET = 64
SLOT_FLAG_TOO_LARGE = 0x1
MIN_SLOT_COUNT = 1
MAX_SLOT_COUNT = 16
MIN_SLOT_SIZE = 1024 * 1024
MAX_SLOT_SIZE = 64 * 1024 * 1024
POLL_ATTEMPTS = 3

_HEADER = struct.Struct("<8sIIQQ")  # magic, version, slot_count, slot_size, slot0_offset
_DESCRIPTOR = struct.Struct("<QIIQ")  # hwnd, pid, active, heartbeat
_U64 = struct.Struct("<Q")
_U32 = struct.Struct("<I")


class RingBuffer(Protocol):
    """Anything slice-readable like an ``mmap`` over the ring file."""

    def __len__(self) -> int: ...

    def __getitem__(self, item: slice, /) -> bytes | bytearray: ...


def _read_u64(buf: RingBuffer, offset: int) -> int:
    return _U64.unpack(bytes(buf[offset:offset + 8]))[0]


def _read_u32(buf: RingBuffer, offset: int) -> int:
    return _U32.unpack(bytes(buf[offset:offset + 4]))[0]


@dataclass(frozen=True)
class ShmTooLarge:
    """The guest flagged this update as too large for the slot; fetch it via HTTP."""

    sequence: int


class ShmNeedsBase:
    """The slot holds a partial delta but the reader has no base frame yet.

    Happens whenever a presenter attaches after the window's first full frame
    was superseded (apps that animate on open). The pump must fetch one full
    frame over HTTP; subsequent deltas then apply in place.
    """

    __slots__ = ("sequence",)

    def __init__(self, sequence: int) -> None:
        self.sequence = sequence



@dataclass(frozen=True)
class RingHeader:
    """Parsed, sanity-checked copy of the ring header at offset 0."""

    slot_count: int
    slot_size: int
    slot0_offset: int

    @staticmethod
    def read(buf: RingBuffer) -> RingHeader | None:
        raw = bytes(buf[0:_HEADER.size])
        if len(raw) < _HEADER.size:
            return None
        magic, version, slot_count, slot_size, slot0_offset = _HEADER.unpack(raw)
        if magic != RING_MAGIC or version != RING_VERSION:
            return None
        if not MIN_SLOT_COUNT <= slot_count <= MAX_SLOT_COUNT:
            return None
        if not MIN_SLOT_SIZE <= slot_size <= MAX_SLOT_SIZE:
            return None
        if slot0_offset + slot_count * slot_size > len(buf):
            return None
        return RingHeader(slot_count, slot_size, slot0_offset)

    def slot_offset(self, index: int) -> int:
        if not 0 <= index < self.slot_count:
            raise IndexError(f"slot {index} out of range 0..{self.slot_count - 1}")
        return self.slot0_offset + index * self.slot_size

    def descriptor(self, buf: RingBuffer, index: int) -> dict[str, int | bool]:
        if not 0 <= index < self.slot_count:
            raise IndexError(f"slot {index} out of range 0..{self.slot_count - 1}")
        offset = HEADER_SLOT_DESC_OFFSET + index * SLOT_DESC_STRIDE
        hwnd, pid, active, heartbeat = _DESCRIPTOR.unpack(
            bytes(buf[offset:offset + _DESCRIPTOR.size]),
        )
        return {"hwnd": hwnd, "pid": pid, "active": active != 0, "heartbeat": heartbeat}


def open_ring(path: Path | str) -> tuple[RingHeader, mmap.mmap] | None:
    """Map the ring file and parse its header; ``None`` when either fails."""
    try:
        with open(path, "r+b") as handle:
            mapped = mmap.mmap(handle.fileno(), 0)
    except (OSError, ValueError):
        return None
    header = RingHeader.read(mapped)
    if header is None:
        mapped.close()
        return None
    return header, mapped


def default_ring_path(cfg: Config | None = None) -> Path:
    """Host path of the IVSHMEM backing file (mirrors the omarchy backend)."""
    if cfg is not None:
        from wayseam.vm.backend import ivshmem_backing_file

        return ivshmem_backing_file(cfg)
    return Path.home() / "Windows" / RING_FILE_NAME


class ShmFrameSource:
    """Seqlock reader for one slot of the shared-memory frame ring."""

    def __init__(
        self,
        path: Path | str,
        slot: int,
        *,
        header: RingHeader | None = None,
        buffer: RingBuffer | None = None,
    ) -> None:
        self._file = None
        self._mmap: mmap.mmap | None = None
        if buffer is None:
            self._file = open(Path(path), "r+b")
            self._mmap = mmap.mmap(self._file.fileno(), 0)
            buffer = self._mmap
        if header is None:
            header = RingHeader.read(buffer)
            if header is None:
                self.close()
                raise ValueError(f"{path} is not a valid WSRING1 ring")
        if not 0 <= slot < header.slot_count:
            self.close()
            raise IndexError(f"slot {slot} out of range 0..{header.slot_count - 1}")
        self._buf = buffer
        self.header = header
        self.slot = slot
        self._base = header.slot_offset(slot)
        self._last_seq = 0

    @property
    def last_sequence(self) -> int:
        return self._last_seq

    def poll(
        self, previous_pixels: bytes | bytearray | None,
    ) -> WayseamFrame | ShmTooLarge | ShmNeedsBase | None:
        """Return the newest published frame, a too-large sentinel, or ``None``."""
        base = self._base
        for _ in range(POLL_ATTEMPTS):
            seq = _read_u64(self._buf, base)
            if seq & 1 or seq == self._last_seq:
                return None  # writer mid-publish, or nothing new
            length = _read_u64(self._buf, base + 8)
            flags = _read_u32(self._buf, base + 16)
            length_ok = length <= self.header.slot_size - SLOT_DATA_OFFSET
            blob = b""
            if length_ok and not flags & SLOT_FLAG_TOO_LARGE:
                start = base + SLOT_DATA_OFFSET
                blob = bytes(self._buf[start:start + length])
            if _read_u64(self._buf, base) != seq:
                continue  # torn by a concurrent publish; retry
            self._last_seq = seq  # remember even on garbage so we do not spin
            if flags & SLOT_FLAG_TOO_LARGE:
                return ShmTooLarge(seq)
            if not length_ok:
                return None
            try:
                return decode_wayseam_delta(blob, previous_pixels, in_place=True)
            except AgentError as exc:
                text = str(exc)
                if "without a base" in text or "inconsistent" in text or "out-of-bounds" in text:
                    return ShmNeedsBase(seq)
                return None
        return None

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> ShmFrameSource:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Input ring (host -> guest), in the header page. Layout mirrors the guest
# consumer in wayseam_wgc.cs: magic @2048, head u64 @2064 (host producer),
# tail u64 @2072 (guest consumer), 1024 x 48-byte entries @2112.
INPUT_MAGIC = b"WSINP1\0\0"
INPUT_MAGIC_OFFSET = 2048
INPUT_HEAD_OFFSET = 2064
INPUT_TAIL_OFFSET = 2072
INPUT_ENTRIES_OFFSET = 2112
INPUT_ENTRY_SIZE = 48
INPUT_ENTRY_COUNT = 1024

INPUT_MOVE = 1
INPUT_WHEEL = 4
INPUT_KEY_DOWN = 5
INPUT_KEY_UP = 6
INPUT_UNICODE = 7

_ENTRY = struct.Struct("<IIqiiiiii8x")  # 48-byte entry (matches guest stride)


class ShmInputWriter:
    """Single-producer writer for the host->guest input ring.

    Clicks intentionally stay on HTTP (the guest route runs the
    managed-by-host hit test); callers must :meth:`flush` before sending a
    click so queued motion lands first.
    """

    def __init__(self, buffer, *, clock=time.monotonic) -> None:
        self._buf = buffer
        self.clock = clock
        self.available = bytes(buffer[INPUT_MAGIC_OFFSET:INPUT_MAGIC_OFFSET + 8]) == INPUT_MAGIC

    def _head(self) -> int:
        return _read_u64(self._buf, INPUT_HEAD_OFFSET)

    def _tail(self) -> int:
        return _read_u64(self._buf, INPUT_TAIL_OFFSET)

    def refresh(self) -> bool:
        """Re-check the guest consumer's magic (it appears once it starts)."""
        self.available = (
            bytes(self._buf[INPUT_MAGIC_OFFSET:INPUT_MAGIC_OFFSET + 8]) == INPUT_MAGIC
        )
        return self.available

    def _write(self, kind: int, hwnd: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0) -> bool:
        if not self.available and not self.refresh():
            return False
        head = self._head()
        if head - self._tail() >= INPUT_ENTRY_COUNT - 1:
            return False  # guest stalled; caller falls back to HTTP
        offset = INPUT_ENTRIES_OFFSET + (head % INPUT_ENTRY_COUNT) * INPUT_ENTRY_SIZE
        self._buf[offset:offset + INPUT_ENTRY_SIZE] = _ENTRY.pack(
            kind, 0, hwnd, a, b, c, d, 0, 0,
        )
        self._buf[INPUT_HEAD_OFFSET:INPUT_HEAD_OFFSET + 8] = _U64.pack(head + 1)
        return True

    def move(self, hwnd: int, x: int, y: int) -> bool:
        return self._write(INPUT_MOVE, hwnd, x, y)

    def wheel(self, hwnd: int, x: int, y: int, delta_x: int, delta_y: int) -> bool:
        return self._write(INPUT_WHEEL, hwnd, x, y, delta_x, delta_y)

    def key(self, hwnd: int, virtual_key: int, *, down: bool, extended: bool) -> bool:
        return self._write(
            INPUT_KEY_DOWN if down else INPUT_KEY_UP, hwnd, virtual_key, 1 if extended else 0,
        )

    def unicode(self, hwnd: int, codepoint: int) -> bool:
        return self._write(INPUT_UNICODE, hwnd, codepoint)

    def flush(self, timeout: float = 0.02) -> bool:
        """Wait briefly until the guest consumed everything queued so far."""
        target = self._head()
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if self._tail() >= target:
                return True
            time.sleep(0.0005)
        return self._tail() >= target
