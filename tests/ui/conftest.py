"""Pytest configuration and fixtures for local UI/UX browser testing."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import urllib.request
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wab_viewer.main import create_app

from .cdp_driver import CDPSession, find_free_port, launch_browser_session

# Locate generate_demo_output.py from Extras
_DEMO_SCRIPT = REPO_ROOT / "Extras" / "generate_demo_output.py"

_spec = importlib.util.spec_from_file_location("generate_demo_output", _DEMO_SCRIPT)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load demo generator from {_DEMO_SCRIPT}")
_demo_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_demo_mod)
generate_demo = _demo_mod.generate_demo
configure_logger = _demo_mod.configure_logger


@pytest.fixture(scope="session")
def ui_demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate a clean, privacy-safe synthetic WhatsApp archive for UI tests."""
    temp_dir = tmp_path_factory.mktemp("ui_demo_data")
    out_dir = temp_dir / "Demo Output"
    logger = configure_logger("ui_test_demo")
    generate_demo(out_dir, logger)
    return out_dir


@pytest.fixture(scope="session")
def ui_server_url(ui_demo_dir: Path) -> Generator[str, None, None]:
    """Start the chat viewer Flask application on an ephemeral port."""
    port = find_free_port()
    app = create_app(ui_demo_dir, rescan=True)
    app.config["TESTING"] = True

    server_thread = threading.Thread(
        target=lambda: app.run(port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for server readiness
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base_url}/api/chats", timeout=1.0) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)

    if not ready:
        raise RuntimeError(f"Chat viewer server failed to become ready at {base_url}")

    yield base_url


@pytest.fixture(scope="session")
def browser(
    ui_server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Generator[CDPSession, None, None]:
    """Launch a headless Edge/Chrome browser connected via CDP for the test session."""
    cdp_port = find_free_port()
    profile_dir = tmp_path_factory.mktemp("browser_profile") / f"edge_{cdp_port}"
    session = launch_browser_session(
        ui_server_url,
        profile_dir,
        cdp_port,
        30,
        0.2,
    )
    yield session
    session.close()


@pytest.fixture(autouse=True)
def reset_ui_state(browser: CDPSession) -> Generator[None, None, None]:
    """Ensure modals and panels are closed after each test."""
    yield
    browser.evaluate(
        """
        (() => {
            const setClose = document.getElementById('settings-close');
            if (setClose) setClose.click();
            const galClose = document.getElementById('media-gallery-close');
            if (galClose) galClose.click();
            const infoClose = document.getElementById('chat-info-close');
            if (infoClose) infoClose.click();
        })()
        """,
        3.0,
    )


@pytest.fixture(scope="session")
def screenshot_dir() -> Path:
    """Directory for saving UI/UX visual validation screenshot artifacts."""
    out_dir = REPO_ROOT / ".test_artifacts" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir
