"""Tests for wab_archiver."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import wab_archiver.adb_extractor as adb
import wab_archiver.android_handler as android_handler
import wab_archiver.archive_db as arc
import wab_archiver.main as wa
from tests.archiver.conftest import (
    _make_msgstore,
)


class TestCheckAdb:
    def test_found(self, logger):
        with patch("wab_archiver.adb_extractor.shutil.which", return_value="/usr/bin/adb"):
            assert adb.check_adb(logger) is True

    def test_not_found(self, logger):
        with patch("wab_archiver.adb_extractor.shutil.which", return_value=None):
            assert adb.check_adb(logger) is False


class TestParseAdbDevices:
    def test_empty_output(self):
        assert adb._parse_adb_devices("") == []
        assert adb._parse_adb_devices("List of devices attached\n\n") == []

    def test_daemon_noise_skipped(self):
        output = (
            "* daemon not running; starting now at tcp:5037 *\n"
            "* daemon started successfully *\n"
            "List of devices attached\n"
            "emulator-5554\tdevice\n"
        )
        assert adb._parse_adb_devices(output) == [("emulator-5554", "device")]

    def test_multiple_devices_and_states(self):
        output = (
            "List of devices attached\n"
            "dev1\tdevice\n"
            "dev2\tunauthorized\n"
            "dev3\toffline\n"
            "dev4\tno permissions (missing udev rules?); see [url]\n"
            "dev5\trecovery\n"
        )
        parsed = adb._parse_adb_devices(output)
        assert parsed == [
            ("dev1", "device"),
            ("dev2", "unauthorized"),
            ("dev3", "offline"),
            ("dev4", "no permissions"),
            ("dev5", "recovery"),
        ]


class TestCheckDeviceConnected:
    def _make_result(self, stdout):
        r = MagicMock()
        r.stdout = stdout.encode()
        return r

    def test_device_connected(self, logger):
        output = "List of devices attached\nemulator-5554\tdevice\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch.object(logger, "info") as mock_info,
        ):
            assert adb.check_device_connected(logger) is True
            assert any("emulator-5554" in str(c) for c in mock_info.call_args_list)

    def test_no_devices(self, logger):
        output = "List of devices attached\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            err_msg = mock_err.call_args[0][0]
            assert "No Android device detected" in err_msg
            assert "USB debugging is enabled" in err_msg

    @pytest.mark.parametrize(
        "is_tty, user_input",
        [
            (False, None),
            (True, "n"),
        ],
    )
    def test_unauthorized_device_exit(self, logger, is_tty, user_input):
        output = "List of devices attached\n98abc123\tunauthorized\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch("sys.stdin.isatty", return_value=is_tty),
            patch("builtins.input", return_value=user_input),
            patch.object(logger, "error") as mock_err,
            patch.object(logger, "warning") as mock_warn,
        ):
            assert adb.check_device_connected(logger) is False
            assert "98abc123" in mock_err.call_args[0][0]
            assert "Unlock your phone" in mock_err.call_args[0][0]
            if not is_tty:
                assert "unauthorized" in mock_warn.call_args[0][0]

    def test_unauthorized_user_confirms_restart_success(self, logger):
        unauth_out = self._make_result("List of devices attached\n98abc123\tunauthorized\n")
        auth_out = self._make_result("List of devices attached\n98abc123\tdevice\n")
        kill_out = self._make_result("")

        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run",
                side_effect=[unauth_out, kill_out, unauth_out, auth_out],
            ) as mock_run,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["y", ""]),
            patch("time.sleep") as mock_sleep,
            patch.object(logger, "info") as mock_info,
        ):
            assert adb.check_device_connected(logger) is True
            mock_sleep.assert_called_once_with(2)
            assert mock_run.call_args_list[1][0][0] == ["adb", "kill-server"]
            assert any(
                "connected and authorized: 98abc123" in str(c) for c in mock_info.call_args_list
            )

    def test_unauthorized_user_confirms_restart_still_unauthorized(self, logger):
        unauth_out = self._make_result("List of devices attached\n98abc123\tunauthorized\n")
        kill_out = self._make_result("")

        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run",
                side_effect=[unauth_out, kill_out, unauth_out, unauth_out],
            ),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["yes", ""]),
            patch("time.sleep"),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            err_msg = mock_err.call_args[0][0]
            assert "still unauthorized" in err_msg
            assert "Revoke USB debugging authorizations" in err_msg

    def test_unauthorized_user_cancels_continue_prompt(self, logger):
        unauth_out = self._make_result("List of devices attached\n98abc123\tunauthorized\n")
        kill_out = self._make_result("")

        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run",
                side_effect=[unauth_out, kill_out, unauth_out],
            ),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["y", KeyboardInterrupt()]),
            patch("time.sleep"),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            assert "cancelled" in mock_err.call_args[0][0]

    def test_offline_device(self, logger):
        output = "List of devices attached\n98abc123\toffline\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            assert "offline" in mock_err.call_args[0][0]

    def test_no_permissions_device(self, logger):
        output = "List of devices attached\n98abc123\tno permissions (missing udev rules?)\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            assert "host permissions" in mock_err.call_args[0][0]

    def test_recovery_device(self, logger):
        output = "List of devices attached\n98abc123\trecovery\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            assert "recovery" in mock_err.call_args[0][0]

    def test_multiple_devices(self, logger):
        output = "List of devices attached\ndev1\tdevice\ndev2\tdevice\n"
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", return_value=self._make_result(output)
            ),
            patch.object(logger, "error") as mock_err,
        ):
            assert adb.check_device_connected(logger) is False
            assert "Multiple Android devices detected" in mock_err.call_args[0][0]

    def test_adb_failure(self, logger):
        with patch(
            "wab_archiver.adb_extractor.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "adb", stderr=b"error"),
        ):
            assert adb.check_device_connected(logger) is False


class TestPullMsgstore:
    def test_regular_path(self, logger, tmp_path):
        with patch("wab_archiver.adb_extractor.subprocess.run") as mock_run:
            result = adb.pull_msgstore(str(tmp_path), business=False, logger=logger)
            call_args = mock_run.call_args[0][0]
            assert "com.whatsapp/WhatsApp" in call_args[2]
            assert "com.whatsapp.w4b" not in call_args[2]
        assert result == str(tmp_path / "msgstore.db.crypt15") or result == os.path.join(
            str(tmp_path), "msgstore.db.crypt15"
        )

    def test_business_path(self, logger, tmp_path):
        with patch("wab_archiver.adb_extractor.subprocess.run") as mock_run:
            adb.pull_msgstore(str(tmp_path), business=True, logger=logger)
            call_args = mock_run.call_args[0][0]
            assert "com.whatsapp.w4b" in call_args[2]

    def test_failure_raises(self, logger, tmp_path):
        with patch(
            "wab_archiver.adb_extractor.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "adb", stderr=b"fail"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                adb.pull_msgstore(str(tmp_path), logger=logger)

    def test_failure_file_not_found_message(self, logger, tmp_path):
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    1, "adb", stderr=b"remote object does not exist"
                ),
            ),
            patch.object(logger, "error") as mock_err,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                adb.pull_msgstore(str(tmp_path), logger=logger)
            err_msg = mock_err.call_args[0][0]
            assert "WhatsApp backup file not found on device" in err_msg
            assert "End-to-end encrypted backup" in err_msg

    def test_failure_permission_denied_message(self, logger, tmp_path):
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "adb", stderr=b"Permission denied"),
            ),
            patch.object(logger, "error") as mock_err,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                adb.pull_msgstore(str(tmp_path), logger=logger)
            err_msg = mock_err.call_args[0][0]
            assert "Permission denied accessing" in err_msg


class TestPullContacts:
    def test_filters_whatsapp_lines(self, logger, tmp_path):
        raw = (
            "Row: 0 display_name=Alice, data1=391234567890@s.whatsapp.net\n"
            "Row: 1 display_name=Bob, data1=bob@gmail.com\n"
            "Row: 2 display_name=Carol, data1=390987654321@s.whatsapp.net\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = raw.encode()
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock_result):
            dest = adb.pull_contacts(str(tmp_path), logger=logger)

        content = open(dest, encoding="utf-8").read()
        assert "@s.whatsapp.net" in content
        assert "bob@gmail.com" not in content
        assert content.count("@s.whatsapp.net") == 2

    def test_retains_phone_and_whatsapp_lines(self, logger, tmp_path):
        raw = (
            "Row: 0 display_name=Alice, data1=391234567890@s.whatsapp.net, "
            "data4=NULL, mimetype=vnd.android.cursor.item/vnd.com.whatsapp.profile\n"
            "Row: 1 display_name=Bob, data1=bob@gmail.com, "
            "data4=NULL, mimetype=vnd.android.cursor.item/email_v2\n"
            "Row: 2 display_name=Carol, data1=+15551234567, "
            "data4=+15551234567, mimetype=vnd.android.cursor.item/phone_v2\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = raw.encode()
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock_result):
            dest = adb.pull_contacts(str(tmp_path), logger=logger)

        content = open(dest, encoding="utf-8").read()
        assert "Alice" in content
        assert "Carol" in content
        assert "bob@gmail.com" not in content

    def test_failure_raises(self, logger, tmp_path):
        with patch(
            "wab_archiver.adb_extractor.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "adb", stderr=b"fail"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                adb.pull_contacts(str(tmp_path), logger=logger)


class TestGetDeviceSerial:
    def test_returns_serial(self, logger):
        mock = MagicMock(returncode=0, stdout=b"emulator-5554\n", stderr=b"")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            assert adb.get_device_serial(logger) == "emulator-5554"

    def test_nonzero_returncode_raises(self, logger):
        mock = MagicMock(returncode=1, stdout=b"", stderr=b"no device")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            with pytest.raises(RuntimeError):
                adb.get_device_serial(logger)

    def test_empty_output_raises(self, logger):
        mock = MagicMock(returncode=0, stdout=b"\n", stderr=b"")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            with pytest.raises(RuntimeError):
                adb.get_device_serial(logger)

    def test_unknown_output_raises(self, logger):
        mock = MagicMock(returncode=0, stdout=b"unknown\n", stderr=b"")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            with pytest.raises(RuntimeError):
                adb.get_device_serial(logger)


class TestProbeMd5Binary:
    def test_md5sum_found(self, logger):
        mock = MagicMock(returncode=0, stdout=b"/usr/bin/md5sum\n", stderr=b"")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            assert adb.probe_md5_binary(logger) == "md5sum"

    def test_md5_found(self, logger):
        mock = MagicMock(returncode=0, stdout=b"/usr/bin/md5\n", stderr=b"")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            assert adb.probe_md5_binary(logger) == "md5"

    def test_neither_found_raises(self, logger):
        mock = MagicMock(returncode=0, stdout=b"\n", stderr=b"")
        with patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock):
            with pytest.raises(RuntimeError):
                adb.probe_md5_binary(logger)


class TestRunAdb:
    def test_success_returns_result(self, logger):
        mock = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with (
            patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock),
            patch("wab_archiver.adb_extractor.time.sleep"),
        ):
            result = adb._run_adb(["adb", "pull", "/remote", "/local"], logger)
        assert result is mock

    def test_transient_error_retries_and_succeeds(self, logger):
        first = MagicMock(returncode=1, stdout=b"", stderr=b"error: closed")
        second = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run", side_effect=[first, second]
            ) as mock_run,
            patch("wab_archiver.adb_extractor.time.sleep"),
        ):
            result = adb._run_adb(["adb", "test"], logger)
        assert mock_run.call_count == 2
        assert result is second

    def test_non_transient_error_raises_immediately(self, logger):
        mock = MagicMock(returncode=1, stdout=b"", stderr=b"fatal: something")
        with (
            patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock) as mock_run,
            patch("wab_archiver.adb_extractor.time.sleep"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                adb._run_adb(["adb", "test"], logger)
        assert mock_run.call_count == 1

    def test_timeout_retries_then_raises(self, logger):
        with (
            patch(
                "wab_archiver.adb_extractor.subprocess.run",
                side_effect=subprocess.TimeoutExpired("adb", 120),
            ) as mock_run,
            patch("wab_archiver.adb_extractor.time.sleep"),
        ):
            with pytest.raises(RuntimeError):
                adb._run_adb(["adb", "test"], logger)
        assert mock_run.call_count == adb._ADB_RETRIES + 1

    def test_transient_exhausted_raises(self, logger):
        mock = MagicMock(returncode=1, stdout=b"", stderr=b"error: closed")
        with (
            patch("wab_archiver.adb_extractor.subprocess.run", return_value=mock) as mock_run,
            patch("wab_archiver.adb_extractor.time.sleep"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                adb._run_adb(["adb", "test"], logger)
        assert mock_run.call_count == adb._ADB_RETRIES + 1


class TestClassifyAdbError:
    def test_path_not_found(self):
        assert adb._classify_adb_error("'/path/foo' does not exist") == "Remote path not found"

    def test_permission_denied(self):
        result = adb._classify_adb_error("Permission denied")
        assert "Permission denied" in result

    def test_open_failed(self):
        result = adb._classify_adb_error("open failed: /path: Operation not permitted")
        assert "Permission denied" in result

    def test_device_offline(self):
        result = adb._classify_adb_error("error: device offline")
        assert "offline" in result or "Disconnected" in result

    def test_disk_full(self):
        assert adb._classify_adb_error("No space left on device") == "Host disk is full"

    def test_unknown_falls_back_to_stripped_stderr(self):
        assert adb._classify_adb_error("  some random error  ") == "some random error"


class TestEnumerateRemoteFiles:
    def test_parses_path_and_size(self, logger):
        stdout = b"/storage/WA/Media/img.jpg\t12345\n"
        with patch("wab_archiver.adb_extractor._run_adb", return_value=MagicMock(stdout=stdout)):
            entries = adb.enumerate_remote_files("/storage/WA/Media", logger)
        assert entries == [("/storage/WA/Media/img.jpg", 12345)]

    def test_multiple_files(self, logger):
        stdout = b"/storage/WA/Media/img.jpg\t12345\n/storage/WA/Media/vid.mp4\t67890\n"
        with patch("wab_archiver.adb_extractor._run_adb", return_value=MagicMock(stdout=stdout)):
            entries = adb.enumerate_remote_files("/storage/WA/Media", logger)
        assert len(entries) == 2
        assert entries[1] == ("/storage/WA/Media/vid.mp4", 67890)

    def test_skips_permission_denied_lines(self, logger):
        stdout = b"/storage/WA/Media/img.jpg\t12345\n/storage/WA/restricted: Permission denied\n"
        with patch("wab_archiver.adb_extractor._run_adb", return_value=MagicMock(stdout=stdout)):
            entries = adb.enumerate_remote_files("/storage/WA/Media", logger)
        assert entries == [("/storage/WA/Media/img.jpg", 12345)]

    def test_skips_unparseable_lines(self, logger):
        stdout = b"badline\n/storage/WA/Media/img.jpg\t12345\n"
        with patch("wab_archiver.adb_extractor._run_adb", return_value=MagicMock(stdout=stdout)):
            entries = adb.enumerate_remote_files("/storage/WA/Media", logger)
        assert entries == [("/storage/WA/Media/img.jpg", 12345)]


_EMPTY_MD5_HEX = "d41d8cd98f00b204e9800998ecf8427e"


class TestRemoteMd5:
    def test_md5sum_format(self, logger):
        stdout = f"{_EMPTY_MD5_HEX}  /path/file\n".encode()
        with patch("wab_archiver.adb_extractor._run_adb", return_value=MagicMock(stdout=stdout)):
            result = adb._remote_md5("/path/file", "md5sum", logger)
        assert result == bytes.fromhex(_EMPTY_MD5_HEX)

    def test_md5_format(self, logger):
        # "<name>: <hash>" format produced by older md5 binaries on non-absolute paths
        stdout = f"img.jpg: {_EMPTY_MD5_HEX}\n".encode()
        with patch("wab_archiver.adb_extractor._run_adb", return_value=MagicMock(stdout=stdout)):
            result = adb._remote_md5("img.jpg", "md5", logger)
        assert result == bytes.fromhex(_EMPTY_MD5_HEX)


class TestStagingPath:
    def test_strips_prefix_and_joins(self):
        result = adb._staging_path(
            "/storage/WA/Media/img.jpg",
            "/storage/WA/Media",
            "/tmp/stage",
        )
        assert result == os.path.join("/tmp/stage", "Media", "img.jpg")

    def test_nested_subdir(self):
        result = adb._staging_path(
            "/storage/WA/Media/WhatsApp Images/img.jpg",
            "/storage/WA/Media",
            "/tmp/stage",
        )
        assert result == os.path.join("/tmp/stage", "Media", "WhatsApp Images", "img.jpg")

    def test_staging_dir_passes_validate_wa_root(self, tmp_path, logger):
        stage_dir = str(tmp_path / "stage")
        remote_path = "/storage/WA/Media/WhatsApp Images/img.jpg"
        local = adb._staging_path(remote_path, "/storage/WA/Media", stage_dir)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        # Should validate without error since staging_dir now contains Media/
        android_handler.validate_wa_root([stage_dir], logger)


class TestWriteAdbConflictsReport:
    def test_no_conflicts_no_file(self, tmp_path, logger):
        report = str(tmp_path / "conflicts.csv")
        adb.write_adb_conflicts_report(report, [], logger)
        assert not os.path.exists(report)

    def test_conflicts_written_as_csv(self, tmp_path, logger):
        report = str(tmp_path / "conflicts.csv")
        conflicts = [
            {
                "remote_path": "/storage/WA/Media/img.jpg",
                "remote_md5": "aabbccdd",
                "archived_md5": "11223344",
            }
        ]
        adb.write_adb_conflicts_report(report, conflicts, logger)
        assert os.path.exists(report)
        content = open(report, encoding="utf-8").read()
        assert "remote_path" in content
        assert "/storage/WA/Media/img.jpg" in content
        assert "aabbccdd" in content


class TestPullMedia:
    def _make_conn(self, tmp_path):
        return arc.open_archive_db(str(tmp_path))

    def test_nothing_to_pull_returns_zeros(self, tmp_path, logger):
        conn = self._make_conn(tmp_path)
        with (
            patch("wab_archiver.adb_extractor.get_device_serial", return_value="serial-1"),
            patch("wab_archiver.adb_extractor.probe_md5_binary", return_value="md5sum"),
            patch("wab_archiver.adb_extractor.enumerate_remote_files", return_value=[]),
        ):
            pulled, skipped, conflicts = adb.pull_media(
                str(tmp_path / "stage"), False, conn, logger, None
            )
        assert pulled == 0
        assert skipped == 0
        assert conflicts == []

    def test_already_done_file_skipped(self, tmp_path, logger):
        conn = self._make_conn(tmp_path)
        remote_path = "/storage/WA/Media/img.jpg"
        arc.upsert_adb_pull_state(conn, remote_path, "serial-1", "done")
        with (
            patch("wab_archiver.adb_extractor.get_device_serial", return_value="serial-1"),
            patch("wab_archiver.adb_extractor.probe_md5_binary", return_value="md5sum"),
            patch(
                "wab_archiver.adb_extractor.enumerate_remote_files",
                return_value=[(remote_path, 1024)],
            ),
        ):
            pulled, skipped, conflicts = adb.pull_media(
                str(tmp_path / "stage"), False, conn, logger, None
            )
        assert pulled == 0
        assert skipped == 1
        assert conflicts == []

    def test_new_file_pulled_marks_done(self, tmp_path, logger):
        conn = self._make_conn(tmp_path)
        remote_path = adb._WA_MEDIA_ROOT + "/WhatsApp Images/img.jpg"
        stage_dir = str(tmp_path / "stage")
        remote_size = 100
        local = adb._staging_path(remote_path, adb._WA_MEDIA_ROOT, stage_dir)

        def _fake_run_adb(cmd, logger):
            if cmd[1] == "pull":
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local, "wb") as f:
                    f.write(b"\x00" * remote_size)
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with (
            patch("wab_archiver.adb_extractor.get_device_serial", return_value="serial-1"),
            patch("wab_archiver.adb_extractor.probe_md5_binary", return_value="md5sum"),
            patch(
                "wab_archiver.adb_extractor.enumerate_remote_files",
                return_value=[(remote_path, remote_size)],
            ),
            patch("wab_archiver.adb_extractor._run_adb", side_effect=_fake_run_adb),
        ):
            pulled, skipped, conflicts = adb.pull_media(stage_dir, False, conn, logger, None)

        assert pulled == 1
        assert skipped == 0
        assert conflicts == []
        assert remote_path in arc.get_adb_done_paths(conn, "serial-1")

    def test_target_paths_filters_unmatched_files(self, tmp_path, logger):
        conn = self._make_conn(tmp_path)
        path1 = adb._WA_MEDIA_ROOT + "/WhatsApp Images/keep.jpg"
        path2 = adb._WA_MEDIA_ROOT + "/WhatsApp Images/ignore.jpg"
        stage_dir = str(tmp_path / "stage")

        target_paths = {"Media/WhatsApp Images/keep.jpg"}

        def _fake_run_adb(cmd, logger):
            if cmd[1] == "pull":
                local = cmd[3]
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local, "wb") as f:
                    f.write(b"data")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with (
            patch("wab_archiver.adb_extractor.get_device_serial", return_value="serial-1"),
            patch("wab_archiver.adb_extractor.probe_md5_binary", return_value="md5sum"),
            patch(
                "wab_archiver.adb_extractor.enumerate_remote_files",
                return_value=[(path1, 4), (path2, 4)],
            ),
            patch("wab_archiver.adb_extractor._run_adb", side_effect=_fake_run_adb),
        ):
            pulled, skipped, conflicts = adb.pull_media(
                stage_dir, False, conn, logger, target_paths
            )

        assert pulled == 1
        assert skipped == 0
        assert conflicts == []
        assert path1 in arc.get_adb_done_paths(conn, "serial-1")
        assert path2 not in arc.get_adb_done_paths(conn, "serial-1")
        conn.close()

    def test_target_paths_empty_returns_zeros(self, tmp_path, logger):
        conn = self._make_conn(tmp_path)
        path1 = adb._WA_MEDIA_ROOT + "/WhatsApp Images/img.jpg"
        stage_dir = str(tmp_path / "stage")

        with (
            patch("wab_archiver.adb_extractor.get_device_serial", return_value="serial-1"),
            patch("wab_archiver.adb_extractor.probe_md5_binary", return_value="md5sum"),
            patch("wab_archiver.adb_extractor.enumerate_remote_files", return_value=[(path1, 100)]),
        ):
            pulled, skipped, conflicts = adb.pull_media(stage_dir, False, conn, logger, set())

        assert pulled == 0
        assert skipped == 0
        assert conflicts == []
        conn.close()


class TestAdbPullState:
    def test_upsert_and_get_done_paths(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/a.jpg", "ABC123", "done")
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/b.jpg", "ABC123", "done")
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/c.jpg", "ABC123", "partial")
        done = arc.get_adb_done_paths(conn, "ABC123")
        assert done == {"/sdcard/Media/a.jpg", "/sdcard/Media/b.jpg"}
        conn.close()

    def test_partial_paths_returned(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/x.jpg", "DEV1", "partial")
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/y.jpg", "DEV1", "done")
        partials = arc.get_adb_partial_paths(conn, "DEV1")
        assert partials == ["/sdcard/Media/x.jpg"]
        conn.close()

    def test_upsert_updates_status(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/a.jpg", "DEV1", "partial")
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/a.jpg", "DEV1", "done")
        done = arc.get_adb_done_paths(conn, "DEV1")
        assert "/sdcard/Media/a.jpg" in done
        partials = arc.get_adb_partial_paths(conn, "DEV1")
        assert partials == []
        conn.close()

    def test_different_devices_isolated(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/a.jpg", "DEV1", "done")
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/a.jpg", "DEV2", "partial")
        assert "/sdcard/Media/a.jpg" in arc.get_adb_done_paths(conn, "DEV1")
        assert "/sdcard/Media/a.jpg" not in arc.get_adb_done_paths(conn, "DEV2")
        assert "/sdcard/Media/a.jpg" in arc.get_adb_partial_paths(conn, "DEV2")
        conn.close()

    def test_remove_pull_state(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        arc.upsert_adb_pull_state(conn, "/sdcard/Media/a.jpg", "DEV1", "done")
        arc.remove_adb_pull_state(conn, "/sdcard/Media/a.jpg", "DEV1")
        assert arc.get_adb_done_paths(conn, "DEV1") == set()
        conn.close()


class TestPrepareInputAdb:
    def test_prepare_input_adb_not_found(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[],
            from_adb=True,
            contacts=None,
            msgstore="dummy",
            output=str(tmp_path),
        )
        with patch("wab_archiver.adb_extractor.check_adb", return_value=False):
            with pytest.raises(SystemExit):
                wa._prepare_input(args, None, logger)

    def test_prepare_input_adb_device_not_connected(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[],
            from_adb=True,
            contacts=None,
            msgstore="dummy",
            output=str(tmp_path),
        )
        with (
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=False),
        ):
            with pytest.raises(SystemExit):
                wa._prepare_input(args, None, logger)

    def test_prepare_input_adb_pull_called_process_error(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[],
            from_adb=True,
            pull_media=False,
            business=False,
            contacts=None,
            msgstore="dummy",
            output=str(tmp_path),
        )
        with (
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=True),
            patch(
                "wab_archiver.adb_extractor.pull_msgstore",
                side_effect=subprocess.CalledProcessError(1, "adb"),
            ),
        ):
            with pytest.raises(SystemExit):
                wa._prepare_input(args, None, logger)

    def test_prepare_input_adb_pull_media_success(self, tmp_path, logger):
        stage_dir = str(tmp_path / "staging")
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[],
            from_adb=True,
            pull_media=True,
            staging=stage_dir,
            business=False,
            contacts=None,
            msgstore="dummy",
            output=str(tmp_path),
        )
        conflicts = [{"original_path": "Media/p.jpg", "reason": "size_mismatch"}]
        with (
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=True),
            patch("wab_archiver.adb_extractor.pull_msgstore", return_value="/pulled/msgstore.db"),
            patch("wab_archiver.adb_extractor.pull_contacts", return_value="/pulled/contacts.txt"),
            patch(
                "wab_archiver.archive_db.open_archive_db", return_value=sqlite3.connect(":memory:")
            ),
            patch(
                "wab_archiver.adb_extractor.pull_media", return_value=(5, 1, conflicts)
            ) as mock_pull_media,
            patch("wab_archiver.adb_extractor.write_adb_conflicts_report") as mock_conf_report,
        ):
            wa._prepare_input(args, None, logger)

        assert args.msgstore == "/pulled/msgstore.db"
        assert args.contacts == "/pulled/contacts.txt"
        assert args.wa_roots == [stage_dir]
        mock_conf_report.assert_called_once()
        mock_pull_media.assert_called_once()
        assert mock_pull_media.call_args[0][4] is None

    def test_prepare_input_adb_pull_media_with_since_ms(self, tmp_path, logger):
        stage_dir = str(tmp_path / "staging")
        db_file = tmp_path / "pulled_msgstore.db"
        conn = _make_msgstore()
        conn.execute("INSERT INTO chat VALUES (1, 'Test Group', 10)")
        conn.execute("INSERT INTO jid VALUES (10, 'group1')")
        conn.execute("INSERT INTO message VALUES (1, 1705000000000, 10, 0)")
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/photo1.jpg', 'image/jpeg', 1, 1, 'url', NULL)"
        )
        conn.commit()
        # Dump memory DB to disk file
        disk_conn = sqlite3.connect(str(db_file))
        conn.backup(disk_conn)
        disk_conn.close()
        conn.close()

        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[],
            from_adb=True,
            pull_media=True,
            staging=stage_dir,
            business=False,
            contacts=None,
            msgstore="dummy",
            output=str(tmp_path),
            since="2024-01-01",
        )
        with (
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=True),
            patch("wab_archiver.adb_extractor.pull_msgstore", return_value=str(db_file)),
            patch("wab_archiver.adb_extractor.pull_contacts", return_value="/pulled/contacts.txt"),
            patch(
                "wab_archiver.archive_db.open_archive_db", return_value=sqlite3.connect(":memory:")
            ),
            patch(
                "wab_archiver.adb_extractor.pull_media", return_value=(1, 0, [])
            ) as mock_pull_media,
        ):
            wa._prepare_input(args, 1704067200000, logger)

        mock_pull_media.assert_called_once()
        target_paths = mock_pull_media.call_args[0][4]
        assert target_paths == {"Media/WhatsApp Images/photo1.jpg"}

    def test_prepare_input_adb_pull_media_runtime_error(self, tmp_path, logger):
        stage_dir = str(tmp_path / "staging")
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[],
            from_adb=True,
            pull_media=True,
            staging=stage_dir,
            business=False,
            contacts=None,
            msgstore="dummy",
            output=str(tmp_path),
        )
        with (
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=True),
            patch("wab_archiver.adb_extractor.pull_msgstore", return_value="/pulled/msgstore.db"),
            patch("wab_archiver.adb_extractor.pull_contacts", return_value="/pulled/contacts.txt"),
            patch(
                "wab_archiver.archive_db.open_archive_db", return_value=sqlite3.connect(":memory:")
            ),
            patch("wab_archiver.adb_extractor.pull_media", side_effect=RuntimeError("USB error")),
        ):
            with pytest.raises(SystemExit):
                wa._prepare_input(args, None, logger)

    def test_prepare_input_loads_android_contacts(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(tmp_path)],
            from_adb=False,
            contacts=str(tmp_path / "contacts.txt"),
            msgstore="dummy",
        )
        with patch(
            "wab_archiver.android_handler.load_contacts", return_value={"39012345": "Alice"}
        ):
            platform, resolver, ios_contacts, contacts = wa._prepare_input(args, None, logger)
        assert contacts == {"39012345": "Alice"}
