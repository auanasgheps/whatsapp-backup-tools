"""Unit tests for system events and association types formatters."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from wab_viewer.system_events import (
    AssociationType,
    IOSEventType,
    SystemActionType,
    format_android_system_event,
    format_ios_system_event,
    format_participant_names,
)


def test_format_participant_names_capping():
    """Verify participant name conjunctions and 3-name overflow capping."""
    assert format_participant_names([]) == "Someone"
    assert format_participant_names(["Alice"]) == "Alice"
    assert format_participant_names(["Alice", "Bob"]) == "Alice and Bob"
    assert format_participant_names(["Alice", "Bob", "Charlie"]) == "Alice, Bob, and Charlie"
    assert (
        format_participant_names(["Alice", "Bob", "Charlie", "David"])
        == "Alice, Bob, Charlie, and 1 other"
    )
    assert (
        format_participant_names(["Alice", "Bob", "Charlie", "David", "Eve", "Frank"])
        == "Alice, Bob, Charlie, and 3 others"
    )


def test_format_android_subject_change():
    """Verify Android subject change formatting."""
    res = format_android_system_event(
        SystemActionType.SUBJECT_CHANGE,
        "Alice",
        [],
        "Book Club",
        {},
    )
    assert res == 'Alice changed the subject to "Book Club"'

    res_empty = format_android_system_event(
        SystemActionType.SUBJECT_CHANGE,
        "Alice",
        [],
        "",
        {},
    )
    assert res_empty == "Alice changed the group subject"


def test_format_android_participant_add_and_join():
    """Verify Android participant add and join formatting with capping."""
    res_single = format_android_system_event(
        SystemActionType.PARTICIPANT_ADDED,
        "Alice",
        ["Bob"],
        "",
        {},
    )
    assert res_single == "Alice added Bob"

    res_multi = format_android_system_event(
        SystemActionType.PARTICIPANT_ADDED,
        "Alice",
        ["Bob", "Charlie", "David", "Eve"],
        "",
        {},
    )
    assert res_multi == "Alice added Bob, Charlie, David, and 1 other"

    res_self_join = format_android_system_event(
        SystemActionType.PARTICIPANT_ADDED,
        "Someone",
        ["You"],
        "",
        {},
    )
    assert res_self_join == "You joined"


def test_format_android_invite_link_join():
    """Verify Android join via invite link (GIL) formatting."""
    res_target = format_android_system_event(
        SystemActionType.JOINED_VIA_INVITE_LINK,
        "Someone",
        ["Bob"],
        "",
        {},
    )
    assert res_target == "Bob joined using this group's invite link"

    res_alt = format_android_system_event(
        SystemActionType.JOINED_VIA_INVITE_LINK_ALT,
        "Someone",
        ["You"],
        "",
        {},
    )
    assert res_alt == "You joined using this group's invite link"


def test_format_android_participant_remove_and_leave():
    """Verify Android participant remove and leave formatting."""
    res_leave = format_android_system_event(
        SystemActionType.PARTICIPANT_LEFT,
        "Someone",
        ["Bob"],
        "",
        {},
    )
    assert res_leave == "Bob left"

    res_remove = format_android_system_event(
        SystemActionType.PARTICIPANT_REMOVED,
        "Alice",
        ["Bob"],
        "",
        {},
    )
    assert res_remove == "Alice removed Bob"

    res_removed_you = format_android_system_event(
        SystemActionType.PARTICIPANT_REMOVED,
        "Alice",
        ["You"],
        "",
        {},
    )
    assert res_removed_you == "Alice removed you"


def test_format_android_admin_promote_demote():
    """Verify Android admin promotion and demotion formatting."""
    res_promoted = format_android_system_event(
        SystemActionType.ADMIN_PROMOTED,
        "Someone",
        ["Bob"],
        "",
        {},
    )
    assert res_promoted == "Bob is now an admin"

    res_demoted_you = format_android_system_event(
        SystemActionType.ADMIN_DEMOTED,
        "Someone",
        ["You"],
        "",
        {},
    )
    assert res_demoted_you == "You're no longer an admin"


def test_format_android_icons_and_descriptions():
    """Verify Android icon and description change/delete events."""
    assert (
        format_android_system_event(
            SystemActionType.ICON_CHANGED,
            "Alice",
            [],
            "",
            {},
        )
        == "Alice changed this group's icon"
    )
    assert (
        format_android_system_event(
            SystemActionType.ICON_DELETED,
            "Alice",
            [],
            "",
            {},
        )
        == "Alice deleted this group's icon"
    )
    assert (
        format_android_system_event(
            SystemActionType.DESCRIPTION_CHANGED,
            "Alice",
            [],
            "",
            {},
        )
        == "Alice changed the group description"
    )
    assert (
        format_android_system_event(
            SystemActionType.DESCRIPTION_DELETED,
            "Alice",
            [],
            "",
            {},
        )
        == "Alice deleted the group description"
    )


def test_format_android_ephemeral_and_announcements():
    """Verify disappearing messages and announcement mode formatting."""
    assert (
        format_android_system_event(
            SystemActionType.EPHEMERAL_TIMER_SET,
            "Alice",
            [],
            "604800",
            {},
        )
        == "Disappearing messages set to 7 days"
    )
    assert (
        format_android_system_event(
            SystemActionType.EPHEMERAL_TIMER_SET,
            "Alice",
            [],
            "86400",
            {},
        )
        == "Disappearing messages set to 1 days"
    )
    assert (
        format_android_system_event(
            SystemActionType.EPHEMERAL_TIMER_SET,
            "Alice",
            [],
            "0",
            {},
        )
        == "Disappearing messages turned off"
    )
    assert (
        format_android_system_event(
            SystemActionType.ANNOUNCEMENT_MODE,
            "Alice",
            [],
            "true",
            {},
        )
        == "Only admins can send messages in this group"
    )
    assert (
        format_android_system_event(
            SystemActionType.ANNOUNCEMENT_MODE,
            "Alice",
            [],
            "false",
            {},
        )
        == "All participants can send messages in this group"
    )


def test_format_android_metadata_device_and_number_change():
    """Verify security code and number change notifications."""
    res_device = format_android_system_event(
        None,
        "Alice",
        [],
        "",
        {"is_device_change": "1"},
    )
    assert res_device == "Your security code with Alice changed."

    res_number = format_android_system_event(
        None,
        "Alice",
        [],
        "",
        {"old_phone": "+123456", "new_phone": "+789012"},
    )
    assert res_number == "+123456 changed their phone number to +789012."


def test_format_ios_system_event():
    """Verify iOS service event formatting across mapped event types."""
    assert (
        format_ios_system_event(
            IOSEventType.SUBJECT_CHANGED,
            "Alice",
            [],
            "New Topic",
            False,
        )
        == 'Alice changed the subject to "New Topic"'
    )
    assert (
        format_ios_system_event(
            IOSEventType.PARTICIPANT_JOINED_OR_ADDED,
            "Alice",
            ["Bob", "Charlie", "David", "Eve"],
            "",
            False,
        )
        == "Alice added Bob, Charlie, David, and 1 other"
    )
    assert (
        format_ios_system_event(
            IOSEventType.SECURITY_CODE_CHANGED,
            "Alice",
            [],
            "",
            False,
        )
        == "Your security code with Alice changed."
    )
    assert (
        format_ios_system_event(
            IOSEventType.NUMBER_CHANGED,
            "Alice",
            [],
            "+999888",
            False,
        )
        == "Alice changed their phone number to +999888."
    )
    assert (
        format_ios_system_event(
            IOSEventType.ANNOUNCEMENT_MODE,
            "Alice",
            [],
            "",
            False,
        )
        == "Only admins can send messages in this group"
    )
    assert (
        format_ios_system_event(
            IOSEventType.ANNOUNCEMENT_MODE,
            "Alice",
            [],
            "false",
            False,
        )
        == "All participants can send messages in this group"
    )
    assert (
        format_ios_system_event(
            IOSEventType.APPROVAL_CHANGE,
            "Alice",
            [],
            "",
            False,
        )
        == "Group membership approval settings changed"
    )
    assert (
        format_ios_system_event(
            IOSEventType.SETTINGS_CHANGED_36,
            "Alice",
            [],
            "",
            False,
        )
        == "Group settings changed"
    )


def test_association_type_enum():
    """Verify association type constants match verified database values."""
    assert AssociationType.ANDROID_HD_VIDEO == 7
    assert AssociationType.ANDROID_HD_PHOTO == 12
    assert AssociationType.IOS_HD_VIDEO == 5
    assert AssociationType.IOS_HD_PHOTO == 10
    assert AssociationType.IOS_LIVE_PHOTO == 12


def test_format_android_e2e_and_privacy_notices():
    """Verify E2E encryption and initial privacy provider notices formatting."""
    e2e_text = "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."
    assert (
        format_android_system_event(SystemActionType.E2E_ENCRYPTION, "Alice", [], "", {})
        == e2e_text
    )
    assert (
        format_android_system_event(SystemActionType.INITIAL_PRIVACY_PROVIDER, "Alice", [], "", {})
        == e2e_text
    )
    assert (
        format_android_system_event(SystemActionType.E2E_IDENTITY_NOTICE, "Alice", [], "", {})
        == e2e_text
    )

    biz_e2e = format_android_system_event(
        SystemActionType.INITIAL_PRIVACY_PROVIDER,
        "Alice",
        [],
        "",
        {"verified_biz_name": "Acme Corp"},
    )
    assert (
        biz_e2e
        == "This chat is with the official business account of Acme Corp. Messages and calls are end-to-end encrypted."
    )


def test_format_android_ephemeral_and_call_settings():
    """Verify ephemeral setting action 56, 68 and group call action 70."""
    res_eph_56 = format_android_system_event(
        SystemActionType.EPHEMERAL_SETTING,
        "Alice",
        [],
        "",
        {"setting_duration": "604800"},
    )
    assert res_eph_56 == "Disappearing messages set to 7 days"

    res_eph_off = format_android_system_event(
        SystemActionType.EPHEMERAL_SETTING_ALT,
        "Alice",
        [],
        "",
        {"setting_duration": "0"},
    )
    assert res_eph_off == "Disappearing messages turned off"

    assert (
        format_android_system_event(SystemActionType.LINKED_GROUP_CALL, "Alice", [], "", {})
        == "Group call"
    )
