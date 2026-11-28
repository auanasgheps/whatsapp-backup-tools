"""
Report the outcome of an archive run to a push URL (``--ping-url``).

Meant for scheduled runs: an Uptime Kuma push URL (or any healthchecks-style
endpoint) turns a missing or ``down`` ping into an alert when the archiver
fails or never completes. Only the status and the exit code are sent, never
archive data. A failed ping is logged and never changes the exit code.
"""

import http.client
import logging
import urllib.parse
import urllib.request

TIMEOUT = 15


def build_url(url: str, code: int) -> str:
    """Append ``status`` and ``msg`` query parameters to *url*."""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {scheme!r}, expected http or https")
    status = "up" if code == 0 else "down"
    msg = "OK" if code == 0 else f"wab-archiver exit {code}"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urllib.parse.urlencode({'status': status, 'msg': msg})}"


def send(url: str, code: int, logger: logging.Logger) -> bool:
    """GET the push URL for exit *code*. Returns False (and logs) on any failure."""
    try:
        target = build_url(url, code)
        with urllib.request.urlopen(target, timeout=TIMEOUT) as r:
            logger.info(f"Ping {'up' if code == 0 else 'down'} sent (HTTP {r.status}).")
        return True
    except (OSError, ValueError, http.client.HTTPException) as e:
        logger.warning(f"Ping to the --ping-url endpoint failed: {e}")
        return False


def exit_code(exc: BaseException | None) -> int:
    """Exit code a run ends with: 0, a SystemExit code, or 1 for any other error."""
    if exc is None:
        return 0
    if isinstance(exc, SystemExit):
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    return 1
