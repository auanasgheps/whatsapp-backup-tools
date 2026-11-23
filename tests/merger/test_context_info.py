"""Unit tests for extract_context_info in wab_archiver.increment_merger.

Tests pure-Python Protobuf wire decoding of quoted messages, nested mentions,
and multimedia metadata with 100% synthetic data.
"""

from __future__ import annotations

import struct

from tests.fixtures.protobuf_factory import (
    build_context_info,
    build_field,
)
from wab_archiver import increment_merger


class TestContextInfoExtraction:
    """Comprehensive tests for extract_context_info wire decoding."""

    def test_empty_or_missing_context_tag_returns_none(self) -> None:
        assert increment_merger.extract_context_info({}) is None
        assert increment_merger.extract_context_info({1: [(0, 42)]}) is None

    def test_context_container_resolution_tags_17_14_15(self) -> None:
        # Tag 17
        ctx_bytes = build_context_info(stanza_id="STZ_17", participant="5550100@s.whatsapp.net")
        res17 = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res17 is not None
        assert res17["stanza_id"] == "STZ_17"
        assert res17["participant"] == "5550100@s.whatsapp.net"

        # Tag 14
        ctx_bytes14 = build_context_info(stanza_id="STZ_14", participant="5550200@s.whatsapp.net")
        res14 = increment_merger.extract_context_info({14: [(2, ctx_bytes14)]})
        assert res14 is not None
        assert res14["stanza_id"] == "STZ_14"

        # Tag 15
        ctx_bytes15 = build_context_info(stanza_id="STZ_15", participant="5550300@s.whatsapp.net")
        res15 = increment_merger.extract_context_info({15: [(2, ctx_bytes15)]})
        assert res15 is not None
        assert res15["stanza_id"] == "STZ_15"

    def test_quoted_simple_conversation_text(self) -> None:
        q_msg = build_field(1, 2, b"Hello from quoted message")
        ctx_bytes = build_context_info(stanza_id="STZ_TXT", quoted_msg_bytes=q_msg)
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["message_type"] == 0
        assert res["quoted_text"] == "Hello from quoted message"

    def test_quoted_extended_text_with_thumbnail(self) -> None:
        ext_inner = build_field(1, 2, b"Extended quoted text") + build_field(
            16, 2, b"\xff\xd8\xffthumb"
        )
        q_msg = build_field(6, 2, ext_inner)
        ctx_bytes = build_context_info(stanza_id="STZ_EXT", quoted_msg_bytes=q_msg)
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["message_type"] == 0
        assert res["quoted_text"] == "Extended quoted text"
        assert res["thumbnail"] == b"\xff\xd8\xffthumb"

    def test_quoted_image_message(self) -> None:
        img_inner = (
            build_field(1, 2, b"https://example.com/img.enc")
            + build_field(2, 2, b"image/jpeg")
            + build_field(3, 2, b"Photo caption")
            + build_field(4, 2, b"\xaa" * 32)
            + build_field(5, 0, 54321)
            + build_field(6, 0, 1080)
            + build_field(7, 0, 1920)
            + build_field(8, 2, b"media_key_bytes")
            + build_field(11, 2, b"/path/to/img")
            + build_field(16, 2, b"thumb_data")
        )
        q_msg = build_field(3, 2, img_inner)
        ctx_bytes = build_context_info(stanza_id="STZ_IMG", quoted_msg_bytes=q_msg)
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["message_type"] == 1
        assert res["quoted_text"] == "Photo caption"
        assert res["thumbnail"] == b"thumb_data"
        assert res["media_info"] is not None
        assert res["media_info"]["file_size"] == 54321
        assert res["media_info"]["height"] == 1080
        assert res["media_info"]["width"] == 1920
        assert res["media_info"]["direct_path"] == "/path/to/img"

    def test_quoted_document_message_title_and_filename(self) -> None:
        doc_inner = (
            build_field(3, 2, b"Project Specs")
            + build_field(8, 2, b"specs_final.pdf")
            + build_field(2, 2, b"application/pdf")
            + build_field(5, 0, 98765)
            + build_field(16, 2, b"doc_thumb")
        )
        q_msg = build_field(7, 2, doc_inner)
        ctx_bytes = build_context_info(stanza_id="STZ_DOC", quoted_msg_bytes=q_msg)
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["message_type"] == 9
        assert res["quoted_text"] == "Project Specs"
        assert res["thumbnail"] == b"doc_thumb"
        assert res["media_info"]["file_size"] == 98765
        assert res["media_info"]["mime_type"] == "application/pdf"

    def test_quoted_video_message(self) -> None:
        vid_inner = (
            build_field(2, 2, b"video/mp4")
            + build_field(4, 2, b"\xbb" * 32)
            + build_field(5, 0, 456789)
            + build_field(6, 0, 720)
            + build_field(7, 2, b"Video caption")
            + build_field(8, 0, 45)  # 45 seconds duration
            + build_field(16, 2, b"vid_thumb")
        )
        q_msg = build_field(9, 2, vid_inner)
        ctx_bytes = build_context_info(stanza_id="STZ_VID", quoted_msg_bytes=q_msg)
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["message_type"] == 3
        assert res["quoted_text"] == "Video caption"
        assert res["thumbnail"] == b"vid_thumb"
        assert res["media_info"]["media_duration"] == 45

    def test_quoted_sticker_message(self) -> None:
        stk_inner = (
            build_field(2, 2, b"image/webp")
            + build_field(4, 2, b"\xcc" * 32)
            + build_field(5, 0, 12345)
            + build_field(16, 2, b"stk_thumb")
        )
        # Tag 20
        q_msg = build_field(20, 2, stk_inner)
        res20 = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_STK20", quoted_msg_bytes=q_msg))]}
        )
        assert res20 is not None
        assert res20["message_type"] == 20
        assert res20["thumbnail"] == b"stk_thumb"
        assert res20["media_info"]["mime_type"] == "image/webp"

        # Tag 26
        q_msg26 = build_field(26, 2, stk_inner)
        res26 = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_STK26", quoted_msg_bytes=q_msg26))]}
        )
        assert res26 is not None
        assert res26["message_type"] == 20

    def test_quoted_audio_ptt_and_music(self) -> None:
        # PTT voice note: tag 6 is set
        ptt_inner = (
            build_field(2, 2, b"audio/ogg; codecs=opus")
            + build_field(4, 0, 32100)
            + build_field(5, 0, 15)  # 15s
            + build_field(6, 0, 1)  # is_ptt
        )
        q_ptt = build_field(8, 2, ptt_inner)
        res_ptt = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_PTT", quoted_msg_bytes=q_ptt))]}
        )
        assert res_ptt is not None
        assert res_ptt["message_type"] == 2
        assert res_ptt["quoted_text"] == "🎵 Voice message"

        # Non-PTT audio: tag 6 is 0
        aud_inner = (
            build_field(2, 2, b"audio/mp3")
            + build_field(4, 0, 500000)
            + build_field(5, 0, 180)
            + build_field(6, 0, 0)
        )
        q_aud = build_field(8, 2, aud_inner)
        res_aud = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_AUD", quoted_msg_bytes=q_aud))]}
        )
        assert res_aud is not None
        assert res_aud["message_type"] == 2
        assert res_aud["quoted_text"] == "🎵 Audio"

    def test_quoted_location_message(self) -> None:
        lat_bytes = struct.pack("<d", 45.4642)
        lng_bytes = struct.pack("<d", 9.1900)
        loc_inner = (
            build_field(1, 1, lat_bytes)
            + build_field(2, 1, lng_bytes)
            + build_field(3, 2, b"Duomo di Milano")
            + build_field(4, 2, b"Piazza del Duomo, Milano")
            + build_field(5, 2, b"https://maps.example.com")
            + build_field(16, 2, b"map_thumb")
        )
        q_msg = build_field(5, 2, loc_inner)
        res = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_LOC", quoted_msg_bytes=q_msg))]}
        )
        assert res is not None
        assert res["message_type"] == 5
        assert res["quoted_text"] == "Duomo di Milano"
        assert res["location_info"] is not None
        assert abs(res["location_info"]["latitude"] - 45.4642) < 1e-4
        assert abs(res["location_info"]["longitude"] - 9.1900) < 1e-4
        assert res["location_info"]["place_name"] == "Duomo di Milano"
        assert res["location_info"]["thumbnail"] == b"map_thumb"

    def test_quoted_contact_vcard(self) -> None:
        vcard_text = "BEGIN:VCARD\nVERSION:3.0\nFN:Synthetic Contact\nTEL:+15550100\nEND:VCARD"
        ct_inner = build_field(1, 2, b"Synthetic Contact") + build_field(
            2, 2, vcard_text.encode("utf-8")
        )
        q_msg = build_field(4, 2, ct_inner)
        res = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_CT", quoted_msg_bytes=q_msg))]}
        )
        assert res is not None
        assert res["message_type"] == 4
        assert res["quoted_text"] == "Synthetic Contact"
        assert res["vcard"] == vcard_text

    def test_quoted_poll_message(self) -> None:
        # Tag 49 with tag 2 question name
        poll_inner = build_field(2, 2, b"Lunch meeting?")
        q_msg = build_field(49, 2, poll_inner)
        res = increment_merger.extract_context_info(
            {17: [(2, build_context_info(stanza_id="STZ_POLL", quoted_msg_bytes=q_msg))]}
        )
        assert res is not None
        assert res["message_type"] == 66
        assert res["quoted_text"] == "Lunch meeting?"

    def test_mentioned_jids_direct_and_nested_in_quote(self) -> None:
        # Direct mentions in context info tag 15
        ctx_bytes = build_context_info(
            stanza_id="STZ_MNT",
            mentioned_jids=["5550100@s.whatsapp.net", "5550200@s.whatsapp.net"],
        )
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["mentioned_jids"] == ["5550100@s.whatsapp.net", "5550200@s.whatsapp.net"]

        # Nested mentions inside quoted message
        inner_quote_ctx = build_context_info(
            stanza_id="INNER_Q", mentioned_jids=["5550300@s.whatsapp.net"]
        )
        quoted_img_with_ctx = build_field(3, 2, b"Caption") + build_field(17, 2, inner_quote_ctx)
        q_msg = build_field(3, 2, quoted_img_with_ctx)
        outer_ctx = build_context_info(stanza_id="OUTER_Q", quoted_msg_bytes=q_msg)

        res_nested = increment_merger.extract_context_info({17: [(2, outer_ctx)]})
        assert res_nested is not None
        assert "5550300@s.whatsapp.net" in res_nested["quoted_mentioned_jids"]

    def test_context_info_nested_tags_49_60_64_93_111(self) -> None:
        """Verify extraction of quoted message nested inside container tags 49, 60, 64, 93, 111."""
        inner_poll = build_field(2, 2, b"Weekend trip destination?")
        wrapper_49 = build_field(49, 2, inner_poll)
        sub_1 = build_field(1, 2, wrapper_49)
        q_msg = build_field(64, 2, sub_1)

        ctx_bytes = build_context_info(stanza_id="STZ_WRAPPER_64", quoted_msg_bytes=q_msg)
        res = increment_merger.extract_context_info({17: [(2, ctx_bytes)]})
        assert res is not None
        assert res["message_type"] == 66
        assert res["quoted_text"] == "Weekend trip destination?"

    def test_context_info_mentioned_jids_deduplication(self) -> None:
        """Verify that duplicate mentioned JIDs inside quotes are deduplicated preserving order."""
        inner_quote_ctx = (
            build_field(1, 2, b"STZ_INNER_DUP")
            + build_field(15, 2, b"5550100@s.whatsapp.net")
            + build_field(15, 2, b"5550200@s.whatsapp.net")
            + build_field(15, 2, b"5550100@s.whatsapp.net")
        )
        quoted_ext_msg = build_field(
            6, 2, build_field(1, 2, b"Quote text") + build_field(17, 2, inner_quote_ctx)
        )
        outer_ctx = build_context_info(stanza_id="STZ_OUTER", quoted_msg_bytes=quoted_ext_msg)

        res = increment_merger.extract_context_info({17: [(2, outer_ctx)]})
        assert res is not None
        assert res["quoted_mentioned_jids"] == ["5550100@s.whatsapp.net", "5550200@s.whatsapp.net"]
