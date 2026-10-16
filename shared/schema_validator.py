from __future__ import annotations

import platform as sys_platform
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


def get_app_version() -> str:
    """Return the installed version of whatsapp-backup-tools or pyproject.toml."""
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("whatsapp-backup-tools")
    except Exception:
        import tomllib

        root = Path(__file__).resolve().parents[1]
        pyproject_path = root / "pyproject.toml"
        if pyproject_path.is_file():
            return tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]["version"]
        return "unknown"


# Required schema for WhatsApp Archiver (needs media tables for archiving)
_ANDROID_REQUIRED_TABLES: set[str] = {"message", "message_media", "chat", "jid", "jid_map"}

_ANDROID_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "message": {"_id", "timestamp", "sender_jid_row_id", "from_me"},
    "message_media": {
        "file_path",
        "mime_type",
        "chat_row_id",
        "message_row_id",
        "message_url",
        "media_name",
    },
    "chat": {"_id", "subject", "jid_row_id"},
    "jid": {"_id", "user"},
    "jid_map": {"lid_row_id", "jid_row_id"},
}

_IOS_REQUIRED_TABLES: set[str] = {"ZWAMESSAGE", "ZWACHATSESSION", "ZWAMEDIAITEM", "ZWAGROUPMEMBER"}

_IOS_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "ZWAMESSAGE": {
        "Z_PK",
        "ZMESSAGEDATE",
        "ZISFROMME",
        "ZCHATSESSION",
        "ZMEDIAITEM",
        "ZGROUPMEMBER",
        "ZPUSHNAME",
    },
    "ZWACHATSESSION": {
        "Z_PK",
        "ZCONTACTJID",
        "ZGROUPINFO",
        "ZPARTNERNAME",
        "ZCONTACTABID",
        "ZLASTMESSAGEDATE",
    },
    "ZWAMEDIAITEM": {
        "Z_PK",
        "ZMEDIALOCALPATH",
        "ZMEDIAURL",
        "ZTITLE",
    },
    "ZWAGROUPMEMBER": {
        "Z_PK",
        "ZMEMBERJID",
    },
}

# Required schema for WhatsApp Viewer (needs chat and message tables for browsing)
_VIEWER_ANDROID_REQUIRED_TABLES: set[str] = {"message", "chat", "jid"}

_VIEWER_ANDROID_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "message": {"_id", "chat_row_id", "from_me", "timestamp"},
    "chat": {"_id", "jid_row_id"},
    "jid": {"_id", "user"},
}

_VIEWER_IOS_REQUIRED_TABLES: set[str] = {"ZWAMESSAGE", "ZWACHATSESSION"}

_VIEWER_IOS_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "ZWAMESSAGE": {
        "Z_PK",
        "ZMESSAGEDATE",
        "ZISFROMME",
        "ZCHATSESSION",
    },
    "ZWACHATSESSION": {
        "Z_PK",
        "ZCONTACTJID",
    },
}


@dataclass(frozen=True)
class SchemaValidationResult:
    is_valid: bool
    platform: str
    is_legacy: bool
    missing_tables: list[str]
    missing_columns: dict[str, list[str]]
    db_fingerprint: str
    issue_url: str
    banner: str
    summary: str


def format_platform_name(platform: str) -> str:
    """Format platform identifier into standard display name ('iOS' or 'Android')."""
    return "iOS" if platform.lower() == "ios" else "Android"


def detect_legacy_android_database(cursor: sqlite3.Cursor, tables: set[str]) -> str | None:
    """Return indicator description if database matches a legacy Android WhatsApp schema."""
    if "messages" in tables and "message" not in tables:
        return "Found legacy 'messages' table instead of modern 'message' table (pre-2019 Android format)."
    if "chat_list" in tables and "chat" not in tables:
        return "Found legacy 'chat_list' table instead of modern 'chat' table (pre-2019 Android format)."
    if "message" in tables:
        cols = {row[1] for row in cursor.execute("PRAGMA table_info(message)").fetchall()}
        if "key_remote_jid" in cols and "chat_row_id" not in cols:
            return "Found legacy 'key_remote_jid' column in 'message' table without modern 'chat_row_id' foreign keys."
    return None


def detect_legacy_ios_database(cursor: sqlite3.Cursor, tables: set[str]) -> str | None:
    """Return indicator description if database matches a legacy iOS WhatsApp schema."""
    if "ZMESSAGE" in tables and "ZWAMESSAGE" not in tables:
        return "Found legacy 'ZMESSAGE' table instead of modern 'ZWAMESSAGE' table (pre-2014 iOS format)."
    if "ZWAMESSAGE" in tables and "ZWAMEDIAITEM" not in tables:
        cols = {row[1] for row in cursor.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
        if "ZMEDIALOCALPATH" in cols:
            return "Found unnormalized media path in 'ZWAMESSAGE' without 'ZWAMEDIAITEM' table (pre-2016 iOS format)."
    return None


def extract_database_fingerprint(cursor: sqlite3.Cursor, platform: str) -> str:
    """Extract SQLite and WhatsApp schema fingerprint metadata without private data."""
    parts: list[str] = []

    try:
        user_ver_row = cursor.execute("PRAGMA user_version").fetchone()
        user_ver = str(user_ver_row[0]) if user_ver_row else "unknown"
        parts.append(f"PRAGMA user_version={user_ver}")
    except sqlite3.Error:
        parts.append("PRAGMA user_version=error")

    try:
        sqlite_ver_row = cursor.execute("SELECT sqlite_version()").fetchone()
        sqlite_ver = str(sqlite_ver_row[0]) if sqlite_ver_row else "unknown"
        parts.append(f"SQLite={sqlite_ver}")
    except sqlite3.Error:
        pass

    if platform == "android":
        try:
            has_props = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='props'"
            ).fetchone()
            if has_props:
                prop_row = cursor.execute(
                    "SELECT value FROM props WHERE key = 'version' LIMIT 1"
                ).fetchone()
                if prop_row and prop_row[0]:
                    parts.append(f"props.version={prop_row[0]}")
        except sqlite3.Error:
            pass
    elif platform == "ios":
        try:
            has_meta = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Z_METADATA'"
            ).fetchone()
            if has_meta:
                meta_cols = {
                    r[1] for r in cursor.execute("PRAGMA table_info(Z_METADATA)").fetchall()
                }
                if "Z_VERSION" in meta_cols:
                    meta_row = cursor.execute("SELECT Z_VERSION FROM Z_METADATA LIMIT 1").fetchone()
                    if meta_row and meta_row[0] is not None:
                        parts.append(f"Z_METADATA.Z_VERSION={meta_row[0]}")
        except sqlite3.Error:
            pass

    return "; ".join(parts)


def build_github_issue_url(
    platform: str,
    missing_tables: list[str],
    missing_columns: dict[str, list[str]],
    fingerprint: str,
    tool_version: str,
) -> str:
    """Build a pre-filled GitHub issue URL with diagnostic context and no private data."""
    base_url = "https://github.com/auanasgheps/whatsapp-backup-tools/issues/new"
    platform_name = format_platform_name(platform)

    title = f"[Schema Incompatibility] WhatsApp {platform_name} schema change detected"

    discrepancy_lines: list[str] = []
    if missing_tables:
        discrepancy_lines.append(f"- Missing table(s): {', '.join(sorted(missing_tables))}")
    if missing_columns:
        for tbl in sorted(missing_columns.keys()):
            cols = ", ".join(sorted(missing_columns[tbl]))
            discrepancy_lines.append(f"- Missing column(s) in '{tbl}': {cols}")

    discrepancy_text = (
        "\n".join(discrepancy_lines) if discrepancy_lines else "- Unspecified schema discrepancy"
    )

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_info = f"{sys_platform.system()} {sys_platform.release()}"

    body = f"""### WhatsApp Database Schema Incompatibility Report

A schema discrepancy was detected when reading the WhatsApp database.

#### Environment
- **whatsapp-backup-tools version**: {tool_version}
- **Platform**: {platform_name}
- **Python**: {py_version} ({os_info})
- **Database Fingerprint**: {fingerprint}

#### Detected Discrepancies
{discrepancy_text}

#### Context
WhatsApp appears to have updated its internal database schema in a recent app release.
Please do NOT attach raw databases containing private messages, contact names, or phone numbers.
"""

    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"{base_url}?{query}"


def build_legacy_github_issue_url(
    platform: str,
    legacy_indicator: str,
    fingerprint: str,
    tool_version: str,
) -> str:
    """Build a pre-filled GitHub issue URL for legacy database feature requests."""
    base_url = "https://github.com/auanasgheps/whatsapp-backup-tools/issues/new"
    platform_name = format_platform_name(platform)
    title = f"[Feature Request / Legacy Schema] Support for older legacy WhatsApp {platform_name} database format"

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_info = f"{sys_platform.system()} {sys_platform.release()}"

    body = f"""### Legacy WhatsApp Database Support Request

An older legacy WhatsApp database schema was detected that is not currently supported by whatsapp-backup-tools.

#### Environment
- **whatsapp-backup-tools version**: {tool_version}
- **Platform**: {platform_name}
- **Python**: {py_version} ({os_info})
- **Database Fingerprint**: {fingerprint}

#### Legacy Indicator
- {legacy_indicator}

#### Context
This database was created by an older version of WhatsApp before the modern schema structure was introduced.
Please do NOT attach raw databases containing private messages, contact names, or phone numbers.
"""
    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"{base_url}?{query}"


def format_schema_banner(
    platform: str,
    missing_tables: list[str],
    missing_columns: dict[str, list[str]],
    issue_url: str,
) -> str:
    """Format a clear, high-visibility terminal banner with upgrade instructions and issue link."""
    platform_name = format_platform_name(platform)
    lines: list[str] = [
        "=" * 80,
        "⚠️  WHATSAPP DATABASE SCHEMA INCOMPATIBILITY DETECTED",
        "=" * 80,
        f"The {platform_name} WhatsApp database does not match the structure expected by this tool.",
    ]

    if missing_tables:
        lines.append(f"  • Missing table(s): {sorted(missing_tables)}")
    if missing_columns:
        for tbl in sorted(missing_columns.keys()):
            lines.append(f"  • Missing column(s) in '{tbl}': {sorted(missing_columns[tbl])}")

    lines.extend(
        [
            "",
            "WhatsApp frequently updates its database format in new app releases.",
            "",
            "Recommended actions:",
            "  1. Check for updates to whatsapp-backup-tools:",
            "     pip install --upgrade whatsapp-backup-tools",
            "",
            "  2. If you are already running the latest version, please report this issue:",
            f"     {issue_url}",
            "=" * 80,
        ]
    )
    return "\n".join(lines)


def format_legacy_banner(
    platform: str,
    legacy_indicator: str,
    issue_url: str,
) -> str:
    """Format a clear terminal banner for legacy, unsupported WhatsApp databases."""
    platform_name = format_platform_name(platform)
    lines: list[str] = [
        "=" * 80,
        "⚠️  LEGACY OR UNSUPPORTED WHATSAPP DATABASE DETECTED",
        "=" * 80,
        f"The {platform_name} WhatsApp database appears to be from an older, legacy version",
        "of WhatsApp created before the database format supported by this tool.",
        "",
        f"  • Indicator: {legacy_indicator}",
        "",
        "This tool supports modern WhatsApp backup databases (Android 2019+ and iOS CoreData).",
        "Older database schemas are not currently supported.",
        "",
        "If you would like to request support for this legacy database format, please open",
        "a feature request on GitHub:",
        f"  {issue_url}",
        "=" * 80,
    ]
    return "\n".join(lines)


def _validate_schema_against_spec(
    cursor: sqlite3.Cursor,
    platform: str,
    tool_version: str,
    required_tables: set[str],
    required_columns: dict[str, set[str]],
) -> SchemaValidationResult:
    tables = {
        row[0]
        for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing_tables = sorted(required_tables - tables)

    missing_columns: dict[str, list[str]] = {}
    for table, required_cols in required_columns.items():
        if table in tables:
            actual_cols = {
                row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
            }
            diff = required_cols - actual_cols
            if diff:
                missing_columns[table] = sorted(diff)

    is_valid = not missing_tables and not missing_columns

    if is_valid:
        return SchemaValidationResult(
            is_valid=True,
            platform=platform,
            is_legacy=False,
            missing_tables=[],
            missing_columns={},
            db_fingerprint="",
            issue_url="",
            banner="",
            summary="",
        )

    fingerprint = extract_database_fingerprint(cursor, platform)
    platform_name = format_platform_name(platform)

    # Check for older, legacy WhatsApp database indicators
    legacy_indicator = None
    if platform == "android":
        legacy_indicator = detect_legacy_android_database(cursor, tables)
    elif platform == "ios":
        legacy_indicator = detect_legacy_ios_database(cursor, tables)

    if legacy_indicator is not None:
        issue_url = build_legacy_github_issue_url(
            platform, legacy_indicator, fingerprint, tool_version
        )
        banner = format_legacy_banner(platform, legacy_indicator, issue_url)
        summary = f"Legacy WhatsApp {platform_name} database detected ({legacy_indicator})"
        return SchemaValidationResult(
            is_valid=False,
            platform=platform,
            is_legacy=True,
            missing_tables=missing_tables,
            missing_columns=missing_columns,
            db_fingerprint=fingerprint,
            issue_url=issue_url,
            banner=banner,
            summary=summary,
        )

    # Newer / unexpected schema change
    issue_url = build_github_issue_url(
        platform, missing_tables, missing_columns, fingerprint, tool_version
    )
    banner = format_schema_banner(platform, missing_tables, missing_columns, issue_url)

    summary_parts: list[str] = []
    if missing_tables:
        summary_parts.append(f"missing tables: {missing_tables}")
    if missing_columns:
        summary_parts.append(f"missing columns: {missing_columns}")
    summary = f"WhatsApp {platform_name} schema validation failed — {'; '.join(summary_parts)}"

    return SchemaValidationResult(
        is_valid=False,
        platform=platform,
        is_legacy=False,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        db_fingerprint=fingerprint,
        issue_url=issue_url,
        banner=banner,
        summary=summary,
    )


def validate_archiver_schema(
    cursor: sqlite3.Cursor,
    platform: str,
    tool_version: str,
) -> SchemaValidationResult:
    """Validate WhatsApp database against required archiver schema."""
    if platform == "android":
        return _validate_schema_against_spec(
            cursor, platform, tool_version, _ANDROID_REQUIRED_TABLES, _ANDROID_REQUIRED_COLUMNS
        )
    if platform == "ios":
        return _validate_schema_against_spec(
            cursor, platform, tool_version, _IOS_REQUIRED_TABLES, _IOS_REQUIRED_COLUMNS
        )
    raise ValueError(f"Unknown platform '{platform}', expected 'android' or 'ios'")


def validate_viewer_schema(
    cursor: sqlite3.Cursor,
    platform: str,
    tool_version: str,
) -> SchemaValidationResult:
    """Validate WhatsApp database against required viewer schema."""
    if platform == "android":
        return _validate_schema_against_spec(
            cursor,
            platform,
            tool_version,
            _VIEWER_ANDROID_REQUIRED_TABLES,
            _VIEWER_ANDROID_REQUIRED_COLUMNS,
        )
    if platform == "ios":
        return _validate_schema_against_spec(
            cursor,
            platform,
            tool_version,
            _VIEWER_IOS_REQUIRED_TABLES,
            _VIEWER_IOS_REQUIRED_COLUMNS,
        )
    raise ValueError(f"Unknown platform '{platform}', expected 'android' or 'ios'")


# Backward-compatible alias for validate_archiver_schema
check_wa_schema = validate_archiver_schema
