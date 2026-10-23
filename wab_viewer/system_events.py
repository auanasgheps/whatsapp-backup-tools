"""System events and message association definitions and formatters.

Exhaustively maps WhatsApp system message action types, event types, and
association types across Android and iOS platforms.
"""

from enum import IntEnum


class SystemActionType(IntEnum):
    """Android message_system action_type constants."""

    SUBJECT_CHANGE = 1
    PARTICIPANT_JOINED_LEGACY = 4
    PARTICIPANT_LEFT_LEGACY = 5
    ICON_CHANGED = 6
    ICON_DELETED = 7
    GROUP_CREATED = 11
    PARTICIPANT_ADDED = 12
    PARTICIPANT_LEFT = 13
    PARTICIPANT_REMOVED = 14
    ADMIN_PROMOTED = 15
    E2E_ENCRYPTION = 18
    EPHEMERAL_TOGGLE = 19
    ADMIN_DEMOTED = 20
    DESCRIPTION_CHANGED = 27
    DESCRIPTION_DELETED = 28
    EPHEMERAL_SETTING = 56
    ANNOUNCEMENT_MODE = 58
    BIZ_ACCOUNT_STATE = 61
    INITIAL_PRIVACY_PROVIDER = 67
    EPHEMERAL_TIMER_SET = 67
    EPHEMERAL_SETTING_ALT = 68
    BIZ_ACCOUNT_VERIFIED = 69
    LINKED_GROUP_CALL = 70
    JOINED_VIA_INVITE_LINK = 79
    JOINED_VIA_INVITE_LINK_ALT = 123
    MEMBERSHIP_APPROVAL_REQUEST = 124
    COMMUNITY_LINK_CHANGED = 127
    E2E_IDENTITY_NOTICE = 129
    MEMBERSHIP_APPROVAL_MODE = 136
    MEMBERSHIP_APPROVAL_APPROVED = 144
    EPHEMERAL_DEFAULT_TIMER = 146


ALLOWED_GROUP_SYSTEM_ACTIONS = (
    int(SystemActionType.SUBJECT_CHANGE),
    int(SystemActionType.PARTICIPANT_JOINED_LEGACY),
    int(SystemActionType.PARTICIPANT_LEFT_LEGACY),
    int(SystemActionType.ICON_CHANGED),
    int(SystemActionType.ICON_DELETED),
    int(SystemActionType.GROUP_CREATED),
    int(SystemActionType.PARTICIPANT_ADDED),
    int(SystemActionType.PARTICIPANT_LEFT),
    int(SystemActionType.PARTICIPANT_REMOVED),
    int(SystemActionType.ADMIN_PROMOTED),
    int(SystemActionType.EPHEMERAL_TOGGLE),
    int(SystemActionType.ADMIN_DEMOTED),
    int(SystemActionType.DESCRIPTION_CHANGED),
    int(SystemActionType.DESCRIPTION_DELETED),
    int(SystemActionType.EPHEMERAL_SETTING),
    int(SystemActionType.ANNOUNCEMENT_MODE),
    int(SystemActionType.EPHEMERAL_SETTING_ALT),
    int(SystemActionType.LINKED_GROUP_CALL),
    int(SystemActionType.JOINED_VIA_INVITE_LINK),
    int(SystemActionType.JOINED_VIA_INVITE_LINK_ALT),
    int(SystemActionType.MEMBERSHIP_APPROVAL_REQUEST),
    int(SystemActionType.COMMUNITY_LINK_CHANGED),
    int(SystemActionType.MEMBERSHIP_APPROVAL_MODE),
    int(SystemActionType.MEMBERSHIP_APPROVAL_APPROVED),
    int(SystemActionType.EPHEMERAL_DEFAULT_TIMER),
)


class IOSEventType(IntEnum):
    """iOS ZGROUPEVENTTYPE constants."""

    NONE = 0
    SUBJECT_CHANGED = 1
    PARTICIPANT_JOINED_OR_ADDED = 2
    ICON_CHANGED = 3
    PARTICIPANT_LEFT = 4
    ADMIN_DEMOTED = 5
    NUMBER_CHANGED = 6
    PARTICIPANT_REMOVED = 7
    ADMIN_PROMOTED = 9
    DESCRIPTION_CHANGED = 10
    GROUP_CREATED = 12
    JOINED_VIA_INVITE_LINK = 15
    CONTACT_SHARED = 21
    DISAPPEARING_MESSAGES = 26
    SETTINGS_CHANGED_36 = 36
    SETTINGS_CHANGED_37 = 37
    APPROVAL_CHANGE = 38
    MEMBERSHIP_APPROVAL_ADDED = 44
    MULTIPLE_PARTICIPANTS_ADDED = 50
    SECURITY_CODE_CHANGED = 56
    ANNOUNCEMENT_MODE = 58


class AssociationType(IntEnum):
    """Message association types across Android and iOS."""

    # Android (message_association)
    ANDROID_REPLY_OR_ALBUM = 1
    ANDROID_BOT_THREAD = 2
    ANDROID_STATUS_REPLY = 3
    ANDROID_HD_VIDEO = 7
    ANDROID_HD_PHOTO = 12
    ANDROID_ALBUM_PARENT = 13

    # iOS (ExtChatDatabase message_parent_association)
    IOS_ALBUM_ITEM = 1
    IOS_HD_VIDEO = 5
    IOS_HD_PHOTO = 10
    IOS_LIVE_PHOTO = 12


def format_participant_names(names: list[str]) -> str:
    """Format a list of participant names, capping after 3 with an overflow count."""
    if not names:
        return "Someone"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    if len(names) == 3:
        return f"{names[0]}, {names[1]}, and {names[2]}"
    overflow = len(names) - 3
    suffix = "other" if overflow == 1 else "others"
    return f"{names[0]}, {names[1]}, {names[2]}, and {overflow} {suffix}"


def format_android_system_event(
    action_type: int | None,
    actor: str,
    targets: list[str],
    text: str,
    metadata: dict[str, str],
) -> str:
    """Format an Android service event into human-readable text."""
    target_str = format_participant_names(targets) if targets else "Someone"

    # Number change
    if "new_phone" in metadata and metadata["new_phone"]:
        old_name = metadata.get("old_phone", actor or "A contact")
        new_name = metadata["new_phone"]
        return f"{old_name} changed their phone number to {new_name}."

    # Security code change
    if metadata.get("is_device_change"):
        contact_name = target_str if target_str != "Someone" else (actor or "this contact")
        return f"Your security code with {contact_name} changed."

    if action_type == SystemActionType.SUBJECT_CHANGE:
        if text:
            return f'{actor} changed the subject to "{text}"'
        return f"{actor} changed the group subject"

    if action_type in (
        SystemActionType.PARTICIPANT_ADDED,
        SystemActionType.PARTICIPANT_JOINED_LEGACY,
    ):
        if target_str in ("You", "you") and (not actor or actor in ("Someone", "You")):
            return "You joined"
        if actor and actor != "Someone" and actor != target_str:
            return f"{actor} added {target_str}"
        return f"{target_str} joined"

    if action_type in (
        SystemActionType.JOINED_VIA_INVITE_LINK,
        SystemActionType.JOINED_VIA_INVITE_LINK_ALT,
    ):
        if target_str in ("You", "you"):
            return "You joined using this group's invite link"
        return f"{target_str} joined using this group's invite link"

    if action_type in (
        SystemActionType.PARTICIPANT_LEFT,
        SystemActionType.PARTICIPANT_LEFT_LEGACY,
    ):
        if target_str in ("You", "you"):
            return "You left"
        return f"{target_str} left"

    if action_type == SystemActionType.PARTICIPANT_REMOVED:
        if target_str in ("You", "you"):
            if actor and actor not in ("Someone", "You"):
                return f"{actor} removed you"
            return "You were removed"
        if actor and actor != "Someone" and actor != target_str:
            return f"{actor} removed {target_str}"
        return f"{target_str} was removed"

    if action_type == SystemActionType.GROUP_CREATED:
        if text:
            return f'{actor} created group "{text}"'
        return f"{actor} created this group"

    if action_type == SystemActionType.ICON_CHANGED:
        return f"{actor} changed this group's icon"

    if action_type == SystemActionType.ICON_DELETED:
        return f"{actor} deleted this group's icon"

    if action_type == SystemActionType.DESCRIPTION_CHANGED:
        return f"{actor} changed the group description"

    if action_type == SystemActionType.DESCRIPTION_DELETED:
        return f"{actor} deleted the group description"

    if action_type == SystemActionType.ADMIN_PROMOTED:
        if target_str in ("You", "you"):
            return "You're now an admin"
        return f"{target_str} is now an admin"

    if action_type == SystemActionType.ADMIN_DEMOTED:
        if target_str in ("You", "you"):
            return "You're no longer an admin"
        return f"{target_str} is no longer an admin"

    if action_type in (
        SystemActionType.E2E_ENCRYPTION,
        SystemActionType.E2E_IDENTITY_NOTICE,
    ):
        biz_name = metadata.get("verified_biz_name")
        if biz_name:
            return f"This chat is with the official business account of {biz_name}. Messages and calls are end-to-end encrypted."
        return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."

    if action_type == SystemActionType.INITIAL_PRIVACY_PROVIDER:
        if text and text.isdigit():
            secs = int(text)
            if secs > 0:
                days = secs // 86400
                if days >= 1:
                    return f"Disappearing messages set to {days} days"
                hours = secs // 3600
                return f"Disappearing messages set to {hours} hours"
            return "Disappearing messages turned off"
        biz_name = metadata.get("verified_biz_name")
        if biz_name:
            return f"This chat is with the official business account of {biz_name}. Messages and calls are end-to-end encrypted."
        return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."

    if action_type in (
        SystemActionType.EPHEMERAL_TOGGLE,
        SystemActionType.EPHEMERAL_SETTING,
        SystemActionType.EPHEMERAL_SETTING_ALT,
    ):
        dur = metadata.get("setting_duration") or metadata.get("duration") or text
        secs = int(dur) if dur and str(dur).isdigit() else 0
        if secs > 0:
            days = secs // 86400
            if days >= 1:
                return f"Disappearing messages set to {days} days"
            hours = secs // 3600
            return f"Disappearing messages set to {hours} hours"
        return "Disappearing messages turned off"

    if action_type == SystemActionType.LINKED_GROUP_CALL:
        return "Group call"

    if action_type == SystemActionType.EPHEMERAL_DEFAULT_TIMER:
        secs = int(text) if text and text.isdigit() else 0
        if secs > 0:
            days = secs // 86400
            return f"New messages in this chat will disappear after {days} days"
        return "Disappearing messages turned off"

    if action_type == SystemActionType.ANNOUNCEMENT_MODE:
        if text.lower() == "true":
            return "Only admins can send messages in this group"
        return "All participants can send messages in this group"

    if action_type in (SystemActionType.BIZ_ACCOUNT_STATE, SystemActionType.BIZ_ACCOUNT_VERIFIED):
        return "This chat is now with a WhatsApp Business account."

    if action_type in (
        SystemActionType.MEMBERSHIP_APPROVAL_REQUEST,
        SystemActionType.MEMBERSHIP_APPROVAL_APPROVED,
    ):
        if actor and actor != "Someone" and actor != target_str:
            return f"{actor} approved {target_str}'s request to join"
        return f"{target_str} joined after admin approval"

    if action_type == SystemActionType.COMMUNITY_LINK_CHANGED:
        parent_name = metadata.get("community_name", "")
        if parent_name:
            return f'{actor} added this group to community "{parent_name}"'
        return f"{actor} added this group to the community"

    if action_type == SystemActionType.MEMBERSHIP_APPROVAL_MODE:
        if text.lower() == "true":
            return "Admin approval is required to join this group"
        return "Admin approval is no longer required to join this group"

    if (
        text
        and not text.startswith("{")
        and "@lid" not in text
        and text.lower() not in ("true", "false")
        and not text.isdigit()
    ):
        return text

    return "Group event"


def format_ios_system_event(
    event_type: int | None,
    actor: str,
    targets: list[str],
    text: str,
    is_from_me: bool,
) -> str:
    """Format an iOS service event into human-readable text."""
    target_str = format_participant_names(targets) if targets else "Someone"

    if event_type == IOSEventType.NUMBER_CHANGED:
        new_phone = text if text and not text.startswith("{") else ""
        if new_phone:
            old_name = actor if actor and actor != "Someone" else "A contact"
            return f"{old_name} changed their phone number to {new_phone}."
        return "Phone number changed"

    if event_type == IOSEventType.SECURITY_CODE_CHANGED:
        contact_name = actor if actor and actor != "Someone" else target_str
        return f"Your security code with {contact_name} changed."

    if event_type == IOSEventType.GROUP_CREATED:
        if text:
            return f'{actor} created group "{text}"'
        return f"{actor} created this group"

    if event_type == IOSEventType.SUBJECT_CHANGED:
        if text:
            return f'{actor} changed the subject to "{text}"'
        return f"{actor} changed the group subject"

    if event_type == IOSEventType.ICON_CHANGED:
        if text == "deleted":
            return f"{actor} deleted this group's icon"
        return f"{actor} changed this group's icon"

    if event_type == IOSEventType.PARTICIPANT_LEFT:
        if is_from_me or target_str in ("You", "you"):
            return "You left"
        return f"{target_str} left"

    if event_type == IOSEventType.JOINED_VIA_INVITE_LINK:
        if is_from_me or target_str in ("You", "you"):
            return "You joined using this group's invite link"
        return f"{target_str} joined using this group's invite link"

    if event_type in (
        IOSEventType.PARTICIPANT_JOINED_OR_ADDED,
        IOSEventType.MULTIPLE_PARTICIPANTS_ADDED,
        IOSEventType.MEMBERSHIP_APPROVAL_ADDED,
    ):
        if target_str in ("You", "you"):
            if actor and actor != "Someone" and actor != "You":
                return f"{actor} added you"
            return "You joined"
        if actor == "You":
            return f"You added {target_str}"
        if actor and actor != "Someone" and actor != target_str:
            return f"{actor} added {target_str}"
        return f"{target_str} joined"

    if event_type == IOSEventType.PARTICIPANT_REMOVED:
        if target_str in ("You", "you"):
            if actor and actor != "Someone" and actor != "You":
                return f"{actor} removed you"
            return "You left"
        if actor == "You":
            return f"You removed {target_str}"
        if actor and actor != "Someone" and actor != target_str:
            return f"{actor} removed {target_str}"
        return f"{target_str} was removed"

    if event_type == IOSEventType.ADMIN_PROMOTED:
        if target_str in ("You", "you"):
            return "You're now an admin"
        return f"{target_str} is now an admin"

    if event_type == IOSEventType.ADMIN_DEMOTED:
        if target_str in ("You", "you"):
            return "You're no longer an admin"
        return f"{target_str} is no longer an admin"

    if event_type == IOSEventType.DESCRIPTION_CHANGED:
        if text:
            return f'{actor} changed the group description to "{text}"'
        return f"{actor} changed the group description"

    if event_type == IOSEventType.DISAPPEARING_MESSAGES:
        secs = int(text) if text and text.isdigit() else None
        if secs and secs > 0:
            days = secs // 86400
            if days >= 1:
                return f"Disappearing messages set to {days} days"
            hours = secs // 3600
            return f"Disappearing messages set to {hours} hours"
        return "Disappearing messages turned off"

    if event_type in (IOSEventType.SETTINGS_CHANGED_36, IOSEventType.SETTINGS_CHANGED_37):
        return "Group settings changed"

    if event_type == IOSEventType.APPROVAL_CHANGE:
        return "Group membership approval settings changed"

    if event_type == IOSEventType.ANNOUNCEMENT_MODE:
        if text and text.lower() == "false":
            return "All participants can send messages in this group"
        return "Only admins can send messages in this group"

    if (
        text
        and not text.startswith("{")
        and "@lid" not in text
        and not text.isdigit()
        and "@s.whatsapp.net" not in text
    ):
        return text

    return "Group event"
