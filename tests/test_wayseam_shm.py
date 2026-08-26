# SPDX-License-Identifier: MIT
from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayseam.guest.agent import WayseamFrame
from wayseam.present.shm import (
    HEADER_SLOT_DESC_OFFSET,
    RING_MAGIC,
    RING_VERSION,
    SLOT_DATA_OFFSET,
    SLOT_DESC_STRIDE,
    SLOT_FLAG_TOO_LARGE,
    RingHeader,
    ShmFrameSource,
    ShmTooLarge,
    default_ring_path,
    open_ring,
)

MIB = 1024 * 1024
SLOT0 = 4096
_HEADER = struct.Struct("<8sIIQQ")
_DELTA = struct.Struct("<4sIIIIIIII")


def build_ring(
    slot_count: int = 3,
    slot_size: int = MIB,
    slot0: int = SLOT0,
    size: int | None = None,
    magic: bytes = RING_MAGIC,
    version: int = RING_VERSION,
) -> bytearray:
    buf = bytearray(slot0 + slot_count * slot_size if size is None else size)
    _HEADER.pack_into(buf, 0, magic, version, slot_count, slot_size, slot0)
    return buf


def publish(buf: bytearray, slot: int, seq: int, blob: bytes, *, flags: int = 0) -> None:
    header = RingHeader.read(buf)
    assert header is not None
    base = header.slot_offset(slot)
    buf[base + SLOT_DATA_OFFSET:base + SLOT_DATA_OFFSET + len(blob)] = blob
    struct.pack_into("<QQI", buf, base, seq, len(blob), flags)


def wsd1(
    width: int, height: int, seq: int,
    x: int, y: int, rect_w: int, rect_h: int, payload: bytes,
) -> bytes:
    return _DELTA.pack(b"WSD1", width, height, width * 4, seq, x, y, rect_w, rect_h) + payload


def full_blob(width: int, height: int, seq: int, pixel: bytes = b"\x10\x20\x30\xff") -> bytes:
    return wsd1(width, height, seq, 0, 0, width, height, pixel * (width * height))


# --- RingHeader -------------------------------------------------------------

def test_header_parse_happy() -> None:
    header = RingHeader.read(build_ring())
    assert header == RingHeader(slot_count=3, slot_size=MIB, slot0_offset=SLOT0)
    assert header.slot_offset(0) == SLOT0


def test_header_rejects_bad_magic() -> None:
    assert RingHeader.read(build_ring(magic=b"NOTRING\0")) is None


def test_header_rejects_bad_version() -> None:
    assert RingHeader.read(build_ring(version=2)) is None


def test_header_rejects_slot_count_out_of_bounds() -> None:
    assert RingHeader.read(build_ring(slot_count=0)) is None
    assert RingHeader.read(build_ring(slot_count=17, size=SLOT0 + 20 * MIB)) is None


def test_header_rejects_slot_size_out_of_bounds() -> None:
    assert RingHeader.read(build_ring(slot_size=MIB - 1)) is None
    assert RingHeader.read(build_ring(slot_count=1, slot_size=65 * MIB)) is None


def test_header_rejects_file_too_small() -> None:
    assert RingHeader.read(build_ring(size=SLOT0 + 3 * MIB - 1)) is None
    assert RingHeader.read(bytearray(8)) is None


def test_descriptor_read() -> None:
    buf = build_ring()
    offset = HEADER_SLOT_DESC_OFFSET + 1 * SLOT_DESC_STRIDE
    struct.pack_into("<QIIQ", buf, offset, 0x1234567890AB, 4242, 1, 987654321)
    header = RingHeader.read(buf)
    assert header is not None
    assert header.descriptor(buf, 1) == {
        "hwnd": 0x1234567890AB, "pid": 4242, "active": True, "heartbeat": 987654321,
    }
    assert header.descriptor(buf, 0) == {
        "hwnd": 0, "pid": 0, "active": False, "heartbeat": 0,
    }
    with pytest.raises(IndexError):
        header.descriptor(buf, 3)


def test_slot_offset_bounds() -> None:
    header = RingHeader(slot_count=3, slot_size=MIB, slot0_offset=SLOT0)
    with pytest.raises(IndexError):
        header.slot_offset(3)
    with pytest.raises(IndexError):
        header.slot_offset(-1)


# --- ShmFrameSource construction --------------------------------------------

def test_constructor_rejects_garbage_ring() -> None:
    with pytest.raises(ValueError):
        ShmFrameSource("unused", 0, buffer=bytearray(64))


def test_constructor_rejects_bad_slot_index() -> None:
    with pytest.raises(IndexError):
        ShmFrameSource("unused", 3, buffer=build_ring())
    with pytest.raises(IndexError):
        ShmFrameSource("unused", -1, buffer=build_ring())


# --- poll --------------------------------------------------------------------

def test_poll_none_when_nothing_published() -> None:
    source = ShmFrameSource("unused", 0, buffer=build_ring())
    assert source.poll(None) is None
    assert source.last_sequence == 0


def test_poll_none_when_writer_mid_publish() -> None:
    buf = build_ring()
    publish(buf, 0, 3, full_blob(2, 2, 1))  # odd seq: guest still writing
    source = ShmFrameSource("unused", 0, buffer=buf)
    assert source.poll(None) is None
    assert source.last_sequence == 0


def test_poll_none_when_seq_unchanged() -> None:
    buf = build_ring()
    publish(buf, 0, 2, full_blob(2, 2, 1))
    source = ShmFrameSource("unused", 0, buffer=buf)
    assert isinstance(source.poll(None), WayseamFrame)
    assert source.poll(None) is None  # same seq: no new frame


def test_full_frame_then_damage_in_place() -> None:
    buf = build_ring()
    publish(buf, 0, 2, full_blob(4, 3, 7, pixel=b"\x10\x20\x30\xff"))
    source = ShmFrameSource("unused", 0, buffer=buf)
    frame = source.poll(None)
    assert isinstance(frame, WayseamFrame)
    assert (frame.width, frame.height, frame.stride) == (4, 3, 16)
    assert frame.server_sequence == 7
    assert bytes(frame.pixels) == b"\x10\x20\x30\xff" * 12
    assert source.last_sequence == 2

    canvas = frame.pixels
    assert isinstance(canvas, bytearray)
    publish(buf, 0, 4, wsd1(4, 3, 8, 1, 1, 2, 1, b"\xaa\xbb\xcc\xdd" * 2))
    patched = source.poll(canvas)
    assert isinstance(patched, WayseamFrame)
    assert patched.pixels is canvas  # applied in place, LatestFramePump-style
    assert patched.damage == (1, 1, 2, 1)
    expected = bytearray(b"\x10\x20\x30\xff" * 12)
    expected[16 + 4:16 + 12] = b"\xaa\xbb\xcc\xdd" * 2
    assert canvas == expected
    assert source.last_sequence == 4


class TornBuffer:
    """mmap-like stub whose seq field reads a fresh even value every time."""

    def __init__(self, data: bytearray, seq_offset: int) -> None:
        self._data = data
        self._seq_offset = seq_offset
        self.seq_reads = 0

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, item: slice) -> bytes:
        if item.start == self._seq_offset and item.stop == self._seq_offset + 8:
            self.seq_reads += 1
            return struct.pack("<Q", 2 * self.seq_reads)
        return bytes(self._data[item])


def test_torn_read_retries_bounded_then_none() -> None:
    buf = build_ring()
    publish(buf, 0, 2, full_blob(2, 2, 1))
    torn = TornBuffer(buf, SLOT0)
    source = ShmFrameSource("unused", 0, buffer=torn)
    assert source.poll(None) is None
    assert torn.seq_reads == 6  # 3 attempts x (read, verify)
    assert source.last_sequence == 0


def test_too_large_flag_returns_sentinel_once() -> None:
    buf = build_ring()
    publish(buf, 0, 2, b"", flags=SLOT_FLAG_TOO_LARGE)
    source = ShmFrameSource("unused", 0, buffer=buf)
    result = source.poll(None)
    assert result == ShmTooLarge(sequence=2)
    assert source.last_sequence == 2
    assert source.poll(None) is None  # consumed until a newer seq arrives
    publish(buf, 0, 4, full_blob(2, 2, 3))
    assert isinstance(source.poll(None), WayseamFrame)


def test_garbage_blob_returns_none_and_remembers_seq() -> None:
    buf = build_ring()
    publish(buf, 0, 2, b"\xde\xad" * 25)
    source = ShmFrameSource("unused", 0, buffer=buf)
    assert source.poll(None) is None
    assert source.last_sequence == 2  # remembered: no spinning on the bad blob
    assert source.poll(None) is None


def test_oversized_length_returns_none_and_remembers_seq() -> None:
    buf = build_ring()
    struct.pack_into("<QQI", buf, SLOT0, 2, MIB, 0)  # length > slot_size - 64
    source = ShmFrameSource("unused", 0, buffer=buf)
    assert source.poll(None) is None
    assert source.last_sequence == 2


def test_slots_one_and_two_use_their_own_offsets() -> None:
    buf = build_ring()
    publish(buf, 1, 2, full_blob(2, 2, 11, pixel=b"\x01\x01\x01\xff"))
    publish(buf, 2, 2, full_blob(3, 1, 22, pixel=b"\x02\x02\x02\xff"))
    header = RingHeader.read(buf)
    assert header is not None
    assert header.slot_offset(1) == SLOT0 + MIB
    assert header.slot_offset(2) == SLOT0 + 2 * MIB
    frame1 = ShmFrameSource("unused", 1, buffer=buf).poll(None)
    frame2 = ShmFrameSource("unused", 2, buffer=buf).poll(None)
    assert isinstance(frame1, WayseamFrame) and frame1.server_sequence == 11
    assert isinstance(frame2, WayseamFrame) and frame2.server_sequence == 22
    assert (frame2.width, frame2.height) == (3, 1)


# --- file-backed ring / open_ring --------------------------------------------

def test_shm_frame_source_over_real_file(tmp_path: Path) -> None:
    buf = build_ring()
    publish(buf, 0, 2, full_blob(2, 2, 5))
    ring = tmp_path / "wayseam-ivshmem.bin"
    ring.write_bytes(bytes(buf))
    with ShmFrameSource(ring, 0) as source:
        frame = source.poll(None)
        assert isinstance(frame, WayseamFrame)
        assert frame.server_sequence == 5
        assert source.last_sequence == 2


def test_open_ring_happy_and_failures(tmp_path: Path) -> None:
    ring = tmp_path / "ring.bin"
    ring.write_bytes(bytes(build_ring()))
    opened = open_ring(ring)
    assert opened is not None
    header, mapped = opened
    assert header.slot_count == 3
    mapped.close()

    assert open_ring(tmp_path / "missing.bin") is None
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"nope")
    assert open_ring(garbage) is None
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert open_ring(empty) is None


# --- default_ring_path --------------------------------------------------------

def test_default_ring_path_without_cfg() -> None:
    assert default_ring_path() == Path.home() / "Windows" / "wayseam-ivshmem.bin"


def test_default_ring_path_with_cfg(tmp_path: Path) -> None:
    cfg = SimpleNamespace(pod=SimpleNamespace(home_share=str(tmp_path)))
    assert default_ring_path(cfg) == tmp_path / "wayseam-ivshmem.bin"


def _input_ring(with_magic=True):

    from wayseam.present import shm as m

    buf = bytearray(4 * 1024 * 1024)
    if with_magic:
        buf[m.INPUT_MAGIC_OFFSET:m.INPUT_MAGIC_OFFSET + 8] = m.INPUT_MAGIC
    return buf


def test_input_writer_requires_the_guest_consumer_magic():
    from wayseam.present.shm import ShmInputWriter

    writer = ShmInputWriter(_input_ring(with_magic=False))
    assert not writer.available
    assert writer.move(0x1, 5, 5) is False
    # The consumer starting later is picked up by refresh().
    buf = _input_ring(with_magic=True)
    writer = ShmInputWriter(_input_ring(with_magic=False))
    writer._buf = buf
    assert writer.refresh() is True


def test_input_writer_packs_entries_and_advances_head():
    import struct as _s

    from wayseam.present import shm as m

    buf = _input_ring()
    writer = m.ShmInputWriter(buf)
    assert writer.move(0xAB, 10, 20)
    assert writer.wheel(0xAB, 10, 20, -120, 240)
    assert writer.key(0xAB, 0x41, down=True, extended=False)
    assert writer.unicode(0xAB, 0x131)  # dotless i

    head = _s.unpack_from("<Q", buf, m.INPUT_HEAD_OFFSET)[0]
    assert head == 4
    kind, _f, hwnd, a, b, c, d, _e, _g = _s.unpack_from(
        "<IIqiiiiii", buf, m.INPUT_ENTRIES_OFFSET + m.INPUT_ENTRY_SIZE,
    )
    assert (kind, hwnd, a, b, c, d) == (m.INPUT_WHEEL, 0xAB, 10, 20, -120, 240)


def test_input_writer_flush_waits_for_the_guest_tail():
    from wayseam.present import shm as m

    buf = _input_ring()
    now = {"t": 0.0}
    writer = m.ShmInputWriter(buf, clock=lambda: now["t"])
    writer.move(0x1, 1, 1)
    # Guest has not consumed: flush times out (clock-driven, no real sleep).
    import struct as _s

    def advance_then_check():
        buf[m.INPUT_TAIL_OFFSET:m.INPUT_TAIL_OFFSET + 8] = _s.pack("<Q", 1)

    assert writer.flush(timeout=-1.0) is False
    advance_then_check()
    assert writer.flush(timeout=0.01) is True


def test_input_writer_backpressure_when_ring_is_full():
    import struct as _s

    from wayseam.present import shm as m

    buf = _input_ring()
    buf[m.INPUT_HEAD_OFFSET:m.INPUT_HEAD_OFFSET + 8] = _s.pack("<Q", m.INPUT_ENTRY_COUNT - 1)
    writer = m.ShmInputWriter(buf)
    assert writer.move(0x1, 1, 1) is False  # guest stalled -> HTTP fallback
