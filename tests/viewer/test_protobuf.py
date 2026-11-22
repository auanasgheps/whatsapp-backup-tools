"""Unit tests for pure-Python protobuf parsing in wab_viewer.protobuf.

Tests low-level wire parsing, iOS CoreData ZRECEIPTINFO blobs, ZMETADATA
quoted messages, link descriptions, reactions, and mentions with 100% synthetic data.
"""

from __future__ import annotations

import base64

import pytest
from tests.fixtures.protobuf_factory import (
    build_field,
    build_ios_poll_blob,
    encode_varint,
    make_ios_reaction_blob,
)
from wab_viewer.protobuf import (
    _extract_context_mentioned_jids,
    _extract_ios_group_description,
    _extract_ios_link_description,
    _extract_ios_mentions,
    _extract_ios_quoted_info,
    _extract_ios_reaction_reactors,
    _extract_ios_reactions,
    _iter_wire_fields,
    _parse_ios_poll_blob,
    _parse_protobuf,
    _read_varint,
    _scan_emojis,
)


class TestVarintParsing:
    """Tests for low-level base-128 varint reading."""

    def test_read_single_byte_varints(self) -> None:
        val, off = _read_varint(b"\x00", 0)
        assert val == 0 and off == 1

        val, off = _read_varint(b"\x01", 0)
        assert val == 1 and off == 1

        val, off = _read_varint(b"\x7f", 0)
        assert val == 127 and off == 1

    def test_read_multi_byte_varints(self) -> None:
        # 128 = 0x80 0x01
        val, off = _read_varint(b"\x80\x01", 0)
        assert val == 128 and off == 2

        # 300 = 0xac 0x02
        val, off = _read_varint(b"\xac\x02", 0)
        assert val == 300 and off == 2

        # Large 64-bit integer
        encoded = encode_varint(123456789012345)
        val, off = _read_varint(encoded, 0)
        assert val == 123456789012345 and off == len(encoded)

    def test_read_varint_truncated_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Truncated varint"):
            _read_varint(b"\x80", 0)

        with pytest.raises(ValueError, match="Truncated varint"):
            _read_varint(b"\x80\x80\x80", 0)

        with pytest.raises(ValueError, match="Truncated varint"):
            _read_varint(b"", 0)


class TestGroupDescriptionExtraction:
    """Tests for _extract_ios_group_description from ZPICTUREID."""

    def test_none_or_empty_input(self) -> None:
        assert _extract_ios_group_description("") is None
        assert _extract_ios_group_description(None) is None  # type: ignore[arg-type]

    def test_invalid_base64(self) -> None:
        assert _extract_ios_group_description("!!!NotValidBase64!!!") is None

    def test_valid_group_description_raw_and_plus_prefixed(self) -> None:
        # Construct protobuf: field 1 (wire 2) -> subfield 3 (wire 2) -> "Project Group"
        sub_desc = build_field(3, 2, b"Project Discussion Group")
        # Add a dummy wire 0, 1, 5 to test wire skipping
        sub_desc += build_field(10, 0, 42)
        sub_desc += build_field(11, 1, b"\x01" * 8)
        sub_desc += build_field(12, 5, b"\x02" * 4)

        top = build_field(1, 2, sub_desc)
        # Add top wire 0, 1, 5 to test outer wire skipping
        top += build_field(20, 0, 99)
        top += build_field(21, 1, b"\x03" * 8)
        top += build_field(22, 5, b"\x04" * 4)

        b64_raw = base64.b64encode(top).decode("ascii")
        assert _extract_ios_group_description(b64_raw) == "Project Discussion Group"

        # With leading '+' prefix commonly found in iOS database rows
        b64_plus = "+" + b64_raw
        assert _extract_ios_group_description(b64_plus) == "Project Discussion Group"

    def test_empty_or_whitespace_description(self) -> None:
        sub_desc = build_field(3, 2, b"   ")
        top = build_field(1, 2, sub_desc)
        b64 = base64.b64encode(top).decode("ascii")
        assert _extract_ios_group_description(b64) is None

    def test_truncated_subfields(self) -> None:
        # Invalid wire / truncated length
        bad_proto = build_field(1, 2, b"\x1a\x99")  # length says 153 bytes but ends
        b64 = base64.b64encode(bad_proto).decode("ascii")
        assert _extract_ios_group_description(b64) is None


class TestPollBlobParsing:
    """Tests for _parse_ios_poll_blob parsing from ZRECEIPTINFO."""

    def test_none_or_empty_blob(self) -> None:
        assert _parse_ios_poll_blob(b"") is None
        assert _parse_ios_poll_blob(None) is None  # type: ignore[arg-type]

    def test_blob_without_field_8(self) -> None:
        data = build_field(1, 2, b"other data") + build_field(2, 0, 100)
        assert _parse_ios_poll_blob(data) is None

    def test_single_and_multiple_choice_poll(self) -> None:
        # Multiple choice: selectable_count == 0
        multi_blob = build_ios_poll_blob(
            question="Favorite fruits?",
            options=["Apple", "Banana", "Orange"],
            selectable_count=0,
        )
        parsed = _parse_ios_poll_blob(multi_blob)
        assert parsed is not None
        assert parsed["question"] == "Favorite fruits?"
        assert parsed["options"] == ["Apple", "Banana", "Orange"]
        assert parsed["multiple_answers"] is True
        assert parsed["votes"] == []

        # Single choice: selectable_count == 1
        single_blob = build_ios_poll_blob(
            question="Primary language?",
            options=["Python", "Rust"],
            selectable_count=1,
        )
        parsed_s = _parse_ios_poll_blob(single_blob)
        assert parsed_s is not None
        assert parsed_s["multiple_answers"] is False

    def test_poll_with_unpacked_and_packed_votes(self) -> None:
        votes = [
            {
                "option_indices": [0],
                "client_ts": 1729000100,
                "stanza_id": "VOTE_STANZA_1",
                "voter_jid": "5550100@s.whatsapp.net",
                "server_ts": 1729000105,
            },
            {
                "option_indices": [1, 2],
                "client_ts": 1729000200,
                "stanza_id": "VOTE_STANZA_2",
                "voter_jid": "5550200@s.whatsapp.net",
                "server_ts": 1729000210,
            },
        ]
        blob = build_ios_poll_blob("Lunch vote?", ["Pizza", "Sushi", "Salad"], 0, votes=votes)

        # Inject a packed varint vote entry for test coverage of packed repeated wire 2
        # Vote subfield 1 with wire 2 contains concatenated varints b'\x00\x02'
        packed_opt_bytes = encode_varint(0) + encode_varint(2)
        packed_vote_inner = (
            build_field(1, 2, packed_opt_bytes)
            + build_field(3, 2, b"PACKED_VOTE")
            + build_field(4, 2, b"5550300@s.whatsapp.net")
        )
        packed_poll_field8 = build_field(
            8,
            2,
            build_field(2, 2, b"Packed poll")
            + build_field(5, 2, packed_vote_inner)
            + build_field(9, 1, b"\x00" * 8)  # wire 1 skip test
            + build_field(10, 5, b"\x00" * 4),  # wire 5 skip test
        )
        parsed_packed = _parse_ios_poll_blob(packed_poll_field8)
        assert parsed_packed is not None
        assert len(parsed_packed["votes"]) == 1
        assert parsed_packed["votes"][0]["option_indices"] == [0, 2]
        assert parsed_packed["votes"][0]["voter_jid"] == "5550300@s.whatsapp.net"

        # Check standard votes
        parsed = _parse_ios_poll_blob(blob)
        assert parsed is not None
        assert len(parsed["votes"]) == 2
        assert parsed["votes"][0]["option_indices"] == [0]
        assert parsed["votes"][0]["stanza_id"] == "VOTE_STANZA_1"
        assert parsed["votes"][1]["option_indices"] == [1, 2]
        assert parsed["votes"][1]["voter_jid"] == "5550200@s.whatsapp.net"

    def test_corrupt_poll_payloads_do_not_crash(self) -> None:
        # Truncated field 8 returns a clean empty poll dictionary or None without crashing
        tag_f8 = (8 << 3) | 2
        corrupted = encode_varint(tag_f8) + encode_varint(50) + b"\x01\x02"
        res = _parse_ios_poll_blob(corrupted)
        assert res is None or res.get("question") == ""


class TestProtobufParser:
    """Tests for _parse_protobuf general list-of-triples decoder."""

    def test_decode_all_wire_types(self) -> None:
        data = (
            build_field(1, 0, 999)  # varint
            + build_field(2, 1, b"12345678")  # fixed64
            + build_field(3, 2, b"sample string")  # length-delimited
            + build_field(4, 5, b"1234")  # fixed32
        )
        parsed = _parse_protobuf(data)
        field_map = {f: (w, v) for f, w, v in parsed}
        assert field_map[1] == ("varint", 999)
        assert field_map[2][0] == "fixed64"
        assert field_map[3] == ("len", b"sample string")

    def test_corrupt_stream_terminates_gracefully(self) -> None:
        # Invalid wire type (e.g. wire type 7)
        invalid_wire_tag = (5 << 3) | 7
        data = encode_varint(invalid_wire_tag) + b"\x01\x02"
        parsed = _parse_protobuf(data)
        assert parsed == []


class TestReactionsParsing:
    """Tests for iOS reactions parsing from ZRECEIPTINFO."""

    def test_extract_ios_reactions_and_reactors(self) -> None:
        entries = [
            {"token": "MSG1", "phone": "5550100", "emoji": "👍".encode(), "from_me": False},
            {"token": "MSG2", "lid": "999888", "emoji": "❤️".encode(), "from_me": True},
        ]
        blob = make_ios_reaction_blob(entries)

        summary = _extract_ios_reactions(blob)
        summary_emojis = [r[1] for r in summary]
        assert "👍" in summary_emojis
        assert "❤️" in summary_emojis

        reactors = _extract_ios_reaction_reactors(blob)
        assert len(reactors) == 2
        assert reactors[0]["emoji"] == "👍"
        assert reactors[0]["phone"] == "5550100"

        assert reactors[1]["emoji"] == "❤️"
        assert reactors[1]["lid"] == "999888@lid"

    def test_scan_emojis_varieties(self) -> None:
        raw_bytes = "Hello 👍 and 🎉 test 😀".encode()
        emoji_hexes = _scan_emojis(raw_bytes)
        emojis = [bytes.fromhex(h).decode("utf-8") for h in emoji_hexes]
        assert "👍" in emojis
        assert "🎉" in emojis
        assert "😀" in emojis


class TestQuotedInfoExtraction:
    """Tests for _extract_ios_quoted_info across all media and message types."""

    def test_none_or_empty_metadata(self) -> None:
        assert _extract_ios_quoted_info(None) is None
        assert _extract_ios_quoted_info(b"") is None
        assert _extract_ios_quoted_info("not bytes") is None  # type: ignore[arg-type]

    def test_quoted_text_message(self) -> None:
        sub_msg = build_field(1, 2, b"Quoted text content")
        metadata = (
            build_field(5, 2, b"STANZA_Q_001")
            + build_field(6, 2, b"5550100@s.whatsapp.net")
            + build_field(19, 2, sub_msg)
        )
        res = _extract_ios_quoted_info(metadata)
        assert res is not None
        assert res["quoted_stanza_id"] == "STANZA_Q_001"
        assert res["quoted_sender"] == "5550100@s.whatsapp.net"
        assert res["quoted_text"] == "Quoted text content"
        assert res["quoted_has_thumb"] is False

    def test_quoted_photo_with_caption(self) -> None:
        img_payload = build_field(3, 2, b"Sunny day at the beach")
        sub_msg = build_field(3, 2, img_payload)
        metadata = (
            build_field(5, 2, b"STANZA_Q_002")
            + build_field(6, 2, b"5550100@s.whatsapp.net")
            + build_field(19, 2, sub_msg)
        )
        res = _extract_ios_quoted_info(metadata)
        assert res is not None
        assert res["quoted_text"] == "📷 Sunny day at the beach"
        assert res["quoted_has_thumb"] is True

    def test_quoted_photo_without_caption(self) -> None:
        img_payload = build_field(1, 2, b"https://example.com/img.enc")
        sub_msg = build_field(3, 2, img_payload)
        metadata = build_field(5, 2, b"STANZA_Q_003") + build_field(19, 2, sub_msg)
        res = _extract_ios_quoted_info(metadata)
        assert res is not None
        assert res["quoted_text"] == "📷 Photo"
        assert res["quoted_has_thumb"] is True

    def test_quoted_audio_and_video(self) -> None:
        # Audio
        sub_aud = build_field(8, 2, b"audio_data")
        res_aud = _extract_ios_quoted_info(
            build_field(5, 2, b"STANZA_A") + build_field(19, 2, sub_aud)
        )
        assert res_aud is not None
        assert res_aud["quoted_text"] == "🎵 Audio"
        assert res_aud["quoted_has_thumb"] is False

        # Video
        sub_vid = build_field(9, 2, b"video_data")
        res_vid = _extract_ios_quoted_info(
            build_field(5, 2, b"STANZA_V") + build_field(19, 2, sub_vid)
        )
        assert res_vid is not None
        assert res_vid["quoted_text"] == "🎥 Video"
        assert res_vid["quoted_has_thumb"] is True

    def test_quoted_document_and_sticker(self) -> None:
        # Document
        sub_doc = build_field(7, 2, b"doc_data")
        res_doc = _extract_ios_quoted_info(
            build_field(5, 2, b"STANZA_D") + build_field(19, 2, sub_doc)
        )
        assert res_doc is not None
        assert res_doc["quoted_text"] == "📄 Document"

        # Sticker
        sub_stk = build_field(26, 2, b"sticker_data")
        res_stk = _extract_ios_quoted_info(
            build_field(5, 2, b"STANZA_S") + build_field(19, 2, sub_stk)
        )
        assert res_stk is not None
        assert res_stk["quoted_text"] == "Sticker"
        assert res_stk["quoted_has_thumb"] is True

    def test_quoted_extended_text_with_preview_and_thumbnail(self) -> None:
        ext_inner = (
            build_field(1, 2, b"Article Title")
            + build_field(2, 2, b"Fallback Matched Text")
            + build_field(16, 2, b"\xff\xd8\xff\xe0thumb")
            + build_field(20, 1, b"\x01" * 8)  # ewire 1
            + build_field(21, 5, b"\x02" * 4)  # ewire 5
        )
        sub_msg = (
            build_field(6, 2, ext_inner)
            + build_field(30, 1, b"\x03" * 8)  # swire 1
            + build_field(31, 5, b"\x04" * 4)  # swire 5
        )
        metadata = (
            build_field(5, 2, b"STANZA_EXT")
            + build_field(19, 2, sub_msg)
            + build_field(40, 1, b"\x05" * 8)  # top wire 1
            + build_field(41, 5, b"\x06" * 4)  # top wire 5
        )
        res = _extract_ios_quoted_info(metadata)
        assert res is not None
        assert res["quoted_text"] == "Article Title"
        assert res["quoted_has_thumb"] is True

    def test_quoted_photo_with_noise_wires(self) -> None:
        # Photo with wire 1 and wire 5 noise
        img_inner = (
            build_field(3, 2, b"Photo with noise")
            + build_field(20, 1, b"\x01" * 8)
            + build_field(21, 5, b"\x02" * 4)
        )
        sub_msg = build_field(3, 2, img_inner)
        metadata = build_field(5, 2, b"STANZA_P") + build_field(19, 2, sub_msg)
        res = _extract_ios_quoted_info(metadata)
        assert res is not None
        assert res["quoted_text"] == "📷 Photo with noise"
        assert res["quoted_has_thumb"] is True


class TestLinkDescriptionAndMentions:
    """Tests for link descriptions and mention cascades."""

    def test_extract_ios_link_description(self) -> None:
        assert _extract_ios_link_description(None) is None
        assert _extract_ios_link_description(b"") is None

        # Clean description
        valid_desc = build_field(3, 2, b"Open source software documentation and guides")
        assert (
            _extract_ios_link_description(valid_desc)
            == "Open source software documentation and guides"
        )

        # Ignores http prefixes
        http_desc = build_field(3, 2, b"https://example.com/article")
        assert _extract_ios_link_description(http_desc) is None

        # Ignores NUL and JPEG SOI bytes
        nul_desc = build_field(3, 2, b"Desc with \x00 null byte")
        assert _extract_ios_link_description(nul_desc) is None

        jpg_desc = build_field(3, 2, b"\xff\xd8\xff\xe0binary image data")
        assert _extract_ios_link_description(jpg_desc) is None

    def test_iter_wire_fields_skips_and_limits(self) -> None:
        data = (
            build_field(1, 0, 42)
            + build_field(2, 1, b"12345678")
            + build_field(3, 5, b"1234")
            + build_field(4, 2, b"test")
        )
        fields = _iter_wire_fields(data)
        assert len(fields) == 4
        assert fields[0] == (1, 0, 42)
        assert fields[1] == (2, 1, b"12345678")
        assert fields[2] == (3, 5, b"1234")
        assert fields[3] == (4, 2, b"test")

        # Truncated wire 1
        tag_wire1 = (5 << 3) | 1
        truncated_w1 = encode_varint(tag_wire1) + b"\x01\x02"
        assert _iter_wire_fields(truncated_w1) == []

    def test_extract_ios_mentions_direct_and_nested_in_quote(self) -> None:
        assert _extract_ios_mentions(None) == []
        assert _extract_ios_mentions(b"") == []

        # Direct root Tag 8 and Tag 15 mentions
        direct_mentions = build_field(8, 2, b"5550100@s.whatsapp.net") + build_field(
            15, 2, b"5550200@s.whatsapp.net"
        )
        extracted = _extract_ios_mentions(direct_mentions)
        assert extracted == ["5550100@s.whatsapp.net", "5550200@s.whatsapp.net"]

        # Nested in Tag 19 quoted message -> imageMessage (Tag 3) -> ContextInfo (Tag 17) -> Tag 15
        nested_ctx = _extract_context_mentioned_jids(
            build_field(15, 2, b"5550300@s.whatsapp.net")
            + build_field(8, 2, b"5550400@s.whatsapp.net")
        )
        assert nested_ctx == ["5550300@s.whatsapp.net", "5550400@s.whatsapp.net"]

        inner_ctx_bytes = build_field(15, 2, b"5550500@s.whatsapp.net")
        img_msg = build_field(17, 2, inner_ctx_bytes)
        q_msg = build_field(3, 2, img_msg)
        top_meta = build_field(19, 2, q_msg)
        assert _extract_ios_mentions(top_meta) == ["5550500@s.whatsapp.net"]
