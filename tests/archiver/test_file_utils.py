"""Tests for wab_archiver."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


import wab_archiver.archive_db as arc
import wab_archiver.main as wa


class TestSanitizeFilename:
    def test_safe_name_unchanged(self):
        assert wa.sanitize_filename("Family Chat") == "Family Chat"

    def test_forbidden_chars_replaced(self):
        # All of \  /  :  *  ?  "  <  >  | are replaced with _
        assert wa.sanitize_filename("bad:name?") == "bad_name_"
        assert wa.sanitize_filename("a/b\\c") == "a_b_c"
        assert wa.sanitize_filename("<pipe>|test") == "_pipe__test"

    def test_strips_leading_trailing_spaces(self):
        assert wa.sanitize_filename("  padded  ") == "padded"

    def test_empty_string(self):
        assert wa.sanitize_filename("") == ""


class TestEscapeLike:
    def test_plain_string_unchanged(self):
        assert wa.escape_like("hello") == "hello"

    def test_percent_escaped(self):
        assert wa.escape_like("50% off") == r"50\% off"

    def test_underscore_escaped(self):
        assert wa.escape_like("file_name") == r"file\_name"

    def test_backslash_escaped_first(self):
        # The backslash must be escaped before % and _ so it is not double-escaped
        assert wa.escape_like("a\\b") == "a\\\\b"

    def test_combined(self):
        assert wa.escape_like("100%_done") == r"100\%\_done"


class TestFormatPhone:
    def test_prepends_double_zero(self):
        assert wa.format_phone("391234567890") == "00391234567890"

    def test_empty_string_returns_empty(self):
        assert wa.format_phone("") == ""


class TestBuildContactFolderName:
    def test_known_contact(self):
        result = wa.build_contact_folder_name("Alice", "391234567890")
        assert result == "Alice (00391234567890)"

    def test_unknown_contact_uses_unknown_prefix(self):
        # display_name is None
        result = wa.build_contact_folder_name(None, "391234567890")
        assert result == "Unknown (00391234567890)"

    def test_display_name_equals_number_treated_as_unknown(self):
        # When WhatsApp has no name, it stores the number as the display name
        result = wa.build_contact_folder_name("391234567890", "391234567890")
        assert result == "Unknown (00391234567890)"

    def test_special_chars_in_name_are_sanitized(self):
        result = wa.build_contact_folder_name("Alice/Bob", "391234567890")
        assert "/" not in result


class TestAppendSenderToFilename:
    def test_normal_case(self):
        result = wa.append_sender_to_filename("IMG-20260512-WA0003.jpg", "JohnDoe")
        assert result == "IMG-20260512-WA0003_JohnDoe.jpg"

    def test_no_extension(self):
        result = wa.append_sender_to_filename("noext", "Me")
        assert result == "noext_Me"

    def test_sender_with_special_chars_is_sanitized(self):
        result = wa.append_sender_to_filename("file.jpg", "Alice/Bob")
        assert "/" not in result


class TestUniqueGroupName:
    def test_unused_name_returned_as_is(self):
        assert arc._unique_group_name("Family", set()) == "Family"
        assert arc._unique_group_name("Family", {"Other"}) == "Family"

    def test_collision_gets_counter_suffix(self):
        assert arc._unique_group_name("Family", {"Family"}) == "Family (2)"

    def test_multiple_collisions(self):
        existing = {"Family", "Family (2)", "Family (3)"}
        assert arc._unique_group_name("Family", existing) == "Family (4)"


class TestResolveUniqueDest:
    def test_new_path_returned_as_is(self, tmp_path, logger):
        dest = str(tmp_path / "image.jpg")
        src = str(tmp_path / "src.jpg")
        tmp_path.joinpath("src.jpg").write_bytes(b"data")
        assert wa.resolve_unique_dest(dest, src, logger) == dest

    def test_identical_file_returns_none(self, tmp_path, logger):
        data = b"same content"
        src = tmp_path / "src.jpg"
        dest = tmp_path / "image.jpg"
        src.write_bytes(data)
        dest.write_bytes(data)
        result = wa.resolve_unique_dest(str(dest), str(src), logger)
        assert result is None

    def test_collision_gets_numbered_suffix(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        dest = tmp_path / "image.jpg"
        src.write_bytes(b"new content")
        dest.write_bytes(b"old content")
        result = wa.resolve_unique_dest(str(dest), str(src), logger)
        assert result == str(tmp_path / "image_1.jpg")

    def test_multiple_collisions_increment_counter(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        dest = tmp_path / "image.jpg"
        src.write_bytes(b"new content")
        dest.write_bytes(b"old content")
        (tmp_path / "image_1.jpg").write_bytes(b"another collision")
        result = wa.resolve_unique_dest(str(dest), str(src), logger)
        assert result == str(tmp_path / "image_2.jpg")


class TestFindIdenticalVariant:
    def test_matches_base_name(self, tmp_path):
        dest = tmp_path / "doc.pdf"
        dest.write_bytes(b"same")
        assert wa.find_identical_variant(str(dest), hashlib.md5(b"same").digest()) == str(dest)

    def test_matches_suffixed_variant(self, tmp_path):
        dest = tmp_path / "doc.pdf"
        dest.write_bytes(b"first")
        (tmp_path / "doc_1.pdf").write_bytes(b"second")
        (tmp_path / "doc_2.pdf").write_bytes(b"third")
        result = wa.find_identical_variant(str(dest), hashlib.md5(b"third").digest())
        assert result == str(tmp_path / "doc_2.pdf")

    def test_no_match_returns_none(self, tmp_path):
        dest = tmp_path / "doc.pdf"
        dest.write_bytes(b"first")
        (tmp_path / "doc_1.pdf").write_bytes(b"second")
        assert wa.find_identical_variant(str(dest), hashlib.md5(b"other").digest()) is None


class TestCollisionRerun:
    """Two different files with the same archive name must not gain a new suffix on every run."""

    @staticmethod
    def _run(sources, dest_path, out, logger, cursor):
        return [
            wa._copy_or_skip(
                str(src),
                str(dest_path),
                str(dest_path.parent),
                orig,
                None,
                str(out),
                False,
                logger,
                cursor,
                False,
                {},
            )
            for orig, src in sources
        ]

    @pytest.mark.parametrize("keep_archive_db", [True, False])
    def test_rerun_skips_suffixed_copy(self, tmp_path, logger, keep_archive_db):
        out = tmp_path / "out"
        out.mkdir()
        src_a = tmp_path / "a.docx"
        src_b = tmp_path / "b.docx"
        src_a.write_bytes(b"first document")
        src_b.write_bytes(b"second document, same name")
        sources = [
            ("Media/WhatsApp Documents/a.docx", src_a),
            ("Media/WhatsApp Documents/Sent/a.docx", src_b),
        ]
        dest_path = out / "Contacts" / "Alice" / "2026" / "Notes.docx"

        conn = arc.open_archive_db(str(out))
        assert self._run(sources, dest_path, out, logger, conn.cursor()) == [(1, 0, 0), (1, 0, 0)]
        conn.commit()
        conn.close()
        if not keep_archive_db:
            os.remove(out / ".wa_media_archiver.db")

        for _ in range(2):
            conn = arc.open_archive_db(str(out))
            cursor = conn.cursor()
            assert self._run(sources, dest_path, out, logger, cursor) == [(0, 1, 0), (0, 1, 0)]
            conn.commit()
            archive_path = cursor.execute(
                "SELECT archive_path FROM archive_copies WHERE original_path = ?",
                (sources[1][0],),
            ).fetchone()[0]
            conn.close()
            assert archive_path == "Contacts/Alice/2026/Notes_1.docx"

        assert sorted(p.name for p in dest_path.parent.iterdir()) == ["Notes.docx", "Notes_1.docx"]


class TestProbeHardlinkSupport:
    def test_probe_success_on_supported_fs(self, tmp_path, logger):
        assert wa.probe_hardlink_support(str(tmp_path), logger) is True

    def test_probe_windows_exfat_error_returns_false(self, tmp_path, logger):
        with patch("os.link", side_effect=OSError(1, "Incorrect function")):
            assert wa.probe_hardlink_support(str(tmp_path), logger) is False

    def test_probe_windows_smb_not_supported_returns_false(self, tmp_path, logger):
        with patch("os.link", side_effect=OSError(50, "The network request is not supported")):
            assert wa.probe_hardlink_support(str(tmp_path), logger) is False

    def test_probe_posix_exdev_returns_false(self, tmp_path, logger):
        import errno

        with patch("os.link", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            assert wa.probe_hardlink_support(str(tmp_path), logger) is False

    def test_probe_posix_eopnotsupp_returns_false(self, tmp_path, logger):
        import errno

        with patch("os.link", side_effect=OSError(errno.EOPNOTSUPP, "Operation not supported")):
            assert wa.probe_hardlink_support(str(tmp_path), logger) is False

    def test_probe_permission_error_returns_false(self, tmp_path, logger):
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            assert wa.probe_hardlink_support(str(tmp_path), logger) is False

    def test_probe_cleanup_os_error_suppressed(self, tmp_path, logger):
        with patch("os.remove", side_effect=OSError("Remove failed")):
            assert wa.probe_hardlink_support(str(tmp_path), logger) is True
