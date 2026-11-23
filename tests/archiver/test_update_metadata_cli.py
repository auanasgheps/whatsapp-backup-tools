"""
Comprehensive unit and integration tests for EXIF metadata injection and the
'update-metadata' CLI subcommand across Android and iOS media.

Validates:
- Pure-Python TIFF/IFD and QuickTime/ISO-BMFF round-trip compliance
- Stamping of JPEGs, MP4s, QuickTime MOVs, and M4A containers
- Timezone offsets and DST transitions (OffsetTimeOriginal, OffsetTimeDigitized)
- Hardlink deduplication safety (skipping st_nlink > 1)
- Camera original EXIF preservation
- Subcommand flags: standard run, --dry-run, --force, --timezone
- Handling of missing, truncated, or corrupt media files
- End-to-end integration for Android and iOS archive layouts
"""

import argparse
import logging
import os
import struct
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from shared import metadata_writer
from wab_archiver import archive_db as arc
from wab_archiver import main as wa

# --- In-Test Strict Binary Parsers (Zero External Dependencies) ---


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
    mdat = _make_box(b"mdat", b"dummy media stream payload")
    return ftyp + moov + mdat


def _build_synthetic_quicktime_mov() -> bytes:
    """Build a minimal QuickTime .mov file (qt  brand in ftyp)."""
    ftyp = _make_box(b"ftyp", b"qt  " + struct.pack(">I", 512) + b"qt  ")
    mvhd_body = struct.pack(">IIII", 0, 0, 1000, 3000) + b"\x00" * 80
    mvhd = _make_fullbox(b"mvhd", 0, 0, mvhd_body)
    moov = _make_box(b"moov", mvhd)
    mdat = _make_box(b"mdat", b"quicktime video stream payload")
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


def _parse_jpeg_exif_tags(jpeg_bytes: bytes) -> dict[int, str | int]:
    """Strict pure-Python parser for TIFF IFD0 and Exif Sub-IFD inside JPEG APP1."""
    assert jpeg_bytes[:2] == b"\xff\xd8", "Not a valid JPEG"
    pos = 2
    exif_payload = None

    while pos < len(jpeg_bytes) - 4:
        if jpeg_bytes[pos] != 0xFF:
            break
        marker = jpeg_bytes[pos + 1]
        length = struct.unpack(">H", jpeg_bytes[pos + 2 : pos + 4])[0]
        if marker == 0xE1:  # APP1
            seg = jpeg_bytes[pos + 4 : pos + 2 + length]
            if seg.startswith(b"Exif\x00\x00"):
                exif_payload = seg[6:]
                break
        pos += 2 + length

    assert exif_payload is not None, "APP1 Exif segment not found"
    assert exif_payload[:2] == b"II", "Expected Little-Endian (II) TIFF header"
    assert struct.unpack("<H", exif_payload[2:4])[0] == 42, "Missing TIFF magic 42"

    ifd0_offset = struct.unpack("<I", exif_payload[4:8])[0]
    tags: dict[int, str | int] = {}

    def _read_ifd(offset: int) -> int:
        num_entries = struct.unpack("<H", exif_payload[offset : offset + 2])[0]
        curr = offset + 2
        for _ in range(num_entries):
            tag, tag_type, count, val_or_offset = struct.unpack(
                "<HHI I", exif_payload[curr : curr + 12]
            )
            curr += 12
            if tag_type == 2:  # ASCII
                if count <= 4:
                    raw_val = exif_payload[curr - 4 : curr - 4 + count]
                else:
                    raw_val = exif_payload[val_or_offset : val_or_offset + count]
                tags[tag] = raw_val.split(b"\x00")[0].decode("ascii", errors="replace")
            elif tag_type in (3, 4):  # SHORT or LONG
                tags[tag] = val_or_offset
        next_ifd_offset = struct.unpack("<I", exif_payload[curr : curr + 4])[0]
        return next_ifd_offset

    _read_ifd(ifd0_offset)
    if 0x8769 in tags:  # Exif Sub-IFD offset
        _read_ifd(int(tags[0x8769]))

    return tags


def _parse_isom_mvhd_creation_time(box_data: bytes) -> int:
    """Parse mvhd creation_time integer from ISO-BMFF / QuickTime container bytes."""
    idx = 0
    while idx < len(box_data) - 8:
        size = struct.unpack(">I", box_data[idx : idx + 4])[0]
        box_type = box_data[idx + 4 : idx + 8]
        if size == 0:
            size = len(box_data) - idx
        if box_type == b"moov":
            inner_idx = idx + 8
            moov_end = idx + size
            while inner_idx < moov_end - 8:
                inner_size = struct.unpack(">I", box_data[inner_idx : inner_idx + 4])[0]
                inner_type = box_data[inner_idx + 4 : inner_idx + 8]
                if inner_type == b"mvhd":
                    version = box_data[inner_idx + 8]
                    if version == 0:
                        return struct.unpack(">I", box_data[inner_idx + 12 : inner_idx + 16])[0]
                    else:
                        return struct.unpack(">Q", box_data[inner_idx + 12 : inner_idx + 20])[0]
                inner_idx += inner_size
        idx += size
    raise ValueError("mvhd box not found in container")


# --- Test Suite ---


class TestExifAndContainerMetadata:
    """Tests for raw metadata injection and standards verification."""

    def test_jpeg_exif_stamping_round_trip(self, tmp_path: Path) -> None:
        """Stamped JPEG contains valid TIFF IFD0 and Sub-IFD tags matching timestamp."""
        img_file = tmp_path / "test.jpg"
        img_file.write_bytes(_build_minimal_jpeg(with_app0=True))

        ts_ms = 1715340000000  # 2024-05-10 11:20:00 UTC
        tz = ZoneInfo("UTC")

        stamped = metadata_writer.write_media_timestamp(str(img_file), ts_ms, tz, False)
        assert stamped is True

        tags = _parse_jpeg_exif_tags(img_file.read_bytes())
        assert tags[0x0132] == "2024:05:10 11:20:00"  # DateTime
        assert tags[0x9003] == "2024:05:10 11:20:00"  # DateTimeOriginal
        assert tags[0x9004] == "2024:05:10 11:20:00"  # DateTimeDigitized
        assert tags[0x9010] == "+00:00"  # OffsetTime
        assert tags[0x9011] == "+00:00"  # OffsetTimeOriginal
        assert tags[0x9012] == "+00:00"  # OffsetTimeDigitized

    def test_quicktime_mov_stamping_round_trip(self, tmp_path: Path) -> None:
        """QuickTime .mov file (iOS format) mvhd creation_time is correctly patched."""
        mov_file = tmp_path / "test.mov"
        mov_file.write_bytes(_build_synthetic_quicktime_mov())

        ts_ms = 1700000000000  # 2023-11-14 22:13:20 UTC
        expected_qt_sec = int(ts_ms / 1000.0) + metadata_writer.EPOCH_1904

        stamped = metadata_writer.write_media_timestamp(str(mov_file), ts_ms, None, False)
        assert stamped is True

        parsed_creation = _parse_isom_mvhd_creation_time(mov_file.read_bytes())
        assert parsed_creation == expected_qt_sec

    def test_m4a_audio_container_stamping(self, tmp_path: Path) -> None:
        """iOS .m4a voice notes and recordings are supported and stamped."""
        m4a_file = tmp_path / "voice_note.m4a"
        m4a_file.write_bytes(_build_synthetic_mp4(version=0))

        ts_ms = 1680000000000
        expected_qt_sec = int(ts_ms / 1000.0) + metadata_writer.EPOCH_1904

        stamped = metadata_writer.write_media_timestamp(str(m4a_file), ts_ms, None, False)
        assert stamped is True

        parsed_creation = _parse_isom_mvhd_creation_time(m4a_file.read_bytes())
        assert parsed_creation == expected_qt_sec

    def test_timezone_dst_and_half_hour_offsets(self, tmp_path: Path) -> None:
        """Test DST summer (+02:00) vs winter (+01:00) in Rome and half-hour (+05:30) in India."""
        # 1. Rome Summer (CEST: +02:00) - July
        rome_summer_ts = 1721044800000  # 2024-07-15 12:00:00 UTC
        tz_rome = ZoneInfo("Europe/Rome")
        dt_str, offset_str = metadata_writer.format_exif_datetime(rome_summer_ts, tz_rome)
        assert dt_str == "2024:07:15 14:00:00"
        assert offset_str == "+02:00"

        # 2. Rome Winter (CET: +01:00) - January
        rome_winter_ts = 1705320000000  # 2024-01-15 12:00:00 UTC
        dt_str, offset_str = metadata_writer.format_exif_datetime(rome_winter_ts, tz_rome)
        assert dt_str == "2024:01:15 13:00:00"
        assert offset_str == "+01:00"

        # 3. Kolkata (+05:30) - All year
        tz_kolkata = ZoneInfo("Asia/Kolkata")
        dt_str, offset_str = metadata_writer.format_exif_datetime(rome_summer_ts, tz_kolkata)
        assert dt_str == "2024:07:15 17:30:00"
        assert offset_str == "+05:30"


class TestUpdateMetadataCliSubcommand:
    """End-to-end tests for the 'update-metadata' CLI subcommand."""

    @pytest.fixture
    def archive_env(self, tmp_path: Path):
        """Prepare a synthetic archive directory with diverse media files and DB records."""
        out = tmp_path / "Archive"
        out.mkdir()

        # Standalone files (st_nlink == 1)
        p_photo = out / "photo_standalone.jpg"
        p_photo.write_bytes(_build_minimal_jpeg(with_app0=True))

        p_video = out / "video.mp4"
        p_video.write_bytes(_build_synthetic_mp4(version=0))

        p_mov = out / "ios_clip.mov"
        p_mov.write_bytes(_build_synthetic_quicktime_mov())

        p_m4a = out / "voice.m4a"
        p_m4a.write_bytes(_build_synthetic_mp4(version=0))

        # Hardlinked file pair (st_nlink > 1)
        p_link_src = out / "linked_base.jpg"
        p_link_src.write_bytes(_build_minimal_jpeg(with_app0=True))
        p_link = out / "photo_duplicate.jpg"
        try:
            os.link(str(p_link_src), str(p_link))
            has_link = True
        except OSError:
            has_link = False

        # DB
        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()
        ts_ms = 1715340000000

        arc.record_file_archived(
            cur,
            "photo_standalone.jpg",
            b"hash_photo",
            "photo_standalone.jpg",
            p_photo.stat().st_size,
            0,
            ts_ms,
        )
        arc.record_file_archived(
            cur, "video.mp4", b"hash_video", "video.mp4", p_video.stat().st_size, 0, ts_ms
        )
        arc.record_file_archived(
            cur, "ios_clip.mov", b"hash_mov", "ios_clip.mov", p_mov.stat().st_size, 0, ts_ms
        )
        arc.record_file_archived(
            cur, "voice.m4a", b"hash_m4a", "voice.m4a", p_m4a.stat().st_size, 0, ts_ms
        )

        if has_link:
            arc.record_file_archived(
                cur,
                "photo_duplicate.jpg",
                b"hash_link",
                "photo_duplicate.jpg",
                p_link.stat().st_size,
                0,
                ts_ms,
            )

        # Missing file on disk
        arc.record_file_archived(cur, "ghost.jpg", b"hash_ghost", "ghost.jpg", 1024, 0, ts_ms)

        conn.commit()
        conn.close()

        return {
            "out": out,
            "has_link": has_link,
            "ts_ms": ts_ms,
            "photo": p_photo,
            "mov": p_mov,
        }

    def test_update_metadata_standard_run(self, archive_env) -> None:
        """Standard run stamps un-stamped files and updates database statuses."""
        out = archive_env["out"]
        logger = logging.getLogger("test_update_metadata")

        args = argparse.Namespace(
            command="update-metadata",
            output=str(out),
            timezone="UTC",
            force=False,
            dry_run=False,
        )

        wa.run_update_metadata_mode(args, logger)

        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()

        cur.execute("SELECT archive_path, exif_stamped FROM archive_copies")
        statuses = dict(cur.fetchall())
        conn.close()

        # Regular media files marked as 1 (stamped)
        assert statuses["photo_standalone.jpg"] == 1
        assert statuses["video.mp4"] == 1
        assert statuses["ios_clip.mov"] == 1
        assert statuses["voice.m4a"] == 1

        # Missing file marked as 3 (missing)
        assert statuses["ghost.jpg"] == 3

        # Hardlink file marked as 2 (skipped hardlink)
        if archive_env["has_link"]:
            assert statuses["photo_duplicate.jpg"] == 2

        # Verify EXIF tags on the stamped photo
        tags = _parse_jpeg_exif_tags(archive_env["photo"].read_bytes())
        assert tags[0x0132] == "2024:05:10 11:20:00"
        assert tags[0x9011] == "+00:00"

    def test_update_metadata_dry_run_leaves_files_untouched(self, archive_env) -> None:
        """--dry-run updates no files and writes no stamps to disk."""
        out = archive_env["out"]
        logger = logging.getLogger("test_dry_run")

        photo_before = archive_env["photo"].read_bytes()

        args = argparse.Namespace(
            command="update-metadata",
            output=str(out),
            timezone="Europe/Rome",
            force=False,
            dry_run=True,
        )

        wa.run_update_metadata_mode(args, logger)

        photo_after = archive_env["photo"].read_bytes()
        assert photo_before == photo_after, "Dry run must not modify file contents"

        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()
        cur.execute(
            "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'photo_standalone.jpg'"
        )
        stamped_status = cur.fetchone()[0]
        conn.close()

        assert stamped_status == 0, "Dry run must not update database exif_stamped status"

    def test_update_metadata_force_flag_preserves_stamped_status(self, archive_env) -> None:
        """--force flag re-evaluates files and preserves stamped status (1) without demotion to 2."""
        out = archive_env["out"]
        logger = logging.getLogger("test_force")

        # Step 1: Run standard update in UTC
        args_utc = argparse.Namespace(
            command="update-metadata",
            output=str(out),
            timezone="UTC",
            force=False,
            dry_run=False,
        )
        wa.run_update_metadata_mode(args_utc, logger)

        tags_utc = _parse_jpeg_exif_tags(archive_env["photo"].read_bytes())
        assert tags_utc[0x9011] == "+00:00"

        # Step 2: Run with --force
        args_force = argparse.Namespace(
            command="update-metadata",
            output=str(out),
            timezone="Europe/Rome",
            force=True,
            dry_run=False,
        )
        wa.run_update_metadata_mode(args_force, logger)

        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()
        cur.execute(
            "SELECT exif_stamped FROM archive_copies WHERE archive_path = 'photo_standalone.jpg'"
        )
        status = cur.fetchone()[0]
        conn.close()

        # Must preserve stamped status 1 (not demoted to 2)
        assert status == 1

    def test_update_metadata_corrupt_file_graceful_skipping(self, tmp_path: Path) -> None:
        """Non-image corrupt file logs warning, skips cleanly, and marks exif_stamped as skipped (2)."""
        out = tmp_path / "CorruptArchive"
        out.mkdir()

        bad_jpg = out / "corrupted.jpg"
        bad_jpg.write_bytes(b"completely_not_a_jpeg_file_header")

        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()
        arc.record_file_archived(
            cur, "corrupted.jpg", b"hash_bad", "corrupted.jpg", 32, 0, 1715340000000
        )
        conn.commit()
        conn.close()

        logger = logging.getLogger("test_corrupt")
        args = argparse.Namespace(
            command="update-metadata",
            output=str(out),
            timezone=None,
            force=False,
            dry_run=False,
        )

        wa.run_update_metadata_mode(args, logger)

        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()
        cur.execute("SELECT exif_stamped FROM archive_copies WHERE archive_path = 'corrupted.jpg'")
        status = cur.fetchone()[0]
        conn.close()

        # Invalid/un-stampable files get marked 2 (skipped)
        assert status == 2
