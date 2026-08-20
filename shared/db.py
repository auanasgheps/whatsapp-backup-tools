import re


def sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a name."""
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def escape_like(s: str) -> str:
    """Escape SQLite LIKE special characters so the string is treated literally."""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def format_phone(number: str) -> str:
    """Format phone number for display: prepend '00' if no prefix."""
    return '00' + number if number else ''


def build_contact_folder_name(display_name: str, number: str) -> str:
    """Build folder name: 'Display Name (00391234567890)' or 'Unknown (00391234567890)'."""
    formatted = format_phone(number)
    if not display_name or display_name == number:
        label = f"Unknown ({formatted})"
    else:
        label = f"{display_name} ({formatted})"
    return sanitize_filename(label)
