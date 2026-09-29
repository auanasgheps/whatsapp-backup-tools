"""Chrome DevTools Protocol (CDP) driver using standard-library sockets.

Provides headless browser control, DOM evaluation, and visual screenshot capture
without requiring external frameworks like Playwright or Selenium.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


class SimpleWS:
    """Minimal RFC 6455 WebSocket client using pure standard-library sockets."""

    def __init__(self, host: str, port: int, path: str) -> None:
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.connect((host, port))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.s.sendall(req.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(1024)
        status_line = resp.split(b"\r\n")[0]
        if b"101" not in status_line:
            self.s.close()
            raise RuntimeError(
                f"WebSocket handshake failed: {status_line.decode('utf-8', errors='ignore')}"
            )

    def send_json(self, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        frame = bytearray([0x81])
        length = len(payload)
        mask_key = os.urandom(4)
        if length <= 125:
            frame.append(0x80 | length)
        elif length <= 65535:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))
        frame.extend(mask_key)
        frame.extend(bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload)))
        self.s.sendall(frame)

    def recv_json(self, timeout: float) -> dict:
        self.s.settimeout(timeout)
        b1 = self.s.recv(1)[0]
        b2 = self.s.recv(1)[0]
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(self.s.recv(2), "big")
        elif length == 127:
            length = int.from_bytes(self.s.recv(8), "big")
        if masked:
            mask = self.s.recv(4)
        payload = b""
        while len(payload) < length:
            chunk = self.s.recv(min(4096, length - len(payload)))
            if not chunk:
                break
            payload += chunk
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return json.loads(payload.decode("utf-8", errors="ignore"))

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


def find_browser_executable() -> str:
    """Locate Microsoft Edge or Google Chrome executable."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if Path(p).is_file():
            return p
    raise FileNotFoundError("Could not find Microsoft Edge or Google Chrome executable.")


def find_free_port() -> int:
    """Find an available ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDPSession:
    """Manages an active CDP browser session."""

    def __init__(
        self,
        ws: SimpleWS,
        proc: subprocess.Popen,
        user_data_dir: Path,
        cdp_port: int,
    ) -> None:
        self.ws = ws
        self.proc = proc
        self.user_data_dir = user_data_dir
        self.cdp_port = cdp_port
        self._next_id = 100

    def evaluate(self, expr: str, timeout: float) -> dict | list | str | int | float | bool | None:
        """Evaluate an async or sync JavaScript expression and await the result."""
        req_id = self._next_id
        self._next_id += 1

        self.ws.send_json(
            {
                "id": req_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expr,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            }
        )

        start = time.time()
        while time.time() - start < timeout:
            remaining = max(0.5, timeout - (time.time() - start))
            try:
                msg = self.ws.recv_json(min(remaining, 2.0))
            except TimeoutError:
                continue
            if msg.get("id") == req_id:
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(f"Browser evaluation error: {res['exceptionDetails']}")
                return res.get("result", {}).get("value")
        raise TimeoutError(
            f"Evaluation timed out after {timeout} seconds waiting for CDP response."
        )

    def capture_screenshot(self, output_path: Path, timeout: float) -> Path:
        """Capture viewport screenshot and write image to disk."""
        req_id = self._next_id
        self._next_id += 1

        self.ws.send_json(
            {
                "id": req_id,
                "method": "Page.captureScreenshot",
                "params": {"format": "png"},
            }
        )

        start = time.time()
        while time.time() - start < timeout:
            remaining = max(0.5, timeout - (time.time() - start))
            try:
                msg = self.ws.recv_json(min(remaining, 2.0))
            except TimeoutError:
                continue
            if msg.get("id") == req_id:
                raw_base64 = msg.get("result", {}).get("data", "")
                if not raw_base64:
                    raise RuntimeError("Screenshot capture returned empty image data.")
                image_bytes = base64.b64decode(raw_base64)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(image_bytes)
                return output_path
        raise TimeoutError(
            f"Screenshot capture timed out after {timeout} seconds waiting for CDP response."
        )

    def close(self) -> None:
        """Close WebSocket, terminate browser process, and delete profile directory."""
        self.ws.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.user_data_dir, ignore_errors=True)


def launch_browser_session(
    target_url: str,
    user_data_dir: Path,
    cdp_port: int,
    max_connection_retries: int,
    retry_delay_seconds: float,
) -> CDPSession:
    """Launch headless browser and connect via CDP WebSocket."""
    browser_exe = find_browser_executable()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser_exe,
        "--headless=new",
        f"--user-data-dir={user_data_dir.resolve()}",
        f"--remote-debugging-port={cdp_port}",
        "--disable-gpu",
        target_url,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    tabs = None
    last_err: Exception | None = None
    for _ in range(max_connection_retries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=2.0) as r:
                tabs = json.loads(r.read().decode("utf-8"))
                if tabs:
                    break
        except Exception as exc:
            last_err = exc
            time.sleep(retry_delay_seconds)

    if not tabs:
        proc.terminate()
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to connect to browser CDP JSON endpoint: {last_err}")

    target_tab = tabs[0]
    for tab in tabs:
        if target_url in tab.get("url", ""):
            target_tab = tab
            break

    ws_url = target_tab.get("webSocketDebuggerUrl")
    if not ws_url:
        proc.terminate()
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise RuntimeError(f"No webSocketDebuggerUrl found in tab: {target_tab}")

    ws_path = ws_url.split(f"{cdp_port}")[1]
    time.sleep(0.5)

    ws = SimpleWS("127.0.0.1", cdp_port, ws_path)
    session = CDPSession(ws, proc, user_data_dir, cdp_port)
    session.ws.send_json({"id": 1, "method": "Runtime.enable"})
    session.ws.send_json({"id": 2, "method": "Page.enable"})
    return session
