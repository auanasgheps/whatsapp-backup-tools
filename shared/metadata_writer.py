"""
Pure-Python metadata writer for archived WhatsApp media.

Writes WhatsApp message timestamps to:
- JPEG EXIF APP1 metadata (DateTimeOriginal, DateTimeDigitized, DateTime, OffsetTimeOriginal).
- QuickTime / ISO-BMFF MP4 video headers (mvhd, tkhd, mdhd creation_time).

Operates with zero external dependencies, no image re-compression, and safe
atomic file operations.
"""

import logging
import os
import struct
from datetime import datetime, tzinfo

from shared.db import ensure_long_path

logger = logging.getLogger(__name__)

# QuickTime epoch: seconds between 1904-01-01 00:00:00 UTC and 1970-01-01 00:00:00 UTC
EPOCH_1904 = 2_082_844_800

# Supported image extensions
JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg"})
# Supported video & audio container extensions (ISO-BMFF / QuickTime)
MP4_EXTENSIONS = frozenset({".mp4", ".mov", ".3gp", ".m4a"})
# Combined supported extensions for metadata stamping
SUPPORTED_EXTENSIONS = JPEG_EXTENSIONS | MP4_EXTENSIONS


def format_exif_datetime(timestamp_ms: int, tz: tzinfo | None) -> tuple[str, str | None]:
    """
    Format a millisecond timestamp into EXIF date/time string and timezone offset.

    Returns:
        tuple of (dt_str, tz_offset_str):
        - dt_str: "YYYY:MM:DD HH:MM:SS" (in local or target timezone)
        - tz_offset_str: "+HH:MM" or "-HH:MM" if timezone is provided, else None.
    """
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=tz)
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S")

    tz_offset_str = None
    offset = dt.utcoffset()
    if offset is not None:
        total_seconds = int(offset.total_seconds())
        sign = "+" if total_seconds >= 0 else "-"
        abs_seconds = abs(total_seconds)
        hours = abs_seconds // 3600
        minutes = (abs_seconds % 3600) // 60
        tz_offset_str = f"{sign}{hours:02d}:{minutes:02d}"

    return dt_str, tz_offset_str


def build_jpeg_exif_app1(dt_str: str, tz_offset_str: str | None) -> bytes:
    """
    Construct a complete JPEG APP1 EXIF segment containing IFD0 and Exif Sub-IFD.

    Tags written:
    - IFD0:
        0x0132 (DateTime)
        0x8769 (ExifOffset)
    - Exif Sub-IFD:
        0x9003 (DateTimeOriginal)
        0x9004 (DateTimeDigitized)
        0x9010 (OffsetTime - if tz_offset_str is provided, Exif 2.31)
        0x9011 (OffsetTimeOriginal - if tz_offset_str is provided)
        0x9012 (OffsetTimeDigitized - if tz_offset_str is provided)
    """
    endian = "<"  # Little-endian (II)
    dt_bytes = dt_str.encode("ascii") + b"\x00"  # 20 bytes

    has_tz = tz_offset_str is not None
    # 6 bytes for "+HH:MM" + 1 null terminator = 7 bytes; pad to 8 bytes for 2-byte word boundary
    tz_bytes = (tz_offset_str.encode("ascii") + b"\x00") if has_tz else b""
    if has_tz and len(tz_bytes) % 2 != 0:
        tz_bytes += b"\x00"

    # TIFF Header (8 bytes): "II" + magic 42 (0x002A) + IFD0 offset (8)
    header = b"II\x2a\x00\x08\x00\x00\x00"

    num_ifd0 = 2
    ifd0_size = 2 + num_ifd0 * 12 + 4
    exif_ifd_offset = 8 + ifd0_size

    num_exif = 5 if has_tz else 2
    exif_ifd_size = 2 + num_exif * 12 + 4
    data_offset = exif_ifd_offset + exif_ifd_size

    dt_offset = data_offset
    tz_offset = data_offset + len(dt_bytes)

    # Build IFD0 (strictly ascending numerical tag order: 0x0132 < 0x8769)
    ifd0 = struct.pack(endian + "H", num_ifd0)
    # Tag 0x0132: DateTime, ASCII (2), count 20
    ifd0 += struct.pack(endian + "HHI I", 0x0132, 2, 20, dt_offset)
    # Tag 0x8769: ExifOffset, LONG (4), count 1, value = exif_ifd_offset
    ifd0 += struct.pack(endian + "HHI I", 0x8769, 4, 1, exif_ifd_offset)
    # Next IFD offset: 0
    ifd0 += struct.pack(endian + "I", 0)

    # Build Exif Sub-IFD (strictly ascending tag order: 0x9003 < 0x9004 < 0x9010 < 0x9011 < 0x9012)
    exif = struct.pack(endian + "H", num_exif)
    # Tag 0x9003: DateTimeOriginal, ASCII (2), count 20
    exif += struct.pack(endian + "HHI I", 0x9003, 2, 20, dt_offset)
    # Tag 0x9004: DateTimeDigitized, ASCII (2), count 20
    exif += struct.pack(endian + "HHI I", 0x9004, 2, 20, dt_offset)
    if has_tz and tz_offset_str is not None:
        # Tag 0x9010: OffsetTime (Exif 2.31 Table 7 Exif-specific IFD)
        exif += struct.pack(endian + "HHI I", 0x9010, 2, len(tz_offset_str) + 1, tz_offset)
        # Tag 0x9011: OffsetTimeOriginal
        exif += struct.pack(endian + "HHI I", 0x9011, 2, len(tz_offset_str) + 1, tz_offset)
        # Tag 0x9012: OffsetTimeDigitized
        exif += struct.pack(endian + "HHI I", 0x9012, 2, len(tz_offset_str) + 1, tz_offset)
    # Next IFD offset: 0
    exif += struct.pack(endian + "I", 0)

    tiff_payload = header + ifd0 + exif + dt_bytes + tz_bytes
    app1_payload = b"Exif\x00\x00" + tiff_payload

    length = len(app1_payload) + 2
    if length > 65535:
        raise ValueError(f"EXIF APP1 payload exceeds 65535 bytes: {length}")

    return b"\xff\xe1" + struct.pack(">H", length) + app1_payload


def has_valid_camera_datetime(tiff_bytes: bytes) -> bool:
    """
    Check whether TIFF data contains a valid, non-dummy DateTimeOriginal (tag 0x9003).

    Returns True if valid camera capture timestamp is present (e.g. year >= 1970),
    False if missing or dummy/zero value.
    """
    if len(tiff_bytes) < 8:
        return False

    byte_order = tiff_bytes[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return False

    magic, ifd0_offset = struct.unpack(endian + "HI", tiff_bytes[2:8])
    if magic != 42 or ifd0_offset < 8 or ifd0_offset >= len(tiff_bytes):
        return False

    def read_ifd_tags(offset: int) -> dict[int, tuple[int, int, int]]:
        if offset + 2 > len(tiff_bytes):
            return {}
        entries_count = struct.unpack(endian + "H", tiff_bytes[offset : offset + 2])[0]
        tags = {}
        curr = offset + 2
        for _ in range(entries_count):
            if curr + 12 > len(tiff_bytes):
                break
            tag, typ, count, val_or_off = struct.unpack(
                endian + "HHI I", tiff_bytes[curr : curr + 12]
            )
            tags[tag] = (typ, count, val_or_off)
            curr += 12
        return tags

    ifd0_tags = read_ifd_tags(ifd0_offset)
    if 0x8769 not in ifd0_tags:
        return False

    exif_offset = ifd0_tags[0x8769][2]
    exif_tags = read_ifd_tags(exif_offset)

    if 0x9003 not in exif_tags:
        return False

    typ, count, val_offset = exif_tags[0x9003]
    if typ != 2 or count < 10 or val_offset + count > len(tiff_bytes):
        return False

    dt_str = (
        tiff_bytes[val_offset : val_offset + count]
        .decode("ascii", errors="replace")
        .rstrip("\x00")
        .strip()
    )

    # Check for valid year >= 1970 and not dummy zeros
    if len(dt_str) >= 4 and dt_str[:4].isdigit():
        year = int(dt_str[:4])
        return year >= 1970

    return False


def has_orientation_tag(tiff_bytes: bytes) -> bool:
    """Check whether TIFF data contains an Orientation tag (0x0112)."""
    if len(tiff_bytes) < 8:
        return False

    byte_order = tiff_bytes[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return False

    magic, ifd0_offset = struct.unpack(endian + "HI", tiff_bytes[2:8])
    if magic != 42 or ifd0_offset < 8 or ifd0_offset >= len(tiff_bytes):
        return False

    if ifd0_offset + 2 > len(tiff_bytes):
        return False

    entries_count = struct.unpack(endian + "H", tiff_bytes[ifd0_offset : ifd0_offset + 2])[0]
    curr = ifd0_offset + 2
    for _ in range(entries_count):
        if curr + 12 > len(tiff_bytes):
            break
        tag = struct.unpack(endian + "H", tiff_bytes[curr : curr + 2])[0]
        if tag == 0x0112:
            return True
        curr += 12

    return False


def inject_jpeg_exif(data: bytes, dt_str: str, tz_offset_str: str | None) -> bytes | None:
    """
    Inject an APP1 EXIF segment into JPEG data if EXIF is missing.

    Preserves pre-existing camera DateTimeOriginal and Orientation tags.
    Maintains JFIF APP0 marker ordering if APP0 is present.

    Returns:
        bytes of modified JPEG, or None if skipped (already has valid EXIF or not a JPEG).
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None  # Not a valid JPEG

    # Scan top-level markers
    pos = 2
    insert_pos = 2
    has_app0 = False
    data_len = len(data)

    while pos < data_len:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]

        # Standalone markers with no length
        if marker in (0xD8, 0xD9):  # SOI, EOI
            pos += 2
            continue

        if pos + 4 > data_len:
            break

        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        seg_end = pos + 2 + seg_len

        if marker == 0xE0:  # APP0 (JFIF)
            has_app0 = True
            insert_pos = seg_end
        elif marker == 0xE1:  # APP1
            payload = data[pos + 4 : seg_end]
            if payload.startswith(b"Exif\x00\x00"):
                # If image already contains an EXIF segment (original camera capture date,
                # orientation tag, or previously stamped), preserve existing TIFF safely.
                return None
        elif marker == 0xDA:  # SOS (Start of Scan - image entropy begins)
            break

        pos = seg_end

    if not has_app0:
        insert_pos = 2  # Immediately after SOI

    app1_seg = build_jpeg_exif_app1(dt_str, tz_offset_str)
    return data[:insert_pos] + app1_seg + data[insert_pos:]


def patch_mp4_timestamps(dest_path: str, timestamp_ms: int) -> bool:
    """
    Patch creation_time and modification_time in MP4/MOV container headers (mvhd, tkhd, mdhd).

    Operates in-place with streaming seek navigation (zero extra RAM, zero extra disk writes).
    Uses UTC timestamp calculated from the 1904 QuickTime epoch.

    Returns:
        True if successfully patched, False otherwise.
    """
    utc_sec = int(timestamp_ms / 1000.0)
    qt_time = utc_sec + EPOCH_1904
    if qt_time < 0 or qt_time > 0xFFFFFFFFFFFFFFFF:
        return False

    qt_time_32 = qt_time & 0xFFFFFFFF
    lp_dest = ensure_long_path(dest_path)

    try:
        with open(lp_dest, "r+b") as f:
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(0)

            pos = 0
            patched = False

            while pos < file_size:
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break

                box_size, box_type = struct.unpack(">II", header)
                box_type_bytes = struct.pack(">I", box_type)

                if box_size == 1:
                    ext_size_bytes = f.read(8)
                    if len(ext_size_bytes) < 8:
                        break
                    box_size = struct.unpack(">Q", ext_size_bytes)[0]
                    header_len = 16
                elif box_size == 0:
                    box_size = file_size - pos
                    header_len = 8
                else:
                    header_len = 8

                if box_size < header_len:
                    break

                if box_type_bytes == b"moov":
                    moov_end = pos + box_size
                    curr = pos + header_len

                    while curr < moov_end:
                        f.seek(curr)
                        child_header = f.read(8)
                        if len(child_header) < 8:
                            break

                        c_size, c_type = struct.unpack(">II", child_header)
                        c_type_bytes = struct.pack(">I", c_type)
                        if c_size < 8:
                            break

                        def _patch_box_at(box_offset: int):
                            f.seek(box_offset + 8)
                            vf = f.read(4)
                            if len(vf) < 4:
                                return
                            version = vf[0]
                            f.seek(box_offset + 12)
                            if version == 0:
                                f.write(struct.pack(">II", qt_time_32, qt_time_32))
                            elif version == 1:
                                f.write(struct.pack(">QQ", qt_time, qt_time))

                        if c_type_bytes == b"mvhd":
                            _patch_box_at(curr)
                            patched = True
                        elif c_type_bytes == b"trak":
                            trak_end = curr + c_size
                            t_curr = curr + 8
                            while t_curr < trak_end:
                                f.seek(t_curr)
                                t_hdr = f.read(8)
                                if len(t_hdr) < 8:
                                    break
                                t_size, t_type = struct.unpack(">II", t_hdr)
                                t_type_bytes = struct.pack(">I", t_type)
                                if t_size < 8:
                                    break

                                if t_type_bytes == b"tkhd":
                                    _patch_box_at(t_curr)
                                elif t_type_bytes == b"mdia":
                                    mdia_end = t_curr + t_size
                                    m_curr = t_curr + 8
                                    while m_curr < mdia_end:
                                        f.seek(m_curr)
                                        m_hdr = f.read(8)
                                        if len(m_hdr) < 8:
                                            break
                                        m_size, m_type = struct.unpack(">II", m_hdr)
                                        m_type_bytes = struct.pack(">I", m_type)
                                        if m_size < 8:
                                            break
                                        if m_type_bytes == b"mdhd":
                                            _patch_box_at(m_curr)
                                        m_curr += m_size
                                t_curr += t_size

                        curr += c_size

                pos += box_size

            return patched
    except (OSError, struct.error) as e:
        logger.debug(f"Could not patch MP4 container timestamps on {dest_path}: {e}")
        return False


def write_media_timestamp(
    dest_path: str,
    timestamp_ms: int,
    tz: tzinfo | None,
    is_hardlink: bool,
) -> bool:
    """
    Write WhatsApp message timestamps to media metadata headers.

    - Hardlink Invariant: skips injection if is_hardlink is True to protect shared inodes.
    - JPEG: injects APP1 EXIF segment (DateTimeOriginal in local time).
    - MP4: patches container creation_time (in UTC).
    - Other formats: gracefully ignored (returns False).

    Returns:
        True if metadata was written or patched, False if skipped or unsupported.
    """
    if is_hardlink:
        return False

    if timestamp_ms is None or timestamp_ms <= 0:
        return False

    ext = os.path.splitext(dest_path)[1].lower()

    if ext in JPEG_EXTENSIONS:
        lp_dest = ensure_long_path(dest_path)
        try:
            with open(lp_dest, "rb") as f:
                raw_bytes = f.read()

            dt_str, tz_offset_str = format_exif_datetime(timestamp_ms, tz)
            modified = inject_jpeg_exif(raw_bytes, dt_str, tz_offset_str)
            if modified is None:
                return False

            tmp_path = dest_path + ".tmp"
            lp_tmp = ensure_long_path(tmp_path)
            try:
                with open(lp_tmp, "wb") as f:
                    f.write(modified)
                os.replace(lp_tmp, lp_dest)
                return True
            finally:
                if os.path.exists(lp_tmp):
                    try:
                        os.remove(lp_tmp)
                    except OSError:
                        pass
        except PermissionError as e:
            logger.debug(f"Permission denied modifying EXIF on {dest_path}: {e}")
            return False
        except (OSError, ValueError) as e:
            logger.debug(f"Could not inject EXIF timestamp on {dest_path}: {e}")
            return False

    if ext in MP4_EXTENSIONS:
        return patch_mp4_timestamps(dest_path, timestamp_ms)

    return False
