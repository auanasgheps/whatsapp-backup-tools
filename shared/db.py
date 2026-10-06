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
        label = f"Unknown ({formatted})"
    else:
        label = f"{display_name} ({formatted})"
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
            if line.startswith("FN:"):
                fn = line[3:].strip()
                if fn:
                    fns.append(fn)

    is_counter = bool(
        cleaned
        and cleaned[0].isdigit()
        and any(k in cleaned.lower() for k in ("contatt", "contact", "kişi", "kontakt"))
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

