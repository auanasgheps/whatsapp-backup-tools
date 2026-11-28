"""Tests for wab_archiver.ping (--ping-url). No real network access."""

import http.client
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
import wab_archiver.main as wa
from wab_archiver import ping

URL = "http://kuma.example.com:3001/api/push/TOKEN"


class TestBuildUrl:
    def test_success(self):
        assert ping.build_url(URL, 0) == f"{URL}?status=up&msg=OK"

    def test_failure_carries_exit_code(self):
        assert ping.build_url(URL, 2) == f"{URL}?status=down&msg=wab-archiver+exit+2"

    def test_existing_query_kept(self):
        assert ping.build_url(f"{URL}?ping=", 0) == f"{URL}?ping=&status=up&msg=OK"

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "kuma.example.com/push", ""])
    def test_non_http_scheme_rejected(self, url):
        with pytest.raises(ValueError):
            ping.build_url(url, 0)


class TestExitCode:
    @pytest.mark.parametrize(
        "exc,code",
        [
            (None, 0),
            (SystemExit(None), 0),
            (SystemExit(0), 0),
            (SystemExit(2), 2),
            (SystemExit("message"), 1),
            (RuntimeError("boom"), 1),
            (KeyboardInterrupt(), 1),
        ],
    )
    def test_mapping(self, exc, code):
        assert ping.exit_code(exc) == code


class TestSend:
    def test_ok(self, logger):
        resp = MagicMock(status=200)
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp) as op:
            assert ping.send(URL, 0, logger) is True
        assert op.call_args.args[0] == f"{URL}?status=up&msg=OK"
        assert op.call_args.kwargs["timeout"] == ping.TIMEOUT

    @pytest.mark.parametrize(
        "error",
        [
            OSError("unreachable"),
            urllib.error.URLError("no route"),
            TimeoutError("timed out"),
            http.client.BadStatusLine("garbage"),
        ],
    )
    def test_network_error_is_logged_not_raised(self, logger, error):
        with patch("urllib.request.urlopen", side_effect=error):
            assert ping.send(URL, 0, logger) is False

    def test_bad_url_is_logged_not_raised(self, logger):
        with patch("urllib.request.urlopen") as op:
            assert ping.send("ftp://example.com/push", 0, logger) is False
        op.assert_not_called()


def _argv(tmp_path, *extra):
    return ["wab-archiver", "archive", "-o", str(tmp_path), "--wa-root", str(tmp_path), *extra]


def _run_main(tmp_path, *extra, run_effect=None):
    with (
        patch("sys.argv", _argv(tmp_path, *extra)),
        patch.object(wa, "run_forward_mode", side_effect=run_effect),
        patch.object(ping, "send") as send,
    ):
        wa.main()
    return send


class TestMainIntegration:
    def test_ping_up_after_success(self, tmp_path):
        send = _run_main(tmp_path, "--ping-url", URL)
        assert send.call_args.args[:2] == (URL, 0)

    def test_ping_down_on_exit_code_and_exit_propagates(self, tmp_path):
        with (
            patch("sys.argv", _argv(tmp_path, "--ping-url", URL)),
            patch.object(wa, "run_forward_mode", side_effect=SystemExit(2)),
            patch.object(ping, "send") as send,
            pytest.raises(SystemExit) as exc,
        ):
            wa.main()
        assert exc.value.code == 2
        assert send.call_args.args[:2] == (URL, 2)

    def test_ping_down_on_crash_and_error_propagates(self, tmp_path):
        with (
            patch("sys.argv", _argv(tmp_path, "--ping-url", URL)),
            patch.object(wa, "run_forward_mode", side_effect=RuntimeError("boom")),
            patch.object(ping, "send") as send,
            pytest.raises(RuntimeError),
        ):
            wa.main()
        assert send.call_args.args[:2] == (URL, 1)

    def test_no_ping_without_url(self, tmp_path):
        send = _run_main(tmp_path)
        send.assert_not_called()

    def test_no_ping_on_dry_run(self, tmp_path):
        send = _run_main(tmp_path, "--ping-url", URL, "--dry-run")
        send.assert_not_called()

    def test_ping_url_from_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'output = "{tmp_path.as_posix()}"\n'
            f'wa_root = "{tmp_path.as_posix()}"\n'
            f'ping_url = "{URL}"\n',
            encoding="utf-8",
        )
        with (
            patch("sys.argv", ["wab-archiver", "archive", "--config", str(cfg)]),
            patch.object(wa, "run_forward_mode"),
            patch.object(ping, "send") as send,
        ):
            wa.main()
        assert send.call_args.args[:2] == (URL, 0)
