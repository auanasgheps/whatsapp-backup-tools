"""Tests for wab_archiver."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import shared.source_detection as sd
import wab_archiver.android_handler as android_handler
import wab_archiver.archive_db as arc
import wab_archiver.ios_handler as ios
import wab_archiver.main as wa
import wab_archiver.progress as prg
from tests.archiver.conftest import (
    _make_msgstore,
    _restore_args,
    _seed_archive,
)


class TestLoadToml:
    def test_valid_toml_returns_dict(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('output = "/tmp/archive"\n', encoding="utf-8")
        result = wa._load_toml(str(cfg))
        assert result == {"output": "/tmp/archive"}

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            wa._load_toml(str(tmp_path / "nonexistent.toml"))

    def test_windows_backslash_path_auto_corrected(self, tmp_path, capsys):
        cfg = tmp_path / "config.toml"
        cfg.write_bytes(b'output = "C:\\Users\\User\\Desktop\\archive"\n')
        result = wa._load_toml(str(cfg))
        assert result["output"] == "C:/Users/User/Desktop/archive"
        assert 'output = "C:/Users/User/Desktop/archive"' in cfg.read_text(encoding="utf-8")
        captured = capsys.readouterr()
        assert "NOTE" in captured.err
        # File must parse cleanly on a second load (no double line endings introduced)
        result2 = wa._load_toml(str(cfg))
        assert result2["output"] == "C:/Users/User/Desktop/archive"

    def test_genuinely_invalid_toml_exits(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("output = [unclosed\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            wa._load_toml(str(cfg))


class TestGenerateConfig:
    def test_writes_example_config(self, tmp_path):
        with pytest.raises(SystemExit):
            wa._generate_config(str(tmp_path))
        dest = tmp_path / "example-config.toml"
        assert dest.exists()
        content = dest.read_text(encoding="utf-8")
        assert "config.toml" in content
        assert "output" in content
        assert "from_adb" in content
        assert "link_duplicates" in content

    def test_overwrites_existing_file(self, tmp_path):
        dest = tmp_path / "example-config.toml"
        dest.write_text("old content", encoding="utf-8")
        with pytest.raises(SystemExit):
            wa._generate_config(str(tmp_path))
        assert "old content" not in dest.read_text(encoding="utf-8")


class TestConfigInParseArgs:
    """Integration tests that exercise config loading through parse_args()."""

    def _write_config(self, path, content):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    @staticmethod
    def _toml_path(p):
        """Convert a path to forward-slash form for safe TOML string literals."""
        return str(p).replace("\\", "/")

    def test_config_values_used_as_defaults(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.wa_roots == ["/wa"]

    def test_config_wa_root_list(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = ["/wa1", "/wa2"]\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.wa_roots == ["/wa1", "/wa2"]

    def test_cli_overrides_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        self._write_config(cfg, 'output = "/from/config"\n')
        out = self._toml_path(tmp_path)
        with patch(
            "sys.argv", ["wa", "--config", str(cfg), "-o", str(tmp_path), "--wa-root", "/cli"]
        ):
            args = wa.parse_args()
        assert args.output == str(tmp_path)

    def test_unknown_key_exits(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\ntypo_key = "bad"\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_since_accepted_in_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\nsince = "2024-01-01"\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.since == "2024-01-01"

    def test_config_from_adb(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\nfrom_adb = true\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.from_adb is True
        assert args.wa_roots == ["/wa"]

    def test_config_link_duplicates(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\nlink_duplicates = true\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.link_duplicates is True

    def test_config_preserve_deleted(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\npreserve_deleted = true\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.preserve_deleted is True

    def test_config_debug(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\ndebug = true\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.debug is True

    def test_config_sections_flattened(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        toml_content = f"""
output = "{out}"

[features]
link_duplicates = true
since = "2024-01-01"

[android]
wa_root = "/wa"
"""
        self._write_config(cfg, toml_content)
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.link_duplicates is True
        assert args.since == "2024-01-01"
        assert args.wa_roots == ["/wa"]

    def test_config_nested_sections_flattened(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        toml_content = f"""
output = "{out}"

[source.android]
wa_root = "/wa_nested"

[options.filtering]
since = "2024-06-01"
link_duplicates = true
"""
        self._write_config(cfg, toml_content)
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.link_duplicates is True
        assert args.since == "2024-06-01"
        assert args.wa_roots == ["/wa_nested"]

    def test_autodetect_single_config_yes(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\n')
        with (
            patch("sys.argv", ["wa"]),
            patch("os.path.dirname", return_value=str(tmp_path)),
            patch("os.getcwd", return_value=str(tmp_path)),
            patch("builtins.input", return_value="y"),
        ):
            args = wa.parse_args()
        assert args.wa_roots == ["/wa"]

    def test_autodetect_single_config_no(self, tmp_path):
        cfg = tmp_path / "config.toml"
        self._write_config(cfg, 'output = "/from/config"\nwa_root = "/wa"\n')
        out = self._toml_path(tmp_path)
        with (
            patch("sys.argv", ["wa", "-o", str(tmp_path), "--wa-root", "/cli"]),
            patch("os.path.dirname", return_value=str(tmp_path)),
            patch("os.getcwd", return_value="/different/dir"),
            patch("builtins.input", return_value="n"),
        ):
            args = wa.parse_args()
        # Config was declined; output comes from CLI
        assert args.output == str(tmp_path)

    def test_autodetect_two_configs_exits(self, tmp_path):
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        self._write_config(dir1 / "config.toml", 'output = "/a"\n')
        self._write_config(dir2 / "config.toml", 'output = "/b"\n')
        with (
            patch("sys.argv", ["wa"]),
            patch("os.path.dirname", return_value=str(dir1)),
            patch("os.getcwd", return_value=str(dir2)),
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_config_equals_form(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        self._write_config(cfg, f'output = "{out}"\nwa_root = "/wa"\n')
        with patch("sys.argv", ["wa", f"--config={cfg}"]):
            args = wa.parse_args()
        assert args.wa_roots == ["/wa"]

    def test_ios_backup_and_wa_root_mutually_exclusive(self, tmp_path):
        with patch(
            "sys.argv",
            ["wa", "archive", "-o", str(tmp_path), "--ios-backup", "/backup", "--wa-root", "/wa"],
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_no_source_exits(self, tmp_path):
        with patch("sys.argv", ["wa", "archive", "-o", str(tmp_path)]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_non_toml_config_exits(self, capsys):
        with patch("sys.argv", ["wa", "--config=cfg.json"]):
            with pytest.raises(SystemExit):
                wa.parse_args()
        assert "toml" in capsys.readouterr().err.lower()

    def test_since_native_date_normalised(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_bytes(b'output = "/tmp"\nwa_root = "/wa"\nsince = 2024-01-01\n')
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            args = wa.parse_args()
        assert args.since == "2024-01-01"

    def test_wa_root_wrong_type_exits(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = self._toml_path(tmp_path)
        cfg.write_text(f'output = "{out}"\nwa_root = 42\n', encoding="utf-8")
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_config_command_exits(self):
        with patch("wab_archiver.main._generate_config", side_effect=SystemExit(0)) as mock_gen:
            with patch("sys.argv", ["wa", "config", "generate"]):
                with pytest.raises(SystemExit):
                    wa.parse_args()
            mock_gen.assert_called_once_with(os.path.dirname(os.path.abspath(wa.__file__)))

    def test_restore_without_output_exits(self):
        with patch("sys.argv", ["wa", "restore"]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_contacts_with_ios_backup_exits(self, tmp_path):
        with patch(
            "sys.argv",
            [
                "wa",
                "archive",
                "-o",
                str(tmp_path),
                "--ios-backup",
                "/backup",
                "--contacts",
                "/contacts",
            ],
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_pull_media_without_staging_exits(self, tmp_path):
        with patch(
            "sys.argv", ["wa", "archive", "-o", str(tmp_path), "--from-adb", "--pull-media"]
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_wa_root_with_from_adb_allowed(self, tmp_path):
        with patch(
            "sys.argv", ["wa", "archive", "-o", str(tmp_path), "--wa-root", "/wa", "--from-adb"]
        ):
            args = wa.parse_args()
        assert args.from_adb is True
        assert args.wa_roots == ["/wa"]

    def test_pull_media_with_wa_root_exits(self, tmp_path):
        with patch(
            "sys.argv",
            [
                "wa",
                "archive",
                "-o",
                str(tmp_path),
                "--from-adb",
                "--pull-media",
                "--staging",
                "/stage",
                "--wa-root",
                "/wa",
            ],
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_from_adb_with_ios_backup_exits(self, tmp_path):
        with patch(
            "sys.argv",
            ["wa", "archive", "-o", str(tmp_path), "--ios-backup", "/backup", "--from-adb"],
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_pull_media_without_from_adb_exits(self, tmp_path):
        with patch(
            "sys.argv",
            ["wa", "archive", "-o", str(tmp_path), "--pull-media", "--staging", "/stage"],
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_no_media_source_exits(self, tmp_path):
        with patch("sys.argv", ["wa", "archive", "-o", str(tmp_path), "--from-adb"]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_restore_with_output_returns_args(self, tmp_path):
        with patch("sys.argv", ["wa", "restore", "-o", str(tmp_path)]):
            args = wa.parse_args()
        assert args.command == "restore"
        assert args.output == str(tmp_path)
        assert args.debug is False

    @pytest.mark.parametrize(
        "cmd,flags,expected_debug",
        [
            ("archive", [], False),
            ("archive", ["-v"], True),
            ("archive", ["--debug"], True),
            ("archive", ["--verbose"], True),
            ("restore", ["-v"], True),
        ],
    )
    def test_debug_flags(self, tmp_path, cmd, flags, expected_debug):
        base = ["wa", cmd, "-o", str(tmp_path)]
        if cmd == "archive":
            base.extend(["--wa-root", str(tmp_path)])
        with patch("sys.argv", [*base, *flags]):
            args = wa.parse_args()
        assert args.debug is expected_debug

    def test_archive_without_output_exits(self, tmp_path):
        with patch("sys.argv", ["wa", "archive", "--wa-root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_wa_root_and_ios_backup_mutual_exclusion(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('output = "/tmp"\nios_backup = "/backup"\n', encoding="utf-8")
        with patch("sys.argv", ["wa", "--config", str(cfg), "--wa-root", str(tmp_path)]):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_pull_media_and_ios_backup_mutual_exclusion(self, tmp_path):
        with patch(
            "sys.argv",
            ["wa", "archive", "-o", str(tmp_path), "--pull-media", "--ios-backup", "/backup"],
        ):
            with pytest.raises(SystemExit):
                wa.parse_args()

    def test_config_command_returns_args_when_mocked(self):
        with (
            patch("sys.argv", ["wa", "config", "generate"]),
            patch("wab_archiver.main._generate_config", return_value=None),
        ):
            args = wa.parse_args()
            assert args.command == "config"


class TestConfigNormalise:
    def test_since_date_literal_normalised(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_bytes(b'output = "/tmp"\nsince = 2024-01-01\n')
        result = wa._load_toml(str(cfg))
        import datetime

        assert isinstance(result["since"], datetime.date)
        # simulate the normalisation that parse_args applies
        if isinstance(result["since"], datetime.date):
            result["since"] = result["since"].isoformat()
        assert result["since"] == "2024-01-01"

    def test_since_wrong_type_exits(self, tmp_path):
        cfg = tmp_path / "config.toml"
        out = str(tmp_path).replace("\\", "/")
        cfg.write_text(f'output = "{out}"\nsince = 123\n', encoding="utf-8")
        with patch("sys.argv", ["wa", "--config", str(cfg)]):
            with pytest.raises(SystemExit):
                wa.parse_args()


class TestSourceDetection:
    def test_android_detected(self, tmp_path):
        (tmp_path / "msgstore.db").touch()
        source_type, db_path = sd.detect_source(str(tmp_path))
        assert source_type == "android"
        assert db_path == str(tmp_path / "msgstore.db")

    def test_ios_detected(self, tmp_path):
        (tmp_path / "ChatStorage.sqlite").touch()
        source_type, db_path = sd.detect_source(str(tmp_path))
        assert source_type == "ios"
        assert db_path == str(tmp_path / "ChatStorage.sqlite")

    def test_android_wins_when_both_present(self, tmp_path):
        (tmp_path / "msgstore.db").touch()
        (tmp_path / "ChatStorage.sqlite").touch()
        source_type, _ = sd.detect_source(str(tmp_path))
        assert source_type == "android"

    def test_returns_none_when_neither(self, tmp_path):
        assert sd.detect_source(str(tmp_path)) == (None, None)

    def test_android_detected_in_whatsapp_databases_subfolder(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        (db_dir / "msgstore.db").touch()
        source_type, db_path = sd.detect_source(str(tmp_path))
        assert source_type == "android"
        assert db_path == str(db_dir / "msgstore.db")

    def test_ios_detected_in_whatsapp_databases_subfolder(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        (db_dir / "ChatStorage.sqlite").touch()
        source_type, db_path = sd.detect_source(str(tmp_path))
        assert source_type == "ios"
        assert db_path == str(db_dir / "ChatStorage.sqlite")


class TestCheckDependencies:
    def _args(self, **kwargs):
        """Return a minimal namespace; only set the fields under test."""
        defaults = dict(from_adb=False, e2e_key=None, ios_password=None)
        defaults.update(kwargs)
        import argparse

        return argparse.Namespace(**defaults)

    def test_no_issues_passes_silently(self, logger):
        args = self._args()
        wa.check_dependencies(args, logger)  # must not raise

    def test_adb_missing_raises(self, logger):
        args = self._args(from_adb=True)
        with patch("wab_archiver.main.shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)

    def test_adb_present_passes(self, logger):
        args = self._args(from_adb=True)
        with patch("wab_archiver.main.shutil.which", return_value="/usr/bin/adb"):
            wa.check_dependencies(args, logger)  # must not raise

    def test_wa_crypt_tools_missing_raises(self, logger):
        args = self._args(e2e_key="key.bin")
        with patch("importlib.util.find_spec", return_value=None):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)

    def test_iphone_backup_decrypt_missing_raises(self, logger):
        args = self._args(ios_password="secret")
        with patch("importlib.util.find_spec", return_value=None):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)

    def test_multiple_missing_reported_together(self, logger, caplog):
        args = self._args(from_adb=True, e2e_key="key.bin")
        # Capture what logger.error receives by inspecting the call
        errors = []
        logger.error = lambda msg, *a, **kw: errors.append(msg)
        with (
            patch("wab_archiver.main.shutil.which", return_value=None),
            patch("importlib.util.find_spec", return_value=None),
            pytest.raises(SystemExit),
        ):
            wa.check_dependencies(args, logger)
        assert len(errors) == 1, "Expected a single combined error message"
        assert "adb" in errors[0]
        assert "wa-crypt-tools" in errors[0]

    def test_tzdata_missing_on_windows_raises(self, logger):
        args = self._args(timezone="Europe/Berlin")
        with (
            patch("wab_archiver.main.sys.platform", "win32"),
            patch("importlib.util.find_spec", return_value=None),
        ):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)


class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def _clear_handlers(self):
        logging.getLogger("wab_archiver").handlers.clear()
        yield
        logging.getLogger("wab_archiver").handlers.clear()

    def test_creates_file_and_stream_handlers(self, tmp_path):
        log = wa.setup_logging(str(tmp_path / "app.log"), False)
        assert len(log.handlers) == 2
        handler_types = {type(h).__name__ for h in log.handlers}
        assert "FileHandler" in handler_types
        assert "StreamHandler" in handler_types

    def test_file_handler_targets_correct_path(self, tmp_path):
        log = wa.setup_logging(str(tmp_path / "app.log"), False)
        file_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0].baseFilename.endswith("app.log")

    def test_second_call_does_not_add_handlers(self, tmp_path):
        wa.setup_logging(str(tmp_path / "app.log"), False)
        wa.setup_logging(str(tmp_path / "app.log"), False)
        assert len(logging.getLogger("wab_archiver").handlers) == 2

    def test_default_file_handler_level_is_info(self, tmp_path):
        log = wa.setup_logging(str(tmp_path / "app.log"), False)
        file_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
        stream_handlers = [h for h in log.handlers if type(h) is logging.StreamHandler]
        assert file_handlers[0].level == logging.INFO
        assert stream_handlers[0].level == logging.INFO
        assert log.level == logging.INFO

    def test_debug_enabled_sets_file_and_logger_to_debug(self, tmp_path):
        log = wa.setup_logging(str(tmp_path / "app.log"), True)
        file_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
        stream_handlers = [h for h in log.handlers if type(h) is logging.StreamHandler]
        assert file_handlers[0].level == logging.DEBUG
        assert stream_handlers[0].level == logging.INFO
        assert log.level == logging.DEBUG

    def test_subsequent_call_updates_level(self, tmp_path):
        wa.setup_logging(str(tmp_path / "app.log"), False)
        log = wa.setup_logging(str(tmp_path / "app.log"), True)
        file_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
        assert file_handlers[0].level == logging.DEBUG
        assert log.level == logging.DEBUG


class TestProgressReporter:
    def test_no_default_parameter_values(self):
        import inspect

        for member_name in dir(prg.ProgressReporter):
            if member_name.startswith("_") and member_name != "__init__":
                continue
            member = getattr(prg.ProgressReporter, member_name)
            if callable(member):
                sig = inspect.signature(member)
                for p_name, param in sig.parameters.items():
                    assert param.default == inspect.Parameter.empty, (
                        f"ProgressReporter.{member_name} parameter '{p_name}' "
                        f"has default value: {param.default}"
                    )

    def test_isatty_in_place_rendering(self, logger):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        reporter = prg.ProgressReporter("Archiving", 100, logger, stream, 0.0)
        reporter.update(50, {"Copied": 10, "Skipped": 40})
        output = stream.getvalue()
        assert "\rArchiving: 50.0% (50/100) | Copied: 10 | Skipped: 40" in output

        reporter.finish({"Copied": 10, "Skipped": 40})
        final_output = stream.getvalue()
        assert final_output.endswith("\n")

    def test_isatty_interleaving_guard(self):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        test_logger = logging.getLogger("test_guard")
        test_logger.setLevel(logging.INFO)
        test_logger.handlers = []
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        test_logger.addHandler(handler)

        reporter = prg.ProgressReporter("Archiving", 100, test_logger, stream, 0.0)
        reporter.update(10, {"Copied": 5})

        test_logger.warning("File missing!")

        output = stream.getvalue()
        assert "File missing!\n" in output

        reporter.finish({"Copied": 5})

    def test_non_tty_milestone_logging(self, logger):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: False

        mock_logger = MagicMock()
        info_logs = []
        mock_logger.info.side_effect = lambda msg, *a, **kw: info_logs.append(str(msg))

        reporter = prg.ProgressReporter("Archiving", 50, mock_logger, stream, 0.0)
        for i in range(1, 51):
            reporter.update(i, {"Copied": i})
        reporter.finish({"Copied": 50})

        # Check that no \r was written to non-tty stream
        assert "\r" not in stream.getvalue()
        # Check that milestones were logged
        assert any("Archiving: 10/50" in msg for msg in info_logs)
        assert any("Archiving: 50/50" in msg for msg in info_logs)

    def test_zero_total_handling(self, logger):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        reporter = prg.ProgressReporter("Evaluating", 0, logger, stream, 0.0)
        reporter.update(0, {})
        reporter.finish({})
        assert "Evaluating: 0" in stream.getvalue()

    def test_cursor_visibility_management(self, logger):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        reporter = prg.ProgressReporter("Archiving", 100, logger, stream, 0.0)
        assert not reporter._cursor_hidden

        reporter.update(10, {"Copied": 5})
        assert reporter._cursor_hidden
        assert "\033[?25l" in stream.getvalue()

        reporter.finish({"Copied": 5})
        assert not reporter._cursor_hidden
        assert "\033[?25h" in stream.getvalue()

    def test_redundant_redraw_skipped(self, logger):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        reporter = prg.ProgressReporter("Archiving", 100, logger, stream, 0.0)
        reporter.update(10, {"Copied": 5})
        len_after_first = len(stream.getvalue())

        # Second update with same current and stats should be suppressed
        reporter.update(10, {"Copied": 5})
        assert len(stream.getvalue()) == len_after_first

        # Update with changed stats should render
        reporter.update(11, {"Copied": 6})
        assert len(stream.getvalue()) > len_after_first
        reporter.finish({"Copied": 6})

    def test_file_handler_excluded_from_guard_and_filter(self, tmp_path):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        log_file = tmp_path / "test.log"
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.DEBUG)

        ch = logging.StreamHandler(stream)
        ch.setLevel(logging.INFO)

        test_logger = logging.getLogger("test_fh_exclusion")
        test_logger.setLevel(logging.DEBUG)
        test_logger.handlers = [fh, ch]

        reporter = prg.ProgressReporter("Archiving", 100, test_logger, stream, 0.0)
        reporter.update(10, {"Copied": 5})

        val_before_debug = stream.getvalue()
        # Debug logs go to file handler, should NOT clear or alter stream
        test_logger.debug("COPIED: media.jpg -> archive/media.jpg")
        assert stream.getvalue() == val_before_debug

        reporter.finish({"Copied": 5})
        fh.close()

    def test_interleaving_log_immediately_restores_status_line(self):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        test_logger = logging.getLogger("test_restore_status")
        test_logger.setLevel(logging.INFO)
        test_logger.handlers = []
        ch = logging.StreamHandler(stream)
        ch.setFormatter(logging.Formatter("%(message)s"))
        test_logger.addHandler(ch)

        reporter = prg.ProgressReporter("Archiving", 100, test_logger, stream, 0.0)
        reporter.update(10, {"Copied": 5})

        test_logger.warning("Missing source file")
        output = stream.getvalue()
        assert "Missing source file\n" in output
        # Status line must be immediately rewritten after the log line
        assert output.endswith("Archiving: 10.0% (10/100) | Copied: 5")

        reporter.finish({"Copied": 5})

    def test_file_handler_receives_milestones_when_tty(self, tmp_path):
        import io

        stream = io.StringIO()
        stream.isatty = lambda: True

        log_file = tmp_path / "milestones.log"
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.INFO)

        test_logger = logging.getLogger("test_milestone_fh")
        test_logger.setLevel(logging.INFO)
        test_logger.handlers = [fh]

        reporter = prg.ProgressReporter("Archiving", 10, test_logger, stream, 0.0)
        reporter.update(10, {"Copied": 10})
        reporter.finish({"Copied": 10})
        fh.close()

        log_content = log_file.read_text(encoding="utf-8")
        assert "Archiving: 10/10 (100.0%) | Copied: 10" in log_content


class TestInjectDefaultArchiveCommand:
    def test_help_flag_not_injected(self):
        with patch("sys.argv", ["prog", "--help"]):
            wa._inject_default_archive_command()
            assert sys.argv[1] == "--help"

    def test_no_command_injects_archive(self):
        with patch("sys.argv", ["prog", "--wa-root", "/wa"]):
            wa._inject_default_archive_command()
            assert sys.argv[1] == "archive"


class TestWarnNetworkPaths:
    def test_unc_output_warns(self):
        args = argparse.Namespace(output="\\\\server\\share", wa_roots=[])
        mock_logger = MagicMock()
        wa._warn_network_paths(args, mock_logger)
        assert mock_logger.warning.called

    def test_unc_wa_root_warns(self):
        args = argparse.Namespace(output="/tmp/out", wa_roots=["\\\\nas\\wa"])
        mock_logger = MagicMock()
        wa._warn_network_paths(args, mock_logger)
        assert mock_logger.warning.called

    def test_normal_paths_no_warning(self):
        args = argparse.Namespace(output="/tmp/out", wa_roots=["/wa/media"])
        mock_logger = MagicMock()
        wa._warn_network_paths(args, mock_logger)
        mock_logger.warning.assert_not_called()


class TestRunRestoreMode:
    def test_empty_archive_exits(self, tmp_path, logger):
        arc.open_archive_db(str(tmp_path)).close()
        with pytest.raises(SystemExit):
            wa.run_restore_mode(_restore_args(tmp_path), logger)

    def test_ios_only_exits(self, tmp_path, logger):
        _seed_archive(tmp_path, "Message/foo.jpg", "Contacts/Alice/2024/Received/foo.jpg")
        with pytest.raises(SystemExit):
            wa.run_restore_mode(_restore_args(tmp_path), logger)

    def test_ios_and_android_warns(self, tmp_path):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        _seed_archive(tmp_path, "Message/ios.jpg", "Contacts/Alice/2024/Received/ios.jpg")
        mock_logger = MagicMock()
        wa.run_restore_mode(_restore_args(tmp_path), mock_logger)
        assert mock_logger.warning.called

    def test_happy_path_restores_file(self, tmp_path, logger):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        wa.run_restore_mode(_restore_args(tmp_path), logger)
        dest = tmp_path / "Media" / "WhatsApp Images" / "img.jpg"
        assert dest.exists()

    def test_dry_run_does_not_copy(self, tmp_path, logger):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        wa.run_restore_mode(_restore_args(tmp_path, dry_run=True), logger)
        dest = tmp_path / "Media" / "WhatsApp Images" / "img.jpg"
        assert not dest.exists()

    def test_unrestorable_no_archive_copy(self, tmp_path, logger):
        # DB row exists but the archive file is absent from disk
        conn = arc.open_archive_db(str(tmp_path))
        md5 = hashlib.md5(b"x").digest()
        arc.record_file_archived(
            conn.cursor(), "Media/img.jpg", md5, "Contacts/Alice/2024/Received/img.jpg", 1, 0, None
        )
        conn.commit()
        conn.close()
        wa.run_restore_mode(_restore_args(tmp_path), logger)
        report = tmp_path / "restore_report.csv"
        assert report.exists()
        assert "unrestorable" in report.read_text(encoding="utf-8")

    def test_already_restored_skipped(self, tmp_path, logger):
        content = b"same content"
        _seed_archive(
            tmp_path,
            "Media/WhatsApp Images/img.jpg",
            "Contacts/Alice/2024/Received/img.jpg",
            content,
        )
        dest = tmp_path / "Media" / "WhatsApp Images" / "img.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        wa.run_restore_mode(_restore_args(tmp_path), logger)
        report = tmp_path / "restore_report.csv"
        assert not report.exists()

    def test_collision_skipped(self, tmp_path, logger):
        _seed_archive(
            tmp_path,
            "Media/WhatsApp Images/img.jpg",
            "Contacts/Alice/2024/Received/img.jpg",
            b"archive content",
        )
        dest = tmp_path / "Media" / "WhatsApp Images" / "img.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"different content")
        wa.run_restore_mode(_restore_args(tmp_path), logger)
        report = tmp_path / "restore_report.csv"
        assert report.exists()
        assert "collision_skipped" in report.read_text(encoding="utf-8")

    def test_copy_error_writes_report(self, tmp_path, logger):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        with patch("wab_archiver.main.shutil.copy2", side_effect=OSError("disk full")):
            wa.run_restore_mode(_restore_args(tmp_path), logger)
        report = tmp_path / "restore_report.csv"
        assert report.exists()
        assert "error:" in report.read_text(encoding="utf-8")

    def test_restore_mode_restores_database(self, tmp_path, logger):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir(parents=True)
        (db_dir / "msgstore.db").write_bytes(b"sqlite_db_payload_bytes")

        wa.run_restore_mode(_restore_args(tmp_path), logger)

        restored_db = tmp_path / "Databases" / "msgstore.db"
        assert restored_db.exists()
        assert restored_db.read_bytes() == b"sqlite_db_payload_bytes"

    def test_restore_mode_database_dry_run(self, tmp_path, logger):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir(parents=True)
        (db_dir / "msgstore.db").write_bytes(b"sqlite_db_payload_bytes")

        wa.run_restore_mode(_restore_args(tmp_path, dry_run=True), logger)

        restored_db = tmp_path / "Databases" / "msgstore.db"
        assert not restored_db.exists()

    def test_restore_mode_database_identical_skipped(self, tmp_path, logger):
        _seed_archive(
            tmp_path, "Media/WhatsApp Images/img.jpg", "Contacts/Alice/2024/Received/img.jpg"
        )
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir(parents=True)
        (db_dir / "msgstore.db").write_bytes(b"sqlite_db_payload_bytes")

        restored_db = tmp_path / "Databases" / "msgstore.db"
        restored_db.parent.mkdir(parents=True)
        restored_db.write_bytes(b"sqlite_db_payload_bytes")

        wa.run_restore_mode(_restore_args(tmp_path), logger)
        assert restored_db.read_bytes() == b"sqlite_db_payload_bytes"


class TestMain:
    def _args(self, tmp_path, command: str, dry_run: bool, debug: bool):
        return argparse.Namespace(
            command=command,
            dry_run=dry_run,
            output=str(tmp_path),
            log=None,
            wa_roots=[],
            debug=debug,
        )

    def test_dispatches_to_forward_mode(self, tmp_path):
        args = self._args(tmp_path, "archive", False, False)
        with (
            patch("wab_archiver.main.parse_args", return_value=args),
            patch("wab_archiver.main.setup_logging", return_value=MagicMock()),
            patch("wab_archiver.main._warn_network_paths"),
            patch("wab_archiver.main.run_forward_mode") as mock_fwd,
            patch("wab_archiver.main.run_restore_mode") as mock_rst,
        ):
            wa.main()
        mock_fwd.assert_called_once()
        mock_rst.assert_not_called()

    def test_dispatches_to_restore_mode(self, tmp_path):
        args = self._args(tmp_path, "restore", False, False)
        with (
            patch("wab_archiver.main.parse_args", return_value=args),
            patch("wab_archiver.main.setup_logging", return_value=MagicMock()),
            patch("wab_archiver.main._warn_network_paths"),
            patch("wab_archiver.main.run_forward_mode") as mock_fwd,
            patch("wab_archiver.main.run_restore_mode") as mock_rst,
        ):
            wa.main()
        mock_rst.assert_called_once()
        mock_fwd.assert_not_called()

    def test_dry_run_logged(self, tmp_path):
        args = self._args(tmp_path, "archive", True, False)
        mock_logger = MagicMock()
        with (
            patch("wab_archiver.main.parse_args", return_value=args),
            patch("wab_archiver.main.setup_logging", return_value=mock_logger),
            patch("wab_archiver.main._warn_network_paths"),
            patch("wab_archiver.main.run_forward_mode"),
            patch("wab_archiver.main.run_restore_mode"),
        ):
            wa.main()
        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("DRY RUN" in m for m in info_msgs)

    def test_restore_dry_run_logged(self, tmp_path):
        args = self._args(tmp_path, "restore", True, False)
        mock_logger = MagicMock()
        with (
            patch("wab_archiver.main.parse_args", return_value=args),
            patch("wab_archiver.main.setup_logging", return_value=mock_logger),
            patch("wab_archiver.main._warn_network_paths"),
            patch("wab_archiver.main.run_forward_mode"),
            patch("wab_archiver.main.run_restore_mode"),
        ):
            wa.main()
        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("DRY RUN MODE" in m for m in info_msgs)

    def test_main_passes_debug_flag_to_setup_logging(self, tmp_path):
        args = self._args(tmp_path, "archive", False, True)
        with (
            patch("wab_archiver.main.parse_args", return_value=args),
            patch("wab_archiver.main.setup_logging", return_value=MagicMock()) as mock_setup,
            patch("wab_archiver.main._warn_network_paths"),
            patch("wab_archiver.main.run_forward_mode"),
        ):
            wa.main()
        expected_log = str(tmp_path / "wab-archiver.log")
        mock_setup.assert_called_once_with(expected_log, True)


class TestMainEntrypoint:
    def test_import_main_does_not_execute(self):
        with patch("wab_archiver.main.main") as mock_main:
            import importlib

            import wab_archiver.__main__

            importlib.reload(wab_archiver.__main__)
            mock_main.assert_not_called()

    def test_run_as_directory_help(self):
        res = subprocess.run(
            [sys.executable, "wab_archiver", "--help"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        assert "Archive WhatsApp media" in res.stdout

    def test_run_as_module_help(self):
        res = subprocess.run(
            [sys.executable, "-m", "wab_archiver", "--help"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        assert "Archive WhatsApp media" in res.stdout


class TestModuleTopLevelExecution:
    def test_python_version_guard(self):
        import runpy

        cached = sys.modules.pop("wab_archiver.main", None)
        try:
            with patch("sys.version_info", (3, 10)), patch("sys.stderr.write"):
                with pytest.raises(SystemExit) as exc_info:
                    runpy.run_module("wab_archiver.main", run_name="_not_main_")
                assert exc_info.value.code == 1
        finally:
            if cached is not None:
                sys.modules["wab_archiver.main"] = cached

    def test_main_guard_invoked(self):
        import runpy

        cached = sys.modules.pop("wab_archiver.main", None)
        try:
            with patch("sys.argv", ["wa", "--version"]):
                with pytest.raises(SystemExit) as exc_info:
                    runpy.run_module("wab_archiver.main", run_name="__main__")
                assert exc_info.value.code == 0
        finally:
            if cached is not None:
                sys.modules["wab_archiver.main"] = cached


class TestRunForwardModeFilters:
    def test_run_forward_mode_msgstore_missing(self, tmp_path, logger):
        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone=None,
            since=None,
            limit=None,
            ios_backup=None,
            dry_run=True,
            msgstore=str(tmp_path / "nonexistent.db"),
            wa_roots=[str(tmp_path)],
            output=str(tmp_path),
            contacts=None,
        )
        with pytest.raises(SystemExit):
            wa.run_forward_mode(args, logger)

    def test_run_forward_mode_valid_timezone(self, tmp_path, logger):
        db_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        (wa_root / "Message").mkdir()

        out_dir = tmp_path / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone="UTC",
            since=None,
            limit=None,
            ios_backup=None,
            dry_run=True,
            msgstore=str(db_path),
            wa_roots=[str(wa_root)],
            output=str(out_dir),
            contacts=None,
            log=None,
        )
        with (
            patch("wab_archiver.main.check_dependencies"),
            patch("wab_archiver.ios_handler.validate_ios_schema"),
            patch("wab_archiver.ios_handler.validate_ios_wa_root"),
            patch("wab_archiver.ios_handler.build_ios_number_map", return_value={}),
            patch("wab_archiver.ios_handler.build_ios_pushname_map", return_value={}),
            patch("wab_archiver.ios_handler.find_ios_ext_db", return_value=None),
            patch("wab_archiver.ios_handler.check_ios_hd_association", return_value=False),
            patch("wab_archiver.ios_handler.build_ios_query", return_value="SELECT 1 WHERE 0"),
            patch(
                "wab_archiver.ios_handler.build_ios_group_subjects_query",
                return_value="SELECT '1', 'g' WHERE 0",
            ),
            patch(
                "wab_archiver.main.process_rows",
                return_value=({"copied": 0, "skipped": 0, "missing": 0, "warnings": 0}, {}, {}, []),
            ),
        ):
            wa.run_forward_mode(args, logger)

    def test_run_forward_mode_adb_pull_media_with_since(self, tmp_path, logger):
        out_dir = tmp_path / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = tmp_path / "staging"

        db_file = tmp_path / "msgstore.db"
        conn = _make_msgstore()
        conn.execute("INSERT INTO chat VALUES (1, 'Test Group', 10)")
        conn.execute("INSERT INTO jid VALUES (10, 'group1')")
        conn.execute("INSERT INTO message VALUES (1, 1705000000000, 10, 0)")
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/img.jpg', 'image/jpeg', 1, 1, 'url', NULL)"
        )
        conn.commit()
        disk_conn = sqlite3.connect(str(db_file))
        conn.backup(disk_conn)
        disk_conn.close()
        conn.close()

        args = argparse.Namespace(
            from_adb=True,
            e2e_key=None,
            ios_password=None,
            timezone="UTC",
            since="2024-01-01",
            limit=None,
            ios_backup=None,
            dry_run=True,
            msgstore="dummy",
            pull_media=True,
            staging=str(stage_dir),
            wa_roots=[],
            business=False,
            output=str(out_dir),
            contacts=None,
            log=None,
        )
        with (
            patch("wab_archiver.main.check_dependencies"),
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=True),
            patch("wab_archiver.adb_extractor.pull_msgstore", return_value=str(db_file)),
            patch(
                "wab_archiver.adb_extractor.pull_contacts",
                return_value=str(tmp_path / "contacts.txt"),
            ),
            patch("wab_archiver.android_handler.load_contacts", return_value={}),
            patch(
                "wab_archiver.adb_extractor.pull_media", return_value=(1, 0, [])
            ) as mock_pull_media,
            patch("wab_archiver.android_handler.validate_schema"),
            patch("wab_archiver.android_handler.validate_wa_root"),
            patch("wab_archiver.android_handler.build_number_map", return_value={}),
            patch(
                "wab_archiver.main.process_rows",
                return_value=({"copied": 0, "skipped": 0, "missing": 0, "warnings": 0}, {}, {}, []),
            ),
        ):
            wa.run_forward_mode(args, logger)

        mock_pull_media.assert_called_once()
        target_paths = mock_pull_media.call_args[0][4]
        assert target_paths == {"Media/WhatsApp Images/img.jpg"}

    def test_run_forward_mode_invalid_timezone(self, tmp_path, logger):
        db_path = tmp_path / "plain.db"
        db_path.touch()
        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone="Invalid/Timezone_Name_XYZ",
            since=None,
            limit=None,
            ios_backup=None,
            dry_run=True,
            msgstore=str(db_path),
            wa_roots=[str(tmp_path)],
            output=str(tmp_path),
            contacts=None,
            log=None,
        )
        with patch("wab_archiver.main.check_dependencies"), pytest.raises(SystemExit):
            wa.run_forward_mode(args, logger)

    def test_run_forward_mode_valid_since(self, tmp_path, logger):
        db_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        (wa_root / "Message").mkdir()

        out_dir = tmp_path / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone="UTC",
            since="2024-06-15",
            limit=100,
            ios_backup=None,
            dry_run=True,
            msgstore=str(db_path),
            wa_roots=[str(wa_root)],
            output=str(out_dir),
            contacts=None,
            log=None,
        )
        with (
            patch("wab_archiver.main.check_dependencies"),
            patch("wab_archiver.ios_handler.validate_ios_schema"),
            patch("wab_archiver.ios_handler.validate_ios_wa_root"),
            patch("wab_archiver.ios_handler.build_ios_number_map", return_value={}),
            patch("wab_archiver.ios_handler.build_ios_pushname_map", return_value={}),
            patch("wab_archiver.ios_handler.find_ios_ext_db", return_value=None),
            patch("wab_archiver.ios_handler.check_ios_hd_association", return_value=False),
            patch(
                "wab_archiver.ios_handler.build_ios_query", return_value="SELECT 1 WHERE 0"
            ) as mock_query,
            patch(
                "wab_archiver.ios_handler.build_ios_group_subjects_query",
                return_value="SELECT '1', 'g' WHERE 0",
            ),
            patch(
                "wab_archiver.main.process_rows",
                return_value=({"copied": 0, "skipped": 0, "missing": 0, "warnings": 0}, {}, {}, []),
            ),
        ):
            wa.run_forward_mode(args, logger)
        mock_query.assert_called_once()
        assert mock_query.call_args[0][0] == 100
        assert mock_query.call_args[0][1] is not None

    def test_run_forward_mode_invalid_since(self, tmp_path, logger):
        db_path = tmp_path / "plain.db"
        db_path.touch()
        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone=None,
            since="invalid-format-since",
            limit=None,
            ios_backup=None,
            dry_run=True,
            msgstore=str(db_path),
            wa_roots=[str(tmp_path)],
            output=str(tmp_path),
            contacts=None,
            log=None,
        )
        with pytest.raises(SystemExit):
            wa.run_forward_mode(args, logger)

    def test_run_forward_mode_ios_contacts_and_pushnames(self, tmp_path, logger):
        db_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        contacts_file = tmp_path / "ContactsV2.sqlite"
        contacts_file.touch()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        (wa_root / "Message").mkdir()

        out_dir = tmp_path / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone=None,
            since=None,
            limit=None,
            ios_backup=None,
            dry_run=True,
            msgstore=str(db_path),
            wa_roots=[str(wa_root)],
            output=str(out_dir),
            contacts=None,
            ios_contacts=str(contacts_file),
            log=None,
        )
        loaded_contacts = {"39111": "Loaded Contact"}
        pushnames = {"39222": "Pushname User", "39111": "Ignored Pushname"}

        with (
            patch("wab_archiver.ios_handler.validate_ios_schema"),
            patch("wab_archiver.ios_handler.validate_ios_wa_root"),
            patch("wab_archiver.ios_handler.build_ios_number_map", return_value={}),
            patch(
                "wab_archiver.ios_handler.load_ios_contacts", return_value=loaded_contacts
            ) as mock_load_contacts,
            patch("wab_archiver.ios_handler.build_ios_pushname_map", return_value=pushnames),
            patch("wab_archiver.ios_handler.find_ios_ext_db", return_value=None),
            patch("wab_archiver.ios_handler.check_ios_hd_association", return_value=False),
            patch("wab_archiver.ios_handler.build_ios_query", return_value="SELECT 1 WHERE 0"),
            patch(
                "wab_archiver.ios_handler.build_ios_group_subjects_query",
                return_value="SELECT '1', 'g' WHERE 0",
            ),
            patch(
                "wab_archiver.main.process_rows",
                return_value=({"copied": 0, "skipped": 0, "missing": 0, "warnings": 0}, {}, {}, []),
            ) as mock_proc,
        ):
            wa.run_forward_mode(args, logger)

        mock_load_contacts.assert_called_once_with(str(contacts_file), logger)
        passed_contacts = mock_proc.call_args[0][2]
        assert passed_contacts["39111"] == "Loaded Contact"
        assert passed_contacts["39222"] == "Pushname User"


class TestRestoreModeFilesystemAndVcard:
    def test_restore_mode_restores_duplicates_as_independent_copies(self, tmp_path, logger):
        out = tmp_path / "archive"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()

        content = b"restored file data"
        md5 = hashlib.md5(content).digest()

        # Seed two original paths with duplicate content
        p1 = out / "Contacts" / "Alice" / "img1.jpg"
        p1.parent.mkdir(parents=True, exist_ok=True)
        p1.write_bytes(content)

        p2 = out / "Groups" / "Family" / "img2.jpg"
        p2.parent.mkdir(parents=True, exist_ok=True)
        # On disk p2 is hardlinked to p1
        os.link(str(p1), str(p2))

        arc.record_file_archived(
            cur,
            "Media/WhatsApp Images/img1.jpg",
            md5,
            "Contacts/Alice/img1.jpg",
            len(content),
            0,
            None,
        )
        arc.record_file_archived(
            cur,
            "Media/WhatsApp Images/img2.jpg",
            md5,
            "Groups/Family/img2.jpg",
            len(content),
            1,
            None,
        )
        conn.commit()
        conn.close()

        args = argparse.Namespace(output=str(out), dry_run=False)
        wa.run_restore_mode(args, logger)

        restored1 = out / "Media" / "WhatsApp Images" / "img1.jpg"
        restored2 = out / "Media" / "WhatsApp Images" / "img2.jpg"
        assert restored1.exists()
        assert restored2.exists()
        assert restored1.read_bytes() == content
        assert restored2.read_bytes() == content
        # Restored files are separate independent copies
        s1 = os.stat(str(restored1))
        s2 = os.stat(str(restored2))
        assert s1.st_ino != s2.st_ino

    def test_restore_mode_ignores_vcard_entries(self, tmp_path, logger):
        out = tmp_path / "archive"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()

        vcard_bytes = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alice\r\nEND:VCARD\r\n"
        vcard_hash = hashlib.md5(vcard_bytes).digest()

        vcard_file = out / "Contacts" / "Alice" / "Contact.vcf"
        vcard_file.parent.mkdir(parents=True, exist_ok=True)
        vcard_file.write_bytes(vcard_bytes)

        # Record virtual vcard
        arc.record_file_archived(
            cur, "vcard:101", vcard_hash, "Contacts/Alice/Contact.vcf", len(vcard_bytes), 0, None
        )
        conn.commit()
        conn.close()

        args = argparse.Namespace(output=str(out), dry_run=False)
        # Should exit with code 1 because no valid Media/ files exist to restore (vcards ignored)
        with pytest.raises(SystemExit):
            wa.run_restore_mode(args, logger)

    def test_restore_mode_restores_legitimate_vcf_document(self, tmp_path, logger):
        out = tmp_path / "archive"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()

        vcard_bytes = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bob\r\nEND:VCARD\r\n"
        vcard_hash = hashlib.md5(vcard_bytes).digest()

        vcf_doc = out / "Contacts" / "Bob" / "contacts.vcf"
        vcf_doc.parent.mkdir(parents=True, exist_ok=True)
        vcf_doc.write_bytes(vcard_bytes)

        # Record real Media/ document file with .vcf extension
        arc.record_file_archived(
            cur,
            "Media/WhatsApp Documents/contacts.vcf",
            vcard_hash,
            "Contacts/Bob/contacts.vcf",
            len(vcard_bytes),
            0,
            None,
        )
        conn.commit()
        conn.close()

        args = argparse.Namespace(output=str(out), dry_run=False)
        wa.run_restore_mode(args, logger)

        restored = out / "Media" / "WhatsApp Documents" / "contacts.vcf"
        assert restored.exists()
        assert restored.read_bytes() == vcard_bytes

    def test_duplicate_report_distinct_paths(self, tmp_path, logger):
        out = tmp_path / "archive"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()

        content = b"identical-content"
        h = hashlib.md5(content).digest()

        # Two different original paths mapped to the SAME archive_path (e.g. repeated send skipped)
        arc.record_file_archived(
            cur, "Media/IMG1.jpg", h, "Contacts/A/IMG1.jpg", len(content), 0, None
        )
        arc.record_file_archived(
            cur, "Media/IMG2.jpg", h, "Contacts/A/IMG1.jpg", len(content), 0, None
        )
        conn.commit()

        report = out / "dup_report.csv"
        wa.write_duplicate_report(str(report), conn, logger, None)

        # Since there is only 1 distinct archive_path, it must NOT be reported as a duplicate of itself
        assert not report.exists()

        # Now add a second distinct archive_path
        arc.record_file_archived(
            cur, "Media/IMG3.jpg", h, "Contacts/B/IMG1.jpg", len(content), 1, None
        )
        conn.commit()

        wa.write_duplicate_report(str(report), conn, logger, None)
        lines = [line.strip() for line in report.read_text().splitlines() if line.strip()]
        # Header + 1 section divider + 2 distinct archive path rows (Contacts/A/IMG1.jpg and Contacts/B/IMG1.jpg)
        assert len(lines) == 4
        conn.close()


class TestVcardExtraction:
    def test_resolve_vcard_display_name_single_name(self):
        from shared.db import resolve_vcard_display_name

        name = resolve_vcard_display_name("John Doe", None)
        assert name == "John Doe"

    def test_resolve_vcard_display_name_ios_separator(self):
        from shared.db import resolve_vcard_display_name

        name = resolve_vcard_display_name("2 contacts_$!<Name-Separator>!$_Jane Doe", None)
        assert name == "Jane Doe"

        pair = resolve_vcard_display_name(
            "3 contacts_$!<Name-Separator>!$_Alice_$!<Name-Separator>!$_Bob", None
        )
        assert pair == "Alice & Bob"

        multi = resolve_vcard_display_name(
            "4 contacts_$!<Name-Separator>!$_Alice_$!<Name-Separator>!$_Bob_$!<Name-Separator>!$_Charlie",
            None,
        )
        assert multi == "Alice and 2 others"

    def test_resolve_vcard_display_name_fn_fallback(self):
        from shared.db import resolve_vcard_display_name

        vcard = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Carlos Danger\r\nEND:VCARD\r\n"
        name = resolve_vcard_display_name(None, vcard)
        assert name == "Carlos Danger"

    def test_resolve_vcard_display_name_multiple_vcards(self):
        from shared.db import resolve_vcard_display_name

        vcard = (
            "BEGIN:VCARD\r\nFN:Alpha\r\nEND:VCARD\r\n"
            "BEGIN:VCARD\r\nFN:Beta\r\nEND:VCARD\r\n"
            "BEGIN:VCARD\r\nFN:Gamma\r\nEND:VCARD\r\n"
        )
        name = resolve_vcard_display_name(None, vcard)
        assert name == "Alpha and 2 others"

    def test_resolve_vcard_display_name_empty_fallback(self):
        from shared.db import resolve_vcard_display_name

        name = resolve_vcard_display_name(None, None)
        assert name == "Contact"

        name_empty = resolve_vcard_display_name("", "")
        assert name_empty == "Contact"

    def test_resolve_vcard_display_name_parameterized_fn(self):
        from shared.db import resolve_vcard_display_name

        vcard_param = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN;CHARSET=UTF-8:Carlos Danger\r\nEND:VCARD\r\n"
        )
        name = resolve_vcard_display_name(None, vcard_param)
        assert name == "Carlos Danger"

        vcard_lower = "BEGIN:VCARD\r\nfn:Mario Rossi\r\nEND:VCARD\r\n"
        name2 = resolve_vcard_display_name(None, vcard_lower)
        assert name2 == "Mario Rossi"

        # Portuguese / international counter string should extract FN from vcard
        name3 = resolve_vcard_display_name("2 contatos", vcard_param)
        assert name3 == "Carlos Danger"

    def test_has_vcard_support_android(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        assert not android_handler.has_vcard_support(cur)
        cur.execute("CREATE TABLE message_vcard (message_row_id INTEGER, vcard TEXT)")
        assert android_handler.has_vcard_support(cur)
        conn.close()

    def test_build_vcard_query_android(self):
        query = android_handler.build_vcard_query(10, 1600000000000)
        assert "message_vcard" in query
        assert "message_type IN (4, 14)" in query
        assert "vcard_text" in query
        assert "LIMIT 5" in query
        assert "1600000000000" in query

    def test_has_ios_vcard_support(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        assert not ios.has_ios_vcard_support(cur)
        cur.execute("CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
        assert not ios.has_ios_vcard_support(cur)
        cur.execute("ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZVCARDSTRING TEXT")
        assert ios.has_ios_vcard_support(cur)
        conn.close()

    def test_build_ios_vcard_query(self):
        query = ios.build_ios_vcard_query(20, 1600000000000)
        assert "ZVCARDSTRING" in query
        assert "ZVCARDNAME" in query
        assert "ZMESSAGETYPE = 4" in query
        assert "LIMIT 10" in query

    def test_resolve_unique_vcard_dest(self, tmp_path, logger):
        target_dir = tmp_path / "Contacts" / "Alice"
        target_dir.mkdir(parents=True)
        dest_file = target_dir / "Bob.vcf"

        content1 = b"BEGIN:VCARD\r\nFN:Bob\r\nEND:VCARD\r\n"
        md5_1 = hashlib.md5(content1).digest()
        cand1 = wa.resolve_unique_vcard_dest(str(dest_file), md5_1, logger)
        assert cand1 == str(dest_file)
        dest_file.write_bytes(content1)

        # Same content again should return None (skip)
        cand_same = wa.resolve_unique_vcard_dest(str(dest_file), md5_1, logger)
        assert cand_same is None

        # Different content with same base name should get disambiguated name
        content2 = b"BEGIN:VCARD\r\nFN:Bob\r\nTEL:12345\r\nEND:VCARD\r\n"
        md5_2 = hashlib.md5(content2).digest()
        cand2 = wa.resolve_unique_vcard_dest(str(dest_file), md5_2, logger)
        assert cand2 == str(target_dir / "Bob_1.vcf")

    def test_process_vcard_rows_creation_and_routing(self, tmp_path, logger):
        archive_conn = arc.open_archive_db(str(tmp_path))

        vcard_content = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Doctor Smith\r\nTEL:+15551234\r\nEND:VCARD\r\n"
        )
        rows = [
            {
                "message_id": 101,
                "timestamp_ms": 1600000000000,
                "chat_row_id": "4412345",
                "chat_subject": None,
                "sender": "4412345",
                "key_from_me": 0,
                "contact_name": "Doctor Smith",
                "vcard_text": vcard_content,
            },
            {
                "message_id": 102,
                "timestamp_ms": 1600000010000,
                "chat_row_id": "999",
                "chat_subject": "Family Chat",
                "sender": "4412345",
                "key_from_me": 0,
                "contact_name": "Nurse Kelly",
                "vcard_text": "BEGIN:VCARD\r\nFN:Nurse Kelly\r\nEND:VCARD\r\n",
            },
        ]

        contacts = {"4412345": "Alice"}
        folder_index = {"4412345": "Alice (4412345)"}
        group_index = {"999": {"folder": "Family Chat", "subject": "Family Chat"}}

        stats, upd_idx, upd_grp = wa.process_vcard_rows(
            rows,
            contacts,
            {},
            folder_index,
            group_index,
            str(tmp_path),
            logger,
            False,
            archive_conn,
            None,
        )
        assert stats["copied"] == 2

        # Verify contact routing (under year / Received)
        contact_files = list((tmp_path / "Contacts").rglob("Doctor Smith.vcf"))
        assert len(contact_files) == 1
        contact_file = contact_files[0]
        assert contact_file.exists()
        assert contact_file.read_bytes() == vcard_content.encode("utf-8")
        assert int(os.path.getmtime(contact_file) * 1000) == 1600000000000

        # Verify group routing
        group_files = list((tmp_path / "Groups").rglob("*Nurse Kelly*.vcf"))
        assert len(group_files) == 1
        group_file = group_files[0]
        assert group_file.exists()

        # Check archive_copies table
        row1 = archive_conn.execute(
            "SELECT archive_path FROM archive_copies WHERE original_path = 'vcard:101'"
        ).fetchone()
        assert row1 is not None
        assert "Doctor Smith.vcf" in row1[0]

        # Repeat processing should be idempotent and skip existing
        stats_repeat, _, _ = wa.process_vcard_rows(
            rows,
            contacts,
            {},
            upd_idx,
            upd_grp,
            str(tmp_path),
            logger,
            False,
            archive_conn,
            None,
        )
        assert stats_repeat["copied"] == 0
        assert stats_repeat["skipped"] == 2

        archive_conn.close()

    def test_process_vcard_rows_rerun_does_not_add_suffixes(self, tmp_path, logger):
        # Two different vCards for the same contact name in the same chat and year:
        # the second one is stored as "<name>_1.vcf" and must be found again on re-runs.
        rows = [
            {
                "message_id": message_id,
                "timestamp_ms": 1600000000000 + message_id,
                "chat_row_id": "4412345",
                "chat_subject": None,
                "sender": "4412345",
                "key_from_me": 0,
                "contact_name": "Doctor Smith",
                "vcard_text": f"BEGIN:VCARD\r\nFN:Doctor Smith\r\nTEL:+1555{message_id}\r\nEND:VCARD\r\n",
            }
            for message_id in (201, 202)
        ]
        contacts = {"4412345": "Alice"}
        folder_index = {"4412345": "Alice (4412345)"}

        for expected_copied in (2, 0, 0):
            archive_conn = arc.open_archive_db(str(tmp_path))
            stats, folder_index, _ = wa.process_vcard_rows(
                rows,
                contacts,
                {},
                folder_index,
                {},
                str(tmp_path),
                logger,
                False,
                archive_conn,
                None,
            )
            archive_conn.commit()
            archive_conn.close()
            assert stats["copied"] == expected_copied

        names = sorted(p.name for p in (tmp_path / "Contacts").rglob("*.vcf"))
        assert names == ["Doctor Smith.vcf", "Doctor Smith_1.vcf"]
