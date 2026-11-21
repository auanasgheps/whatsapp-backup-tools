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
        keys = list(tags.keys())
        assert keys == sorted(keys), f"IFD tags at offset {offset} not sorted: {keys}"
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

    # IFD0 contains DateTime and ExifOffset, strictly sorted
    assert ifd0[0x0132] == dt_str
    assert 0x8769 in ifd0
    # Exif Sub-IFD contains DateTimeOriginal, DateTimeDigitized, and Exif 2.31 offset tags, strictly sorted
    assert exif[0x9003] == dt_str
    assert exif[0x9004] == dt_str
    assert exif[0x9010] == tz_str
    assert exif[0x9011] == tz_str
    assert exif[0x9012] == tz_str


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


def test_archive_db_exif_stamped_column_migration(tmp_path):
    import sqlite3

    from wab_archiver import archive_db as arc

    out = tmp_path / "out"
    out.mkdir()
    db_file = out / ".wa_media_archiver.db"

    # Create a pre-migration DB missing exif_stamped column with user_version = 6
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA user_version = 6")
    conn.executescript("""
        CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT);
        CREATE TABLE files (original_path TEXT PRIMARY KEY, md5 BLOB, size INTEGER);
        CREATE TABLE archive_copies (
            original_path TEXT NOT NULL REFERENCES files(original_path),
            archive_path TEXT NOT NULL,
            is_hardlink INTEGER DEFAULT 0,
            timestamp_ms INTEGER,
            PRIMARY KEY (original_path, archive_path)
        );
    """)
    conn.commit()
    conn.close()

    # Open via open_archive_db and verify migration adds column while preserving user_version = 6
    migrated_conn = arc.open_archive_db(str(out))
    cols = {r[1] for r in migrated_conn.execute("PRAGMA table_info(archive_copies)").fetchall()}
    assert "exif_stamped" in cols
    assert migrated_conn.execute("PRAGMA user_version").fetchone()[0] == 6

    # Test helper methods
    cur = migrated_conn.cursor()
    cur.execute("INSERT INTO files VALUES ('p1', X'11', 10)")
    cur.execute("INSERT INTO archive_copies VALUES ('p1', 'rel1.jpg', 0, 1000, 0)")
    cur.execute("INSERT INTO files VALUES ('p2', X'22', 10)")
    cur.execute("INSERT INTO archive_copies VALUES ('p2', 'rel2.jpg', 1, 2000, 0)")
    migrated_conn.commit()

    pending = arc.get_pending_metadata_copies(migrated_conn)
    assert pending == [("rel1.jpg", 1000)]

    arc.mark_metadata_stamped(cur, "rel1.jpg", 1)
    migrated_conn.commit()

    assert arc.get_pending_metadata_copies(migrated_conn) == []
    all_copies = arc.get_all_metadata_copies(migrated_conn)
    assert all_copies == [("rel1.jpg", 1000)]
    migrated_conn.close()


def test_update_metadata_subcommand_e2e(tmp_path):
    import argparse
    import logging
    import sys

    from wab_archiver import archive_db as arc
    from wab_archiver import main as wa

    logger = logging.getLogger("test_update_metadata")
    out = tmp_path / "out"
    out.mkdir()

    # Create dummy media
    img_bytes = _build_minimal_jpeg(with_app0=True)
    img_path = out / "photo.jpg"
    img_path.write_bytes(img_bytes)

    # Standalone photo (not hardlinked)
    photo_standalone = out / "photo_standalone.jpg"
    photo_standalone.write_bytes(img_bytes)

    mp4_bytes = _build_synthetic_mp4(version=0)
    mp4_path = out / "video.mp4"
    mp4_path.write_bytes(mp4_bytes)

    m4a_bytes = _build_synthetic_mp4(version=0)
    m4a_path = out / "audio.m4a"
    m4a_path.write_bytes(m4a_bytes)

    # Hardlink
    link_path = out / "photo_link.jpg"
    try:
        os.link(str(img_path), str(link_path))
        has_hardlink = True
    except OSError:
        has_hardlink = False

    # Unsupported format
    txt_path = out / "doc.txt"
    txt_path.write_bytes(b"hello text")

    # Set up DB
    conn = arc.open_archive_db(str(out))
    cur = conn.cursor()
    ts_ms = 1778414400000

    arc.record_file_archived(
        cur, "Media/photo_standalone.jpg", b"10", "photo_standalone.jpg", len(img_bytes), 0, ts_ms
    )
    arc.record_file_archived(cur, "Media/photo.jpg", b"1", "photo.jpg", len(img_bytes), 0, ts_ms)
    arc.record_file_archived(cur, "Media/video.mp4", b"2", "video.mp4", len(mp4_bytes), 0, ts_ms)
    arc.record_file_archived(cur, "Media/audio.m4a", b"3", "audio.m4a", len(m4a_bytes), 0, ts_ms)
    if has_hardlink:
        arc.record_file_archived(
            cur, "Media/link.jpg", b"1", "photo_link.jpg", len(img_bytes), 1, ts_ms
        )
    arc.record_file_archived(cur, "Media/doc.txt", b"4", "doc.txt", len(b"hello text"), 0, ts_ms)
    # File in DB but missing from disk
    arc.record_file_archived(cur, "Media/missing.jpg", b"5", "missing.jpg", 100, 0, ts_ms)
    conn.commit()

    # 1. Dry run test
    dry_args = argparse.Namespace(
        output=str(out),
        timezone=None,
        force=False,
        dry_run=True,
    )
    wa.run_update_metadata_mode(dry_args, logger)

    # Verify no files modified in dry run
    assert arc.get_pending_metadata_copies(conn) != []
    assert metadata_writer.has_valid_camera_datetime(photo_standalone.read_bytes()[6:]) is False

    # 2. Real run
    real_args = argparse.Namespace(
        output=str(out),
        timezone="UTC",
        force=False,
        dry_run=False,
    )
    wa.run_update_metadata_mode(real_args, logger)

    # Verify standalone JPEG got EXIF
    app1_idx = photo_standalone.read_bytes().find(b"\xff\xe1")
    assert app1_idx != -1
    # Verify mtime was preserved
    assert abs(os.path.getmtime(str(photo_standalone)) - (ts_ms / 1000.0)) < 2.0
    if sys.platform == "win32":
        assert abs(os.stat(str(photo_standalone)).st_ctime - (ts_ms / 1000.0)) < 2.0

    # Verify MP4 got patched
    mp4_data = mp4_path.read_bytes()
    idx = mp4_data.find(b"mvhd")
    c_time = struct.unpack(">I", mp4_data[idx + 8 : idx + 12])[0]
    assert c_time == 1778414400 + metadata_writer.EPOCH_1904
    assert abs(os.path.getmtime(str(mp4_path)) - (ts_ms / 1000.0)) < 2.0

    # Verify M4A got patched
    m4a_data = m4a_path.read_bytes()
    idx = m4a_data.find(b"mvhd")
    c_time_m4a = struct.unpack(">I", m4a_data[idx + 8 : idx + 12])[0]
    assert c_time_m4a == 1778414400 + metadata_writer.EPOCH_1904

    # Verify database status
    row_standalone = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'photo_standalone.jpg'"
    ).fetchone()
    assert row_standalone[0] == 1
    if has_hardlink:
        row_hardlink = conn.execute(
            "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'photo.jpg'"
        ).fetchone()
        assert row_hardlink[0] == 2
    row_mp4 = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'video.mp4'"
    ).fetchone()
    assert row_mp4[0] == 1
    row_txt = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'doc.txt'"
    ).fetchone()
    assert row_txt[0] == 2
    row_miss = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'missing.jpg'"
    ).fetchone()
    assert row_miss[0] == 3

    # 3. Re-run idempotency: pending should be empty
    assert arc.get_pending_metadata_copies(conn) == []
    wa.run_update_metadata_mode(real_args, logger)

    # 4. Force run
    force_args = argparse.Namespace(
        output=str(out),
        timezone="UTC",
        force=True,
        dry_run=False,
    )
    wa.run_update_metadata_mode(force_args, logger)
    conn.close()


def test_copy_or_skip_marks_exif_stamped_on_ingestion(tmp_path):
    import logging

    from wab_archiver import archive_db as arc
    from wab_archiver import main as wa

    logger = logging.getLogger("test_ingestion_stamp")
    out = tmp_path / "out"
    out.mkdir()
    conn = arc.open_archive_db(str(out))
    cursor = conn.cursor()

    raw_bytes = _build_minimal_jpeg(with_app0=True)
    src = tmp_path / "IMG-004.jpg"
    src.write_bytes(raw_bytes)
    dest_path = out / "IMG-004.jpg"
    ts_ms = 1778416200000

    wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-004.jpg",
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

    # Verify exif_stamped is set to 1 directly on ingestion
    row = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'IMG-004.jpg'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    conn.close()


def test_set_file_times_windows_creation_time(tmp_path):
    import sys

    from wab_archiver import main as wa

    test_file = tmp_path / "timestamp_test.jpg"
    test_file.write_bytes(b"sample bytes")

    ts_ms = 1621512000000
    wa.set_file_times(str(test_file), ts_ms)

    st = os.stat(str(test_file))
    assert abs(st.st_mtime - (ts_ms / 1000.0)) < 2.0
    if sys.platform == "win32":
        assert abs(st.st_ctime - (ts_ms / 1000.0)) < 2.0


def test_copy_or_skip_dry_run_stamped_no_false_collision(tmp_path):
    """Verify dry_run on an archive with stamped files does not falsely report collisions."""
    import logging

    from wab_archiver import archive_db as arc
    from wab_archiver import main as wa

    logger = logging.getLogger("test_dry_run_stamped")
    out = tmp_path / "out"
    out.mkdir()
    conn = arc.open_archive_db(str(out))
    cursor = conn.cursor()

    raw_bytes = _build_minimal_jpeg(with_app0=True)
    src = tmp_path / "IMG-DRY.jpg"
    src.write_bytes(raw_bytes)
    dest_path = out / "IMG-DRY.jpg"
    ts_ms = 1778416200000

    # 1. Archive real with exif_timestamps=True (modifies dest file on disk)
    wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-DRY.jpg",
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
    assert dest_path.exists()
    assert dest_path.read_bytes() != raw_bytes  # Stamped with EXIF!

    # 2. Dry run with cursor provided
    dry_res = wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-DRY.jpg",
        ts_ms,
        str(out),
        True,  # dry_run
        logger,
        cursor,
        False,
        {},
        True,
        UTC,
    )
    assert dry_res == (0, 1, 0)
    # Ensure no collision candidate was created
    assert not (out / "IMG-DRY_1.jpg").exists()

    # 3. Dry run with archived_file_set preloaded
    archived_set = arc.load_archived_file_set(conn)
    dry_res_set = wa._copy_or_skip(
        str(src),
        str(dest_path),
        str(out),
        "Media/WhatsApp Images/IMG-DRY.jpg",
        ts_ms,
        str(out),
        True,  # dry_run
        logger,
        None,
        False,
        {},
        True,
        UTC,
        archived_set,
    )
    assert dry_res_set == (0, 1, 0)
    conn.close()


def test_update_metadata_force_preserves_stamped_status(tmp_path):
    """Verify update-metadata --force does not demote exif_stamped=1 to 2 for JPEGs."""
    import argparse
    import logging

    from wab_archiver import archive_db as arc
    from wab_archiver import main as wa

    logger = logging.getLogger("test_force_preserve")
    out = tmp_path / "out"
    out.mkdir()
    conn = arc.open_archive_db(str(out))
    cur = conn.cursor()

    raw_bytes = _build_minimal_jpeg(with_app0=True)
    img = out / "stamped.jpg"
    img.write_bytes(raw_bytes)
    ts_ms = 1778414400000

    arc.record_file_archived(
        cur, "Media/stamped.jpg", b"hash", "stamped.jpg", len(raw_bytes), 0, ts_ms
    )
    conn.commit()

    # Initial run to stamp
    args = argparse.Namespace(output=str(out), timezone="UTC", force=False, dry_run=False)
    wa.run_update_metadata_mode(args, logger)

    row = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'stamped.jpg'"
    ).fetchone()
    assert row[0] == 1

    # Force re-evaluation
    force_args = argparse.Namespace(output=str(out), timezone="UTC", force=True, dry_run=False)
    wa.run_update_metadata_mode(force_args, logger)

    row_after = conn.execute(
        "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'stamped.jpg'"
    ).fetchone()
    assert row_after[0] == 1  # Preserved as 1, not demoted to 2!
    conn.close()


def test_write_media_timestamp_tmp_cleanup_on_error(tmp_path, monkeypatch):
    """Verify .tmp file is cleaned up if os.replace fails."""
    raw_jpeg = _build_minimal_jpeg(with_app0=True)
    jpeg_path = tmp_path / "fail_replace.jpg"
    jpeg_path.write_bytes(raw_jpeg)
    tmp_file = tmp_path / "fail_replace.jpg.tmp"

    def _failing_replace(src, dst):
        assert os.path.exists(src)
        raise OSError("Simulated disk/lock failure during replace")

    monkeypatch.setattr(os, "replace", _failing_replace)

    res = metadata_writer.write_media_timestamp(str(jpeg_path), 1778416200000, UTC, False)
    assert res is False
    assert not tmp_file.exists()  # .tmp must be removed by finally block!
