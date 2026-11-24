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

    def test_outer_wire_skips_and_unknown_wire(self) -> None:
        # Outer wire 0, wire 1, wire 5, unknown wire 3
        sub_desc = build_field(3, 2, b"Group Topic")
        field1 = build_field(1, 2, sub_desc)
        unknown_wire = encode_varint((99 << 3) | 3) + b"\x01"
        top = (
            build_field(10, 0, 50)
            + build_field(11, 1, b"\x01" * 8)
            + build_field(12, 5, b"\x02" * 4)
            + field1
            + unknown_wire
        )
        b64 = base64.b64encode(top).decode("ascii")
        assert _extract_ios_group_description(b64) == "Group Topic"

    def test_outer_truncations(self) -> None:
        # Outer tag varint truncated
        b64_tag = base64.b64encode(b"\x80").decode("ascii")
        assert _extract_ios_group_description(b64_tag) is None

        # Outer wire 0 val truncated
        tag_w0 = encode_varint((10 << 3) | 0) + b"\x80"
        b64_w0 = base64.b64encode(tag_w0).decode("ascii")
        assert _extract_ios_group_description(b64_w0) is None

        # Outer wire 2 len truncated
        tag_w2 = encode_varint((1 << 3) | 2) + b"\x80"
        b64_w2 = base64.b64encode(tag_w2).decode("ascii")
        assert _extract_ios_group_description(b64_w2) is None

    def test_subfield_wire_skips_and_unknown_wire(self) -> None:
        # Subfields inside field 1: wire 0, 1, 5, and unknown wire 3
        unknown_sub = encode_varint((99 << 3) | 3) + b"\x01"
        sub_desc = (
            build_field(10, 0, 50)
            + build_field(11, 1, b"\x01" * 8)
            + build_field(12, 5, b"\x02" * 4)
            + build_field(3, 2, b"Verified Subfield Desc")
            + unknown_sub
        )
        top = build_field(1, 2, sub_desc)
        b64 = base64.b64encode(top).decode("ascii")
        assert _extract_ios_group_description(b64) == "Verified Subfield Desc"

    def test_subfield_truncations(self) -> None:
        # Subfield tag truncated
        top_tag = build_field(1, 2, b"\x80")
        assert _extract_ios_group_description(base64.b64encode(top_tag).decode("ascii")) is None

        # Subfield wire 0 val truncated
        top_w0 = build_field(1, 2, encode_varint((5 << 3) | 0) + b"\x80")
        assert _extract_ios_group_description(base64.b64encode(top_w0).decode("ascii")) is None

        # Subfield wire 2 len truncated
        top_w2 = build_field(1, 2, encode_varint((3 << 3) | 2) + b"\x80")
        assert _extract_ios_group_description(base64.b64encode(top_w2).decode("ascii")) is None

    def test_subfield_and_outer_unknown_wire_branches(self) -> None:
        # Exercises line 84 (inner unknown wire break) and line 88 (outer unknown wire break)
        sub_unknown = encode_varint((99 << 3) | 3) + b"\x01"
        top = (
            build_field(1, 2, sub_unknown)  # sub_offset encounters wire 3 -> breaks on line 84
            + encode_varint((88 << 3) | 3)  # outer offset encounters wire 3 -> breaks on line 88
            + b"\x01"
        )
        b64 = base64.b64encode(top).decode("ascii")
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

    def test_outer_wires_and_truncations(self) -> None:
        # Outer wire 0, 1, 5, unknown wire 3
        poll_inner = build_field(2, 2, b"Valid Question")
        field8 = build_field(8, 2, poll_inner)
        data = (
            build_field(1, 0, 10)
            + build_field(2, 1, b"\x01" * 8)
            + build_field(3, 5, b"\x02" * 4)
            + field8
            + encode_varint((9 << 3) | 3)
            + b"\x00"
        )
        parsed = _parse_ios_poll_blob(data)
        assert parsed is not None and parsed["question"] == "Valid Question"

        # Outer tag truncated
        assert _parse_ios_poll_blob(b"\x80") is None

        # Outer wire 0 val truncated
        assert _parse_ios_poll_blob(encode_varint((1 << 3) | 0) + b"\x80") is None

        # Outer wire 2 len truncated
        assert _parse_ios_poll_blob(encode_varint((8 << 3) | 2) + b"\x80") is None

    def test_poll_blob_sub_truncations_and_unknown_wire(self) -> None:
        # Poll blob tag truncated
        res_tag = _parse_ios_poll_blob(build_field(8, 2, b"\x80"))
        assert res_tag is not None and res_tag["question"] == ""

        # Poll blob wire 0 val truncated (lines 145-146)
        res_w0 = _parse_ios_poll_blob(build_field(8, 2, encode_varint((4 << 3) | 0) + b"\x80"))
        assert res_w0 is not None and res_w0["question"] == ""

        # Poll blob wire 2 len truncated (lines 154-155)
        res_w2 = _parse_ios_poll_blob(build_field(8, 2, encode_varint((2 << 3) | 2) + b"\x80"))
        assert res_w2 is not None and res_w2["question"] == ""

        # Poll blob unknown wire 3 (line 253), wire 1, wire 5
        poll_inner = (
            build_field(2, 2, b"Q?")
            + build_field(10, 1, b"\x00" * 8)
            + build_field(11, 5, b"\x00" * 4)
            + encode_varint((12 << 3) | 3)
            + b"\x01"
        )
        res_unknown = _parse_ios_poll_blob(build_field(8, 2, poll_inner))
        assert res_unknown is not None and res_unknown["question"] == "Q?"

    def test_poll_option_inner_wires_and_truncations(self) -> None:
        # Option wire 0, 1, 5, unknown wire 3
        opt_inner = (
            build_field(10, 0, 42)
            + build_field(11, 1, b"\x01" * 8)
            + build_field(12, 5, b"\x02" * 4)
            + build_field(1, 2, b"Clean Option")
            + encode_varint((13 << 3) | 3)
            + b"\x01"
        )
        poll_blob = build_field(2, 2, b"Question?") + build_field(3, 2, opt_inner)
        res = _parse_ios_poll_blob(build_field(8, 2, poll_blob))
        assert res is not None and res["options"] == ["Clean Option"]

        # Option tag truncated
        poll_opt_trunc_tag = build_field(3, 2, b"\x80")
        assert _parse_ios_poll_blob(build_field(8, 2, poll_opt_trunc_tag)) is not None

        # Option wire 0 val truncated
        poll_opt_trunc_w0 = build_field(3, 2, encode_varint((10 << 3) | 0) + b"\x80")
        assert _parse_ios_poll_blob(build_field(8, 2, poll_opt_trunc_w0)) is not None

        # Option wire 2 len truncated
        poll_opt_trunc_w2 = build_field(3, 2, encode_varint((1 << 3) | 2) + b"\x80")
        assert _parse_ios_poll_blob(build_field(8, 2, poll_opt_trunc_w2)) is not None

    def test_poll_vote_inner_wires_and_truncations(self) -> None:
        # Vote containing server_ts (wire 0, v_num=5), wire 1, wire 5, unknown wire 3
        vote_inner = (
            build_field(1, 0, 0)  # option index
            + build_field(2, 0, 1729000100)  # client_ts
            + build_field(5, 0, 1729000200)  # server_ts
            + build_field(10, 1, b"\x01" * 8)  # wire 1
            + build_field(11, 5, b"\x02" * 4)  # wire 5
            + build_field(3, 2, b"STANZA_VOTE")
            + build_field(4, 2, b"5550100@s.whatsapp.net")
            + encode_varint((12 << 3) | 3)
            + b"\x01"  # unknown wire
        )
        poll_blob = build_field(2, 2, b"Q?") + build_field(5, 2, vote_inner)
        res = _parse_ios_poll_blob(build_field(8, 2, poll_blob))
        assert res is not None
        assert len(res["votes"]) == 1
        assert res["votes"][0]["server_ts"] == 1729000200
        assert res["votes"][0]["stanza_id"] == "STANZA_VOTE"

        # Vote tag truncated
        poll_vote_trunc_tag = build_field(5, 2, b"\x80")
        assert _parse_ios_poll_blob(build_field(8, 2, poll_vote_trunc_tag)) is not None

        # Vote wire 0 val truncated
        poll_vote_trunc_w0 = build_field(5, 2, encode_varint((2 << 3) | 0) + b"\x80")
        assert _parse_ios_poll_blob(build_field(8, 2, poll_vote_trunc_w0)) is not None

        # Vote wire 2 len truncated
        poll_vote_trunc_w2 = build_field(5, 2, encode_varint((3 << 3) | 2) + b"\x80")
        assert _parse_ios_poll_blob(build_field(8, 2, poll_vote_trunc_w2)) is not None

        # Vote packed option indices truncated
        poll_vote_trunc_packed = build_field(
            5, 2, encode_varint((1 << 3) | 2) + encode_varint(1) + b"\x80"
        )
        assert _parse_ios_poll_blob(build_field(8, 2, poll_vote_trunc_packed)) is not None


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

    def test_truncated_varint_catches_exception(self) -> None:
        # Truncated varint tag triggers ValueError caught by lines 292-293
        assert _parse_protobuf(b"\x80") == []


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

    def test_scan_emojis_vs16_without_preceding_emoji(self) -> None:
        # Starts immediately with Variation Selector 16 (\xef\xb8\x8f)
        assert _scan_emojis(b"\xef\xb8\x8f\xef\xb8\x8f") == []

    def test_reactions_missing_field_7(self) -> None:
        blob = build_field(1, 2, b"other data")
        assert _extract_ios_reactions(blob) == []
        assert _extract_ios_reaction_reactors(blob) == []

    def test_reactions_skips_non_len_subfields(self) -> None:
        # Field 7 contains a wire 0 subfield alongside a valid reactor entry (covers line 355)
        valid_reactor = build_field(1, 2, b"5550100") + build_field(2, 2, "🔥".encode())
        field7_data = (
            build_field(99, 0, 123)  # ew != "len"
            + build_field(1, 2, valid_reactor)
        )
        receipt = build_field(7, 2, field7_data)
        reactions = _extract_ios_reactions(receipt)
        assert len(reactions) == 1
        assert reactions[0][1] == "🔥"

    def test_reaction_reactors_skips_non_len_and_missing_emojis(self) -> None:
        # 1. ew != "len" in field 7
        # 2. reactor with no emojis
        # 3. reactor with wire 0 inside (sw != "len")
        # 4. reactor with both phone and lid
        # 5. reactor with neither (self-reaction)
        reactor_no_emoji = build_field(1, 2, b"token_only")
        reactor_with_wire0 = (
            build_field(2, 2, "🎉".encode())
            + build_field(10, 0, 999)  # sw != "len"
            + build_field(3, 2, b"5550100@s.whatsapp.net")
            + build_field(4, 2, b"100000000000001@lid")
        )
        self_reactor = build_field(2, 2, "💙".encode())

        field7_data = (
            build_field(99, 0, 100)  # ew != "len"
            + build_field(1, 2, reactor_no_emoji)
            + build_field(2, 2, reactor_with_wire0)
            + build_field(3, 2, self_reactor)
        )
        receipt = build_field(7, 2, field7_data)
        reactors = _extract_ios_reaction_reactors(receipt)
        assert len(reactors) == 2

        assert reactors[0]["emoji"] == "🎉"
        assert reactors[0]["phone"] == "5550100"
        assert reactors[0]["lid"] == "100000000000001@lid"

        assert reactors[1]["emoji"] == "💙"
        assert reactors[1]["phone"] is None
        assert reactors[1]["lid"] is None


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

    def test_top_level_wires_and_overruns(self) -> None:
        # Multi-byte varint wire 0 at top level and length >= 128 (covers line 442)
        wire0_multibyte = encode_varint((10 << 3) | 0) + b"\x80\x01"
        dummy_130_bytes = build_field(20, 2, b"x" * 130)
        unknown_top = encode_varint((11 << 3) | 3) + b"\x00"
        metadata = (
            wire0_multibyte
            + dummy_130_bytes
            + build_field(5, 2, b"STANZA_TOP")
            + build_field(19, 2, build_field(1, 2, b"Text"))
            + unknown_top
        )
        res = _extract_ios_quoted_info(metadata)
        assert res is not None and res["quoted_stanza_id"] == "STANZA_TOP"

        # Wire 2 length overrun at top level
        overrun_top = encode_varint((12 << 3) | 2) + encode_varint(100) + b"short"
        assert _extract_ios_quoted_info(overrun_top) is None

    def test_sub_message_wires_and_overruns(self) -> None:
        # Sub-message inside field 19: multi-byte wire 0, length >= 128 (covers line 481), unknown wire 3
        sub_payload = (
            encode_varint((10 << 3) | 0)
            + b"\x80\x01"
            + build_field(1, 2, b"x" * 130)
            + encode_varint((11 << 3) | 3)
            + b"\x00"
        )
        metadata = build_field(5, 2, b"STANZA_SUB") + build_field(19, 2, sub_payload)
        res = _extract_ios_quoted_info(metadata)
        assert res is not None and res["quoted_text"] == "x" * 130

        # Sub-message wire 2 overrun
        sub_overrun = encode_varint((12 << 3) | 2) + encode_varint(100) + b"short"
        metadata_overrun = build_field(5, 2, b"STANZA_OVER") + build_field(19, 2, sub_overrun)
        res_o = _extract_ios_quoted_info(metadata_overrun)
        assert res_o is not None and res_o["quoted_text"] == ""

    def test_extended_text_wires_and_overruns(self) -> None:
        # Extended text (field 6): multi-byte wire 0, unknown wire 3, length >= 128 (covers line 519)
        # Also test efield == 2 fallback both when efield == 1 exists and when it doesn't
        ext_with_both = (
            encode_varint((10 << 3) | 0)
            + b"\x80\x01"
            + build_field(1, 2, b"Primary Text " + b"x" * 130)
            + build_field(2, 2, b"Fallback Matched Text")
            + encode_varint((11 << 3) | 3)
            + b"\x00"
        )
        meta_both = build_field(5, 2, b"STANZA_EB") + build_field(
            19, 2, build_field(6, 2, ext_with_both)
        )
        res_b = _extract_ios_quoted_info(meta_both)
        assert res_b is not None and res_b["quoted_text"].startswith("Primary Text")

        # efield == 2 fallback alone (efield == 1 absent)
        ext_fallback = build_field(2, 2, b"Only Fallback Text")
        meta_fb = build_field(5, 2, b"STANZA_EFB") + build_field(
            19, 2, build_field(6, 2, ext_fallback)
        )
        res_fb = _extract_ios_quoted_info(meta_fb)
        assert res_fb is not None and res_fb["quoted_text"] == "Only Fallback Text"

        # Extended text wire 2 overrun
        ext_overrun = encode_varint((12 << 3) | 2) + encode_varint(100) + b"short"
        meta_eo = build_field(5, 2, b"STANZA_EO") + build_field(
            19, 2, build_field(6, 2, ext_overrun)
        )
        res_eo = _extract_ios_quoted_info(meta_eo)
        assert res_eo is not None and res_eo["quoted_text"] == ""

    def test_image_message_wires_and_overruns(self) -> None:
        # Image message (field 3): multi-byte wire 0, unknown wire 3, length >= 128 (covers line 567)
        img_inner = (
            encode_varint((10 << 3) | 0)
            + b"\x80\x01"
            + build_field(3, 2, b"Caption Here " + b"x" * 130)
            + encode_varint((11 << 3) | 3)
            + b"\x00"
        )
        meta_img = build_field(5, 2, b"STANZA_IMG") + build_field(
            19, 2, build_field(3, 2, img_inner)
        )
        res_i = _extract_ios_quoted_info(meta_img)
        assert res_i is not None and res_i["quoted_text"].startswith("📷 Caption Here")

        # Image message wire 2 overrun
        img_overrun = encode_varint((12 << 3) | 2) + encode_varint(100) + b"short"
        meta_io = build_field(5, 2, b"STANZA_IO") + build_field(
            19, 2, build_field(3, 2, img_overrun)
        )
        res_io = _extract_ios_quoted_info(meta_io)
        assert res_io is not None and res_io["quoted_text"] == "📷 Photo"

    def test_quoted_info_exception_handling(self) -> None:
        # Trigger Exception catch in _extract_ios_quoted_info (lines 611-613)
        class FaultyBytes(bytes):
            def __len__(self) -> int:
                return 10

            def __getitem__(self, item: object) -> object:
                raise RuntimeError("Synthetic simulated extraction crash")

        assert _extract_ios_quoted_info(FaultyBytes()) is None


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

    def test_link_description_wires_and_overruns(self) -> None:
        # Wire 0 multi-byte varint, tag >= 128 (line 632), length >= 128 (line 648), wire 1, wire 5
        data = (
            encode_varint((1 << 3) | 0)
            + b"\x80\x01"
            + build_field(20, 2, b"x" * 130)  # tag 20 << 3 = 160 >= 128, length 130 >= 128
            + build_field(2, 1, b"\x01" * 8)
            + build_field(4, 5, b"\x02" * 4)
            + build_field(3, 2, b"Valid Link Title")
        )
        assert _extract_ios_link_description(data) == "Valid Link Title"

        # Wire 2 length overrun
        overrun_data = encode_varint((3 << 3) | 2) + encode_varint(100) + b"short"
        assert _extract_ios_link_description(overrun_data) is None

        # Unknown wire type (line 663)
        unknown_link = encode_varint((5 << 3) | 3) + b"\x00"
        assert _extract_ios_link_description(unknown_link) is None

    def test_link_description_exception_handling(self) -> None:
        class FaultyLinkBytes(bytes):
            def __len__(self) -> int:
                return 10

            def __getitem__(self, item: object) -> object:
                raise RuntimeError("Synthetic simulated link crash")

        assert _extract_ios_link_description(FaultyLinkBytes()) is None

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

    def test_iter_wire_fields_all_truncation_branches(self) -> None:
        # Truncated tag varint
        assert _iter_wire_fields(b"\x80") == []

        # Truncated wire 0 val varint
        assert _iter_wire_fields(encode_varint((1 << 3) | 0) + b"\x80") == []

        # Truncated wire 2 length varint
        assert _iter_wire_fields(encode_varint((1 << 3) | 2) + b"\x80") == []

        # Wire 2 length overrun
        assert _iter_wire_fields(encode_varint((1 << 3) | 2) + encode_varint(100) + b"short") == []

        # Truncated wire 5 (<4 bytes)
        assert _iter_wire_fields(encode_varint((1 << 3) | 5) + b"\x01\x02") == []

        # Unknown wire type (wire 3)
        assert _iter_wire_fields(encode_varint((1 << 3) | 3) + b"\x01") == []

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

    def test_extract_ios_mentions_skips_non_wire2_and_deduplicates(self) -> None:
        # Tag 19 containing non-wire-2 fields (covers line 737: q_wire != 2)
        inner_ctx = build_field(15, 2, b"5550100@s.whatsapp.net")
        container_msg = build_field(14, 2, inner_ctx)  # ContextInfo tag 14
        # Test container types: 18, 20, 26, 42, 45, etc.
        video_msg = build_field(20, 2, container_msg)

        non_wire2_quote = (
            build_field(1, 0, 999)  # q_wire == 0
            + build_field(2, 1, b"\x01" * 8)  # q_wire == 1
            + video_msg
        )
        top = (
            build_field(19, 2, non_wire2_quote)
            + build_field(8, 2, b"5550100@s.whatsapp.net")  # duplicate mention
        )
        mentions = _extract_ios_mentions(top)
        assert mentions == ["5550100@s.whatsapp.net"]
