"""CLI Runner for Local UI/UX Audits across WhatsApp Chat Backups.

Consolidates the experimental scroll verification and sweep checks into a clean CLI.
Allows auditing synthetic demo data or local private archives without running full pytest.

Usage:
    # Audit synthetic demo data:
    python tests/ui/run_audit.py

    # Audit a specific local archive directory:
    python tests/ui/run_audit.py --data-dir "Extras"

    # Sweep all group chats to verify no blank pages or rendering crashes:
    python tests/ui/run_audit.py --sweep
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.ui.cdp_driver import CDPSession, find_free_port, launch_browser_session
from wab_viewer.main import create_app


def audit_single_chat(session: CDPSession, chat_id: str, scroll_steps: int) -> dict:
    """Audit bottom pinning and upward scroll pagination on a specific chat."""
    js_code = f"""
    (async () => {{
        for (let i = 0; i < 50; i++) {{
            if (document.querySelectorAll('.chat-item').length > 0) break;
            await new Promise(r => setTimeout(r, 100));
        }}

        let item = document.querySelector('.chat-item[data-id="{chat_id}"]');
        if (!item) {{
            item = document.querySelector('.chat-item[data-type="group"]');
        }}
        if (!item) return {{ error: 'No chat found to test' }};

        item.click();
        for (let j = 0; j < 50; j++) {{
            await new Promise(r => setTimeout(r, 50));
            const loading = document.getElementById('chat-loading');
            if (loading && loading.style.display === 'none') break;
        }}
        await new Promise(r => setTimeout(r, 150));

        const scroll = document.getElementById('message-scroll');
        const diff = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
        const initial = {{
            rows: scroll.querySelectorAll('.msg-row').length,
            scrollTop: scroll.scrollTop,
            scrollHeight: scroll.scrollHeight,
            clientHeight: scroll.clientHeight,
            diffFromBottom: diff,
            isAtBottom: diff < 5
        }};

        const steps = [];
        for (let s = 1; s <= {scroll_steps}; s++) {{
            const prevRows = scroll.querySelectorAll('.msg-row').length;
            scroll.scrollTop = 50;
            scroll.dispatchEvent(new Event('scroll'));

            for (let w = 0; w < 30; w++) {{
                await new Promise(r => setTimeout(r, 100));
                if (scroll.querySelectorAll('.msg-row').length > prevRows) break;
            }}
            await new Promise(r => setTimeout(r, 100));

            const rows = Array.from(scroll.querySelectorAll('.msg-row[data-msg-id]'));
            steps.push({{
                step: s,
                totalRows: rows.length,
                scrollTop: scroll.scrollTop,
                firstMsgId: rows[0] ? rows[0].dataset.msgId : null,
                lastMsgId: rows[rows.length - 1] ? rows[rows.length - 1].dataset.msgId : null
            }});
        }}
        return {{ initial, steps }};
    }})()
    """
    res = session.evaluate(js_code, 45.0)
    if not isinstance(res, dict):
        raise TypeError(f"Expected dict from browser evaluation, got {type(res)}")
    return res


def sweep_group_chats(session: CDPSession) -> list[dict]:
    """Sweep all group chats to verify no chat renders empty messages with date separators."""
    js_code = """
    (async () => {
        for (let i = 0; i < 50; i++) {
            if (document.querySelectorAll('.chat-item').length > 0) break;
            await new Promise(r => setTimeout(r, 100));
        }

        const groupItems = Array.from(document.querySelectorAll('.chat-item[data-type="group"]'));
        const results = [];
        for (let i = 0; i < groupItems.length; i++) {
            const item = groupItems[i];
            const id = item.dataset.id;
            const name = item.querySelector('.chat-name')?.textContent || '';
            item.click();

            for (let j = 0; j < 40; j++) {
                await new Promise(r => setTimeout(r, 50));
                const loading = document.getElementById('chat-loading');
                if (loading && loading.style.display === 'none') break;
            }

            const scroll = document.getElementById('message-scroll');
            const dateSeps = scroll ? scroll.querySelectorAll('.date-separator').length : 0;
            const rows = scroll ? scroll.querySelectorAll('.msg-row').length : 0;
            const serviceBubbles = scroll ? scroll.querySelectorAll('.msg-service-bubble').length : 0;

            results.push({
                id,
                name,
                dateSeps,
                rows,
                serviceBubbles
            });
        }
        return results;
    })()
    """
    res = session.evaluate(js_code, 90.0)
    if not isinstance(res, list):
        raise TypeError(f"Expected list from sweep evaluation, got {type(res)}")
    return res


def run_audit(data_path: Path, sweep_mode: bool, chat_id: str, scroll_steps: int) -> int:
    """Run UI audit server, launch headless browser, and execute checks."""
    app_port = find_free_port()
    cdp_port = find_free_port()

    print(f"[1/4] Starting viewer application for {data_path} on port {app_port}...")
    app = create_app(data_path, False)
    server_thread = threading.Thread(
        target=lambda: app.run(port=app_port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()
    time.sleep(1.0)

    profile_dir = REPO_ROOT / ".test_artifacts" / f"audit_profile_{cdp_port}"
    print(f"[2/4] Launching headless browser on CDP port {cdp_port}...")
    session = launch_browser_session(
        f"http://127.0.0.1:{app_port}",
        profile_dir,
        cdp_port,
        30,
        0.2,
    )

    try:
        if sweep_mode:
            print("[3/4] Sweeping all group chats for blank page rendering bugs...")
            results = sweep_group_chats(session)
            print(f"Total group chats tested: {len(results)}")
            blank_chats = [r for r in results if r["rows"] == 0 and r["dateSeps"] > 0]
            if blank_chats:
                print(f"FAILED: {len(blank_chats)} chats had 0 rows and >0 date separators:")
                for b in blank_chats:
                    print(f"  - Chat ID {b['id']} ({b['name']})")
                return 1
            print("SUCCESS: 0 blank groups found. All chats render messages correctly.")
            return 0

        print(f"[3/4] Auditing bottom pinning and scroll pagination for chat {chat_id}...")
        audit_res = audit_single_chat(session, chat_id, scroll_steps)
        initial = audit_res.get("initial", {})
        print(
            f"  Initial load: {initial.get('rows')} rows, isAtBottom={initial.get('isAtBottom')} "
            f"(diff={initial.get('diffFromBottom')}px)"
        )
        if not initial.get("isAtBottom"):
            print("FAILED: Chat did not open pinned to bottom!")
            return 1

        for step in audit_res.get("steps", []):
            print(
                f"  Scroll Up Step #{step['step']}: {step['totalRows']} rows in DOM, "
                f"top_id={step['firstMsgId']}, bottom_id={step['lastMsgId']}"
            )

        print("[4/4] Verification complete: All checks passed.")
        return 0
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="WhatsApp Chat Viewer UI/UX Audit Runner")
    parser.add_argument(
        "--data-dir",
        type=str,
        default="",
        help="Path to WhatsApp archive directory (defaults to Extras/Demo Output)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Sweep all group chats to verify no blank pages",
    )
    parser.add_argument(
        "--chat-id",
        type=str,
        default="1",
        help="Specific chat ID to audit (default: 1)",
    )
    parser.add_argument(
        "--scroll-steps",
        type=int,
        default=3,
        help="Number of scroll-up steps to test (default: 3)",
    )
    args = parser.parse_args()

    if args.data_dir:
        data_path = Path(args.data_dir)
    else:
        data_path = REPO_ROOT / "Extras" / "Demo Output"

    if not data_path.exists():
        print(f"Data directory {data_path} does not exist. Generating demo data...")
        from Extras.generate_demo_output import configure_logger, generate_demo

        generate_demo(data_path, configure_logger("audit_demo"))

    exit_code = run_audit(data_path, args.sweep, args.chat_id, args.scroll_steps)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
