import os
import re


def sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a name."""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def escape_like(s: str) -> str:
    """Escape SQLite LIKE special characters so the string is treated literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def format_phone(number: str) -> str:
    """Format phone number for display: prepend '00' if no prefix."""
    return "00" + number if number else ""


def build_contact_folder_name(display_name: str, number: str) -> str:
    """Build folder name: 'Display Name (00391234567890)' or 'Unknown (00391234567890)'."""
    formatted = format_phone(number)
    if not display_name or display_name == number:
        label = f"Unknown ({formatted})" if formatted else "Unknown Contact"
    else:
        label = f"{display_name} ({formatted})" if formatted else display_name
    return sanitize_filename(label)


def resolve_vcard_display_name(raw_name: str | None, vcard_text: str | None) -> str:
    """Resolve a clean, friendly display name for a contact card / vCard."""
    cleaned = (raw_name or "").strip().strip("\u200e\u200f").strip()
    if "_$!<Name-Separator>!$_" in cleaned:
        parts = [
            p.strip().strip("\u200e\u200f").strip()
            for p in cleaned.split("_$!<Name-Separator>!$_")
            if p.strip()
        ]
        if len(parts) >= 2:
            names = parts[1:]
            if len(names) == 1:
                return names[0]
            if len(names) == 2:
                return f"{names[0]} & {names[1]}"
            return f"{names[0]} and {len(names) - 1} others"
        if parts:
            return parts[0]

    fns = []
    if vcard_text:
        for line in vcard_text.splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("FN:") or upper.startswith("FN;"):
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    fn = parts[1].strip()
                    if fn:
                        fns.append(fn)

    is_counter = bool(
        cleaned
        and cleaned[0].isdigit()
        and any(
            k in cleaned.lower()
            for k in ("contatt", "contact", "contat", "kişi", "kontakt", "kontak")
        )
    )

    if cleaned and not is_counter:
        return cleaned

    if fns:
        if len(fns) == 1:
            return fns[0]
        if len(fns) == 2:
            return f"{fns[0]} & {fns[1]}"
        return f"{fns[0]} and {len(fns) - 1} others"

    if cleaned:
        return cleaned

    return "Contact"


def classify_media_type(path: str | None, fallback_path: str | None) -> str:
    """Classify a file path or filename into a standard media category.

    Categories returned:
        'image', 'video', 'document', 'audio', 'gif', 'sticker', 'vcard'
    """
    candidates = [p for p in (path, fallback_path) if p]
    if not candidates:
        return "document"

    norm_candidates = [p.lower().replace("\\", "/") for p in candidates]

    # 1. Contact cards / vCards
    for p in norm_candidates:
        if p.endswith(".vcf") or p.startswith("vcard:"):
            return "vcard"

    # 2. Stickers (WhatsApp Stickers folder, stk- prefix, .was/.webp sticker files)
    for p in norm_candidates:
        if "/documents/" in p or "whatsapp documents" in p:
            continue
        fname = os.path.basename(p)
        if (
            "/stickers/" in p
            or "whatsapp stickers" in p
            or (fname.startswith("stk-") and (p.endswith(".webp") or p.endswith(".was")))
            or ("sticker" in p and p.endswith(".webp"))
            or p.endswith(".webp")
            or p.endswith(".was")
        ):
            return "sticker"

    # 3. Animated GIFs (WhatsApp Animated Gifs folder, gif- prefix, .gif extension)
    for p in norm_candidates:
        fname = os.path.basename(p)
        if (
            p.endswith(".gif")
            or "/animated gifs/" in p
            or "whatsapp animated gifs" in p
            or fname.startswith("gif-")
        ):
            return "gif"

    # 4. Extension-based classification
    for p in norm_candidates:
        ext = os.path.splitext(p)[1]
        if ext in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp"):
            return "image"
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v"):
            return "video"
        if ext in (".mp3", ".ogg", ".aac", ".opus", ".m4a", ".wav", ".amr"):
            return "audio"
        if ext in (
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".txt",
            ".csv",
            ".rtf",
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".gz",
        ):
            return "document"

    # 5. Folder structure fallback (Android / iOS / general media folders)
    for p in norm_candidates:
        if "whatsapp images" in p or "/images/" in p or "/photos/" in p:
            return "image"
        if "whatsapp video" in p or "/video/" in p or "/videos/" in p:
            return "video"
        if (
            "whatsapp audio" in p
            or "whatsapp voice notes" in p
            or "/audio/" in p
            or "/voice notes/" in p
        ):
            return "audio"
        if "whatsapp documents" in p or "/documents/" in p or "/docs/" in p:
            return "document"
        if "sticker" in p:
            return "sticker"
        if "gif" in p:
            return "gif"

    return "document"


def extract_ios_link_description(metadata_bytes: bytes | None) -> str | None:
    """Extract page description string from wire tag 3 of an iOS ZMETADATA protobuf blob."""
    if not metadata_bytes or not isinstance(metadata_bytes, (bytes, bytearray)):
        return None
    try:
        idx = 0
        n = len(metadata_bytes)
        while idx < n:
            b = metadata_bytes[idx]
            idx += 1
            wire_type = b & 7
            field_num = b >> 3
            if wire_type == 0:
                while idx < n and (metadata_bytes[idx] & 0x80):
                    idx += 1
                idx += 1
            elif wire_type == 2:
                length = 0
                shift = 0
                while idx < n:
                    lb = metadata_bytes[idx]
                    idx += 1
                    length |= (lb & 0x7F) << shift
                    if not (lb & 0x80):
                        break
                    shift += 7
                if field_num == 3:
                    desc_bytes = metadata_bytes[idx : idx + length]
                    return desc_bytes.decode("utf-8", errors="replace")
                idx += length
            elif wire_type == 5:
                idx += 4
            elif wire_type == 1:
                idx += 8
            else:
                break
    except Exception:
        pass
    return None
