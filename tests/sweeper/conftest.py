"""Simulated Android device harness and synthetic WhatsApp DB fixtures for wab-sweeper."""

from __future__ import annotations

import hashlib
import logging
import shlex
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from shared import adb


@dataclass
class FakeAndroidDevice:
    """Stateful in-memory/tempdir Android filesystem + ADB command simulator."""

    root_dir: Path
    serial: str = "SIM-DEV-001"
    sdk_level: int = 34
    md5_binary: str = "md5sum"
    free_kb: int = 20 * 1024 * 1024  # 20 GiB
    active_backup: bool = False
    wakelock_restricted: bool = False
    readonly_paths: set[str] = field(default_factory=set)
    disconnect_after_scripts: int | None = None
    _script_calls: int = 0
    _chmod_calls: list[tuple[str, str]] = field(default_factory=list)
    _touch_calls: list[tuple[str, str]] = field(default_factory=list)
    _rmdir_calls: list[str] = field(default_factory=list)
    _pushed_files: list[str] = field(default_factory=list)

    def _to_host(self, remote_path: str) -> Path:
        rel = remote_path.lstrip("/")
        return self.root_dir / Path(*rel.split("/"))

    def put_file(self, remote_path: str, content: bytes) -> None:
        host_path = self._to_host(remote_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(content)

    def exists(self, remote_path: str) -> bool:
        return self._to_host(remote_path).exists()

    def read_bytes(self, remote_path: str) -> bytes:
        return self._to_host(remote_path).read_bytes()

    def enumerate_files(self, media_root: str) -> list[tuple[str, int]]:
        base = self._to_host(media_root)
        if not base.exists():
            return []
        entries: list[tuple[str, int]] = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.root_dir).as_posix()
                entries.append(("/" + rel, p.stat().st_size))
        return entries

    def compute_md5(self, remote_path: str) -> bytes:
        host_path = self._to_host(remote_path)
        if not host_path.is_file():
            raise FileNotFoundError(remote_path)
        return hashlib.md5(host_path.read_bytes()).digest()

    def run_shell_script(self, script: str) -> subprocess.CompletedProcess:
        self._script_calls += 1
        if (
            self.disconnect_after_scripts is not None
            and self._script_calls > self.disconnect_after_scripts
        ):
            raise RuntimeError("Simulated USB disconnect mid-batch (device offline)")

        out_lines: list[str] = []
        lines = script.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("p="):
                raw_p = shlex.split(line[2:])[0]
                host_p = self._to_host(raw_p)
                if not host_p.exists():
                    out_lines.append(f"OK\t{raw_p}")
                elif raw_p in self.readonly_paths:
                    out_lines.append(f"FAIL\t{raw_p}")
                else:
                    host_p.unlink()
                    out_lines.append(f"OK\t{raw_p}")
                # Skip the if/else/fi block (8 lines after p=...)
                while i < len(lines) and lines[i].strip() != "fi":
                    i += 1
                # Also skip outer fi if nested
                if i + 1 < len(lines) and lines[i + 1].strip() == "fi":
                    i += 1
            elif line.startswith("rmdir -- "):
                target = shlex.split(line.split("2>/dev/null")[0][len("rmdir -- ") :])[0]
                self._rmdir_calls.append(target)
                host_d = self._to_host(target)
                if host_d.is_dir() and not any(host_d.iterdir()):
                    host_d.rmdir()
            elif line.startswith("mkdir -p -- "):
                target = shlex.split(line[len("mkdir -p -- ") :])[0]
                self._to_host(target).mkdir(parents=True, exist_ok=True)
            elif line.startswith("mv -- "):
                parts = shlex.split(line[len("mv -- ") :])
                src_p = self._to_host(parts[0])
                dst_p = self._to_host(parts[1])
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_p), str(dst_p))
            elif line.startswith("chmod "):
                parts = shlex.split(line)
                mode = parts[1]
                target = parts[-1]
                self._chmod_calls.append((mode, target))
            elif line.startswith("touch -d "):
                parts = shlex.split(line.split("2>/dev/null")[0])
                ts_val = parts[2]
                target = parts[-1]
                self._touch_calls.append((ts_val, target))
            i += 1

        return subprocess.CompletedProcess(
            args=["adb", "shell", "sh"],
            returncode=0,
            stdout=("\n".join(out_lines) + "\n").encode("utf-8"),
            stderr=b"",
        )

    def push(self, local_path: str, remote_path: str) -> subprocess.CompletedProcess:
        host_dst = self._to_host(remote_path)
        host_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, host_dst)
        self._pushed_files.append(remote_path)
        return subprocess.CompletedProcess(
            args=["adb", "push", local_path, remote_path],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )


def install_fake_device(monkeypatch: pytest.MonkeyPatch, device: FakeAndroidDevice) -> None:
    """Bind shared.adb primitives to the simulated Android device."""
    monkeypatch.setattr(adb, "check_adb", lambda logger: True)
    monkeypatch.setattr(adb, "resolve_connected_device", lambda pref, logger: device.serial)
    monkeypatch.setattr(adb, "get_android_sdk_level", lambda serial, logger: device.sdk_level)
    monkeypatch.setattr(adb, "probe_md5_binary", lambda logger: device.md5_binary)
    monkeypatch.setattr(
        adb,
        "probe_md5_binary_for_device",
        lambda device_serial, logger: device.md5_binary,
    )
    monkeypatch.setattr(
        adb,
        "enumerate_remote_files",
        lambda root, logger: device.enumerate_files(root),
    )
    monkeypatch.setattr(
        adb,
        "enumerate_remote_files_for_device",
        lambda root, device_serial, logger: device.enumerate_files(root),
    )
    monkeypatch.setattr(
        adb,
        "_remote_md5",
        lambda rp, binary, logger: device.compute_md5(rp),
    )
    monkeypatch.setattr(
        adb,
        "remote_md5_for_device",
        lambda rp, binary, device_serial, logger: device.compute_md5(rp),
    )
    monkeypatch.setattr(
        adb,
        "get_remote_free_kb",
        lambda serial, rp, logger: device.free_kb,
    )
    monkeypatch.setattr(
        adb,
        "check_active_whatsapp_backup",
        lambda serial, pkg, logger: device.active_backup,
    )

    def _wake_lock(serial: str | None, enabled: bool, logger: logging.Logger) -> bool:
        if device.wakelock_restricted:
            logger.debug("Simulated OEM wake-lock restriction")
            return False
        return True

    monkeypatch.setattr(adb, "set_usb_wake_lock", _wake_lock)
    monkeypatch.setattr(
        adb,
        "run_adb_shell_script",
        lambda serial, script, logger: device.run_shell_script(script),
    )
    monkeypatch.setattr(
        adb,
        "push_file",
        lambda serial, local_p, remote_p, logger: device.push(local_p, remote_p),
    )


def create_synthetic_archive(
    archive_dir: Path,
    *,
    contacts: list[tuple[str, str, str]],
    groups: list[tuple[str, str, str]],
    jids: list[tuple[int, str, str]],
    chats: list[tuple[int, int, str | None]],
    messages: list[tuple[int, int, int, str, int, int, int, int]],
    media_rows: list[tuple[int, int, str, str]],
    archive_files: list[dict[str, Any]],
    jid_map_rows: list[tuple[int, int]] | None = None,
    preserved_rows: list[tuple[str, str, str, str]] | None = None,
) -> None:
    """Create synthetic .wa_media_archiver.db, msgstore.db, and optional preserved_messages.db."""
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 1. .wa_media_archiver.db
    arc_conn = sqlite3.connect(archive_dir / ".wa_media_archiver.db")
    arc_conn.executescript(
        """
        CREATE TABLE contacts (
            number TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE groups (
            chat_row_id TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE files (
            original_path TEXT PRIMARY KEY,
            md5 BLOB NOT NULL,
            size INTEGER
        );
        CREATE TABLE archive_copies (
            original_path TEXT NOT NULL REFERENCES files(original_path),
            archive_path TEXT NOT NULL,
            is_hardlink INTEGER NOT NULL DEFAULT 0,
            timestamp_ms INTEGER,
            exif_stamped INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (original_path, archive_path)
        );
        CREATE TABLE adb_pull_state (
            remote_path TEXT NOT NULL,
            device_serial TEXT NOT NULL,
            status TEXT NOT NULL,
            pulled_at TEXT,
            PRIMARY KEY (remote_path, device_serial)
        );
        """
    )
    arc_conn.executemany("INSERT INTO contacts VALUES (?, ?, ?)", contacts)
    arc_conn.executemany("INSERT INTO groups VALUES (?, ?, ?)", groups)
    for item in archive_files:
        orig_path = item["original_path"]
        arc_rel = item["archive_path"]
        raw_bytes: bytes = item["raw_bytes"]
        disk_bytes: bytes = item.get("disk_bytes", raw_bytes)
        write_disk: bool = item.get("write_disk", True)
        exif_stamped: int = item.get("exif_stamped", 0)
        ts_ms: int = item.get("timestamp_ms", 1700000000000)

        md5_bytes = hashlib.md5(raw_bytes).digest()
        arc_conn.execute(
            "INSERT OR REPLACE INTO files (original_path, md5, size) VALUES (?, ?, ?)",
            (orig_path, md5_bytes, len(raw_bytes)),
        )
        arc_conn.execute(
            "INSERT OR REPLACE INTO archive_copies "
            "(original_path, archive_path, is_hardlink, timestamp_ms, exif_stamped) "
            "VALUES (?, ?, 0, ?, ?)",
            (orig_path, arc_rel, ts_ms, exif_stamped),
        )
        if write_disk:
            disk_path = archive_dir / Path(*arc_rel.split("/"))
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_bytes(disk_bytes)

    arc_conn.commit()
    arc_conn.close()

    # 2. msgstore.db (strictly replicating real NOT NULL constraints)
    msg_conn = sqlite3.connect(archive_dir / "msgstore.db")
    msg_conn.executescript(
        """
        CREATE TABLE jid (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            server TEXT NOT NULL,
            raw_string TEXT NOT NULL
        );
        CREATE TABLE jid_map (
            lid_row_id INTEGER PRIMARY KEY,
            jid_row_id INTEGER NOT NULL
        );
        CREATE TABLE chat (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            jid_row_id INTEGER NOT NULL,
            subject TEXT
        );
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            message_type INTEGER NOT NULL,
            starred INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE message_media (
            message_row_id INTEGER PRIMARY KEY,
            chat_row_id INTEGER NOT NULL,
            file_path TEXT,
            mime_type TEXT
        );
        """
    )
    for jid_id, user, server in jids:
        msg_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (?, ?, ?, ?)",
            (jid_id, user, server, f"{user}@{server}"),
        )
    if jid_map_rows:
        msg_conn.executemany("INSERT INTO jid_map VALUES (?, ?)", jid_map_rows)
    msg_conn.executemany("INSERT INTO chat (_id, jid_row_id, subject) VALUES (?, ?, ?)", chats)
    msg_conn.executemany(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, starred) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        messages,
    )
    msg_conn.executemany(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path, mime_type) "
        "VALUES (?, ?, ?, ?)",
        media_rows,
    )
    msg_conn.commit()
    msg_conn.close()

    # 3. Optional preserved_messages.db
    if preserved_rows:
        pres_conn = sqlite3.connect(archive_dir / "preserved_messages.db")
        pres_conn.executescript(
            """
            CREATE TABLE preserved_messages (
                chat_id TEXT NOT NULL,
                stanza_id TEXT NOT NULL,
                original_path TEXT,
                archive_path TEXT,
                PRIMARY KEY (chat_id, stanza_id)
            );
            """
        )
        pres_conn.executemany(
            "INSERT INTO preserved_messages VALUES (?, ?, ?, ?)",
            preserved_rows,
        )
        pres_conn.commit()
        pres_conn.close()
