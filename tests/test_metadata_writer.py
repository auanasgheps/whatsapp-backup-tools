"""
Unit tests for shared/metadata_writer.py (pure-Python EXIF and MP4 timestamp writer).
"""

import os
import struct
from datetime import UTC

from shared import metadata_writer


def _make_box(box_type: bytes, payload: bytes) -> bytes:
    size = len(payload) + 8
    return struct.pack(">I", size) + box_type + payload


def _make_fullbox(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    ver_flags = (version << 24) | (flags & 0xFFFFFF)
    return _make_box(box_type, struct.pack(">I", ver_flags) + payload)


def _build_synthetic_mp4(version: int) -> bytes:
    ftyp = _make_box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    if version == 0:
        mvhd_body = struct.pack(">IIII", 0, 0, 1000, 5000) + b"\x00" * 80
        tkhd_body = struct.pack(">IIII", 0, 0, 1, 0) + struct.pack(">I", 5000) + b"\x00" * 60
        mdhd_body = struct.pack(">IIII", 0, 0, 1000, 5000) + b"\x00" * 4
    else:
        mvhd_body = struct.pack(">QQII", 0, 0, 1000, 5000) + b"\x00" * 80
        tkhd_body = struct.pack(">QQII", 0, 0, 1, 0) + struct.pack(">I", 5000) + b"\x00" * 60
        mdhd_body = struct.pack(">QQII", 0, 0, 1000, 5000) + b"\x00" * 4

    mvhd = _make_fullbox(b"mvhd", version, 0, mvhd_body)
    tkhd = _make_fullbox(b"tkhd", version, 1, tkhd_body)
    mdhd = _make_fullbox(b"mdhd", version, 0, mdhd_body)
    mdia = _make_box(b"mdia", mdhd)
    trak = _make_box(b"trak", tkhd + mdia)
    moov = _make_box(b"moov", mvhd + trak)
    mdat = _make_box(b"mdat", b"dummy video media stream data")
    return ftyp + moov + mdat


def _build_minimal_jpeg(with_app0: bool) -> bytes:
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" if with_app0 else b""
    dqt = b"\xff\xdb\x00\x05\x00\x01\x02"
    sof = b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    entropy = b"\x12\x34\x56\x78"
    eoi = b"\xff\xd9"
    return soi + app0 + dqt + sof + sos + entropy + eoi


def _parse_test_exif(tiff_data: bytes):
    endian = "<" if tiff_data[:2] == b"II" else ">"
    magic, ifd0_offset = struct.unpack(endian + "HI", tiff_data[2:8])
    assert magic == 42

    def read_ifd(offset: int):
        entries_count = struct.unpack(endian + "H", tiff_data[offset : offset + 2])[0]
        tags = {}
        curr = offset + 2
        for _ in range(entries_count):
            tag, typ, count, val_or_off = struct.unpack(
                endian + "HHI I", tiff_data[curr : curr + 12]
            )
            curr += 12
            if typ == 2:
                val = (
                    tiff_data[val_or_off : val_or_off + count]
                    .decode("ascii", errors="replace")
                    .rstrip("\x00")
                )
            else:
                val = val_or_off
            tags[tag] = val
        return tags

    ifd0 = read_ifd(ifd0_offset)
    exif = read_ifd(ifd0[0x8769])
    return ifd0, exif


def test_format_exif_datetime():
    # 2026-05-10 12:30:00 UTC = 1778416200000 ms
    ts_ms = 1778416200000
    dt_str, tz_str = metadata_writer.format_exif_datetime(ts_ms, UTC)
    assert dt_str == "2026:05:10 12:30:00"
    assert tz_str == "+00:00"

    # None timezone
    dt_local, tz_none = metadata_writer.format_exif_datetime(ts_ms, None)
    assert len(dt_local) == 19
    assert tz_none is None


def test_build_jpeg_exif_app1_roundtrip():
    dt_str = "2026:05:10 14:30:00"
    tz_str = "+02:00"
    seg = metadata_writer.build_jpeg_exif_app1(dt_str, tz_str)

    assert seg[:2] == b"\xff\xe1"
    seg_len = struct.unpack(">H", seg[2:4])[0]
    assert seg_len == len(seg) - 2
    assert seg[4:10] == b"Exif\x00\x00"

    tiff_payload = seg[10:]
    ifd0, exif = _parse_test_exif(tiff_payload)

    assert ifd0[0x0132] == dt_str
    assert ifd0[0x9010] == tz_str
    assert exif[0x9003] == dt_str
    assert exif[0x9004] == dt_str
    assert exif[0x9011] == tz_str


def test_inject_jpeg_exif_without_app0():
    jpeg = _build_minimal_jpeg(with_app0=False)
    modified = metadata_writer.inject_jpeg_exif(jpeg, "2026:05:10 14:30:00", None)
    assert modified is not None
    # APP1 should be immediately after SOI (offset 2)
    assert modified[:2] == b"\xff\xd8"
    assert modified[2:4] == b"\xff\xe1"


def test_inject_jpeg_exif_with_app0():
    jpeg = _build_minimal_jpeg(with_app0=True)
    modified = metadata_writer.inject_jpeg_exif(jpeg, "2026:05:10 14:30:00", "+01:00")
    assert modified is not None
    # APP0 should remain at offset 2
    assert modified[:2] == b"\xff\xd8"
    assert modified[2:4] == b"\xff\xe0"
    app0_len = struct.unpack(">H", modified[4:6])[0]
    app1_offset = 2 + 2 + app0_len
    # APP1 must follow APP0
    assert modified[app1_offset : app1_offset + 2] == b"\xff\xe1"


def test_preserve_existing_valid_camera_exif():
    # Build a JPEG that already has valid EXIF
    jpeg = _build_minimal_jpeg(with_app0=False)
    existing_exif_seg = metadata_writer.build_jpeg_exif_app1("2020:01:01 10:00:00", None)
    jpeg_with_exif = jpeg[:2] + existing_exif_seg + jpeg[2:]

    # Injecting should return None (preserved)
    result = metadata_writer.inject_jpeg_exif(jpeg_with_exif, "2026:05:10 14:30:00", None)
    assert result is None


def test_preserve_existing_orientation():
    # Construct a minimal TIFF with orientation tag (0x0112)
    header = b"II\x2a\x00\x08\x00\x00\x00"
    ifd0 = struct.pack("<H", 1)
    ifd0 += struct.pack("<HHI I", 0x0112, 3, 1, 6)  # Orientation: 6 (Rotate 90 CW)
    ifd0 += struct.pack("<I", 0)
    tiff = header + ifd0
    assert metadata_writer.has_orientation_tag(tiff) is True

    app1_payload = b"Exif\x00\x00" + tiff
    seg = b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
    jpeg = _build_minimal_jpeg(with_app0=False)
    jpeg_with_orientation = jpeg[:2] + seg + jpeg[2:]

    result = metadata_writer.inject_jpeg_exif(jpeg_with_orientation, "2026:05:10 14:30:00", None)
    assert result is None  # Preserved to protect portrait orientation!


def test_has_valid_camera_datetime_dummy():
    # Dummy zero timestamp should not be considered valid camera date
    seg = metadata_writer.build_jpeg_exif_app1("0000:00:00 00:00:00", None)
    tiff = seg[10:]
    assert metadata_writer.has_valid_camera_datetime(tiff) is False


def test_patch_mp4_timestamps_v0(tmp_path):
    mp4_bytes = _build_synthetic_mp4(version=0)
    mp4_path = tmp_path / "test_v0.mp4"
    mp4_path.write_bytes(mp4_bytes)

    # 2026-05-10 12:00:00 UTC = 1778414400 s
    ts_ms = 1778414400000
    res = metadata_writer.patch_mp4_timestamps(str(mp4_path), ts_ms)
    assert res is True

    expected_qt = 1778414400 + metadata_writer.EPOCH_1904
    data = mp4_path.read_bytes()
    idx = data.find(b"mvhd")
    c_time = struct.unpack(">I", data[idx + 8 : idx + 12])[0]
    assert c_time == expected_qt


def test_patch_mp4_timestamps_v1(tmp_path):
    mp4_bytes = _build_synthetic_mp4(version=1)
    mp4_path = tmp_path / "test_v1.mp4"
    mp4_path.write_bytes(mp4_bytes)

    ts_ms = 1778414400000
    res = metadata_writer.patch_mp4_timestamps(str(mp4_path), ts_ms)
    assert res is True

    expected_qt = 1778414400 + metadata_writer.EPOCH_1904
    data = mp4_path.read_bytes()
    idx = data.find(b"mvhd")
    c_time = struct.unpack(">Q", data[idx + 8 : idx + 16])[0]
    assert c_time == expected_qt


def test_write_media_timestamp_hardlink_safety(tmp_path):
    # Calling with is_hardlink=True must return False immediately without touching the file
    p = tmp_path / "test.jpg"
    p.write_bytes(b"dummy")
    res = metadata_writer.write_media_timestamp(str(p), 1778414400000, None, True)
    assert res is False
    assert p.read_bytes() == b"dummy"


def test_write_media_timestamp_invalid_timestamp(tmp_path):
    p = tmp_path / "test.jpg"
    p.write_bytes(b"dummy")
    assert metadata_writer.write_media_timestamp(str(p), 0, None, False) is False
    assert metadata_writer.write_media_timestamp(str(p), -100, None, False) is False


def test_write_media_timestamp_unsupported_format(tmp_path):
    p = tmp_path / "test.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n...")
    res = metadata_writer.write_media_timestamp(str(p), 1778414400000, None, False)
    assert res is False


def test_write_media_timestamp_jpeg_e2e(tmp_path):
    raw_jpeg = _build_minimal_jpeg(with_app0=True)
    jpeg_path = tmp_path / "sample.jpg"
    jpeg_path.write_bytes(raw_jpeg)

    ts_ms = 1778416200000
    res = metadata_writer.write_media_timestamp(str(jpeg_path), ts_ms, UTC, False)
    assert res is True

    # Check file on disk now has valid APP1
    modified = jpeg_path.read_bytes()
    assert b"Exif\x00\x00" in modified
    assert b"2026:05:10 12:30:00" in modified


def test_copy_or_skip_exif_integration_and_incremental_no_collision(tmp_path):
    import logging

    from wab_archiver import archive_db as arc
    from wab_archiver import main as wa

    logger = logging.getLogger("test_archiver")
    out = tmp_path / "out"
    out.mkdir()
    conn = arc.open_archive_db(str(out))
    cursor = conn.cursor()

    src = tmp_path / "IMG-001.jpg"
    src.write_bytes(_build_minimal_jpeg(with_app0=True))
    dest_path = out / "Contacts" / "Alice" / "2026" / "IMG-001.jpg"
    dest_dir = dest_path.parent

    # Run 1: Copy with exif_timestamps=True
    ts_ms = 1778416200000
    res1 = wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(dest_dir),
        "Media/WhatsApp Images/IMG-001.jpg",
        ts_ms,
        str(out),
        False,
        logger,
        cursor,
        False,
        {},
        True,
        UTC,
    )
    conn.commit()
    assert res1 == (1, 0, 0)
    assert dest_path.exists()
    assert b"Exif\x00\x00" in dest_path.read_bytes()

    # Run 2: Incremental run with the exact same file
    res2 = wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(dest_dir),
        "Media/WhatsApp Images/IMG-001.jpg",
        ts_ms,
        str(out),
        False,
        logger,
        cursor,
        False,
        {},
        True,
        UTC,
    )
    conn.commit()
    # Must be skipped (0, 1, 0) with zero collision copy created
    assert res2 == (0, 1, 0)
    collision_candidate = out / "Contacts" / "Alice" / "2026" / "IMG-001_1.jpg"
    assert not collision_candidate.exists()
    conn.close()


def test_copy_or_skip_exif_disabled_by_default(tmp_path):
    import logging

    from wab_archiver import main as wa

    logger = logging.getLogger("test_archiver")
    out = tmp_path / "out"
    out.mkdir()

    raw_bytes = _build_minimal_jpeg(with_app0=True)
    src = tmp_path / "IMG-002.jpg"
    src.write_bytes(raw_bytes)
    dest_path = out / "IMG-002.jpg"

    ts_ms = 1778416200000
    # exif_timestamps defaults to False
    res = wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-002.jpg",
        ts_ms,
        str(out),
        False,
        logger,
        None,
        False,
        {},
    )
    assert res == (1, 0, 0)
    assert dest_path.exists()
    # Content must match exactly (no EXIF injected)
    assert dest_path.read_bytes() == raw_bytes


def test_load_archived_file_set_and_query_elimination(tmp_path):
    import logging

    from wab_archiver import archive_db as arc
    from wab_archiver import main as wa

    logger = logging.getLogger("test_archiver")
    out = tmp_path / "out"
    out.mkdir()
    conn = arc.open_archive_db(str(out))
    cursor = conn.cursor()

    raw_bytes = _build_minimal_jpeg(with_app0=True)
    src = tmp_path / "IMG-003.jpg"
    src.write_bytes(raw_bytes)
    dest_path = out / "IMG-003.jpg"
    ts_ms = 1778416200000

    # Archive file once
    wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-003.jpg",
        ts_ms,
        str(out),
        False,
        logger,
        cursor,
        False,
        {},
        True,
        UTC,
    )
    conn.commit()

    # Pre-fetch archived file set
    archived_set = arc.load_archived_file_set(conn)
    src_hash = wa.file_md5(str(src))
    assert ("Media/WhatsApp Images/IMG-003.jpg", "IMG-003.jpg", src_hash) in archived_set

    # Second run using in-memory set with cursor=None (demonstrates 0 SQL queries needed)
    res_cached = wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-003.jpg",
        ts_ms,
        str(out),
        False,
        logger,
        None,  # No cursor: verified via in-memory set!
        False,
        {},
        True,
        UTC,
        archived_set,
    )
    assert res_cached == (0, 1, 0)
    conn.close()


def test_m4a_audio_container_timestamp_patching(tmp_path):
    m4a_bytes = _build_synthetic_mp4(version=0)
    m4a_path = tmp_path / "voice_message.m4a"
    m4a_path.write_bytes(m4a_bytes)

    ts_ms = 1778414400000
    res = metadata_writer.write_media_timestamp(str(m4a_path), ts_ms, None, False)
    assert res is True

    expected_qt = 1778414400 + metadata_writer.EPOCH_1904
    data = m4a_path.read_bytes()
    idx = data.find(b"mvhd")
    c_time = struct.unpack(">I", data[idx + 8 : idx + 12])[0]
    assert c_time == expected_qt


def test_permission_error_graceful_handling(tmp_path, monkeypatch):
    raw_jpeg = _build_minimal_jpeg(with_app0=True)
    jpeg_path = tmp_path / "readonly.jpg"
    jpeg_path.write_bytes(raw_jpeg)

    # Monkeypatch os.replace to simulate Windows read-only attribute Access Denied
    def _mock_replace(src, dst):
        raise PermissionError("Access is denied (read-only file)")

    monkeypatch.setattr(os, "replace", _mock_replace)

    ts_ms = 1778416200000
    res = metadata_writer.write_media_timestamp(str(jpeg_path), ts_ms, UTC, False)
    assert res is False
