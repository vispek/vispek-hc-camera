# SPDX-License-Identifier: Apache-2.0
"""Light-source serial protocol: official vectors, reply scanning, property tests."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vispek_hc.protocol import (
    Command,
    Function,
    Reply,
    ReplyScanner,
    Verdict,
    crc8,
    encode_off,
    encode_on,
    encode_reply,
    judge,
    parse_command,
)

# Worked examples from the Vispek protocol document (sheet 1).
ON_1_PWM_500 = bytes.fromhex("3d 01 03 01 01 f4 83 0d 0a")
ON_5_PWM_1023 = bytes.fromhex("3d 01 03 05 03 ff 33 0d 0a")
OFF_1 = bytes.fromhex("3d 02 01 01 93 0d 0a")
OFF_5 = bytes.fromhex("3d 02 01 05 8f 0d 0a")
REPLY_ON_OK = bytes.fromhex("3d 01 01 00 29 0d 0a")
REPLY_OFF_OK = bytes.fromhex("3d 02 01 00 94 0d 0a")

# Routine sheet of the same document: LED 1-17, ON at PWM 1023 then OFF (34 frames).
ROUTINE = [
    (1, "3d 01 03 01 03 ff 98 0d 0a", "3d 02 01 01 93 0d 0a"),
    (2, "3d 01 03 02 03 ff 25 0d 0a", "3d 02 01 02 9a 0d 0a"),
    (3, "3d 01 03 03 03 ff 4e 0d 0a", "3d 02 01 03 9d 0d 0a"),
    (4, "3d 01 03 04 03 ff 58 0d 0a", "3d 02 01 04 88 0d 0a"),
    (5, "3d 01 03 05 03 ff 33 0d 0a", "3d 02 01 05 8f 0d 0a"),
    (6, "3d 01 03 06 03 ff 8e 0d 0a", "3d 02 01 06 86 0d 0a"),
    (7, "3d 01 03 07 03 ff e5 0d 0a", "3d 02 01 07 81 0d 0a"),
    (8, "3d 01 03 08 03 ff a2 0d 0a", "3d 02 01 08 ac 0d 0a"),
    (9, "3d 01 03 09 03 ff c9 0d 0a", "3d 02 01 09 ab 0d 0a"),
    (10, "3d 01 03 0a 03 ff 74 0d 0a", "3d 02 01 0a a2 0d 0a"),
    (11, "3d 01 03 0b 03 ff 1f 0d 0a", "3d 02 01 0b a5 0d 0a"),
    (12, "3d 01 03 0c 03 ff 09 0d 0a", "3d 02 01 0c b0 0d 0a"),
    (13, "3d 01 03 0d 03 ff 62 0d 0a", "3d 02 01 0d b7 0d 0a"),
    (14, "3d 01 03 0e 03 ff df 0d 0a", "3d 02 01 0e be 0d 0a"),
    (15, "3d 01 03 0f 03 ff b4 0d 0a", "3d 02 01 0f b9 0d 0a"),
    (16, "3d 01 03 10 03 ff 51 0d 0a", "3d 02 01 10 e4 0d 0a"),
    (17, "3d 01 03 11 03 ff 3a 0d 0a", "3d 02 01 11 e3 0d 0a"),
]


def reference_crc8(data: bytes) -> int:
    """Bitwise transcription of the C reference in the Vispek protocol document."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def scan(data: bytes) -> list[Reply]:
    return ReplyScanner().feed(data)


# --- framing ---------------------------------------------------------------------------


def test_on_frames_match_the_official_examples() -> None:
    assert encode_on(1, 500) == ON_1_PWM_500
    assert encode_on(5, 1023) == ON_5_PWM_1023


def test_off_frames_match_the_official_examples() -> None:
    assert encode_off(1) == OFF_1
    assert encode_off(5) == OFF_5


def test_reply_frames_match_the_official_examples() -> None:
    assert encode_reply(Function.ON) == REPLY_ON_OK
    assert encode_reply(Function.OFF) == REPLY_OFF_OK


@pytest.mark.parametrize(("led_id", "on_hex", "off_hex"), ROUTINE)
def test_routine_sheet_frames_are_byte_identical(led_id: int, on_hex: str, off_hex: str) -> None:
    assert encode_on(led_id, 1023) == bytes.fromhex(on_hex)
    assert encode_off(led_id) == bytes.fromhex(off_hex)


def test_routine_sheet_has_34_frames() -> None:
    assert 2 * len(ROUTINE) == 34
    assert [row[0] for row in ROUTINE] == list(range(1, 18))


@pytest.mark.parametrize("led_id", [0, 23, -1, 256])
def test_led_id_outside_protocol_range_is_refused(led_id: int) -> None:
    with pytest.raises(ValueError, match="LED id"):
        encode_on(led_id, 100)
    with pytest.raises(ValueError, match="LED id"):
        encode_off(led_id)


@pytest.mark.parametrize("pwm", [-1, 1024, 65536])
def test_pwm_outside_range_is_refused(pwm: int) -> None:
    with pytest.raises(ValueError, match="PWM"):
        encode_on(5, pwm)


def test_protocol_range_reaches_led_22_and_pwm_zero() -> None:
    assert parse_command(encode_on(22, 0)) == Command(Function.ON, 22, 0)
    assert parse_command(encode_off(22)) == Command(Function.OFF, 22, None)


# --- CRC -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("covered_hex", "crc"),
    [
        ("3d 01 03 01 01 f4", 0x83),
        ("3d 01 03 05 03 ff", 0x33),
        ("3d 02 01 01", 0x93),
        ("3d 02 01 05", 0x8F),
        ("3d 01 01 00", 0x29),
        ("3d 02 01 00", 0x94),
        ("", 0x00),
    ],
)
def test_crc8_known_values(covered_hex: str, crc: int) -> None:
    assert crc8(bytes.fromhex(covered_hex)) == crc


def test_crc8_matches_reference_for_every_single_byte() -> None:
    for value in range(256):
        assert crc8(bytes([value])) == reference_crc8(bytes([value]))


@given(st.binary(max_size=64))
def test_crc8_matches_reference_for_arbitrary_data(data: bytes) -> None:
    assert crc8(data) == reference_crc8(data)


# --- command parsing -------------------------------------------------------------------


def test_parse_command_reads_the_official_examples() -> None:
    assert parse_command(ON_1_PWM_500) == Command(Function.ON, 1, 500)
    assert parse_command(OFF_5) == Command(Function.OFF, 5, None)


@pytest.mark.parametrize(
    "frame_hex",
    [
        "",
        "3d 02 01 01 93 0d",  # truncated
        "3e 02 01 01 93 0d 0a",  # wrong head
        "3d 03 01 01 93 0d 0a",  # unknown function
        "3d 02 03 01 93 0d 0a",  # length does not fit the function
        "3d 02 01 01 94 0d 0a",  # CRC
        "3d 02 01 01 93 0a 0d",  # tail
        "3d 02 01 00 94 0d 0a",  # LED 0
        "3d 01 03 05 04 00 ab 0d 0a",  # PWM 1024, CRC valid
    ],
)
def test_parse_command_rejects_malformed_frames(frame_hex: str) -> None:
    with pytest.raises(ValueError, match="command"):
        parse_command(bytes.fromhex(frame_hex))


# --- reply scanning --------------------------------------------------------------------


def test_scanner_reads_a_normal_reply() -> None:
    assert scan(REPLY_ON_OK) == [Reply(Function.ON, 0)]
    assert scan(REPLY_OFF_OK) == [Reply(Function.OFF, 0)]


def test_reply_round_trips_to_bytes() -> None:
    assert Reply(Function.ON, 0).to_bytes() == REPLY_ON_OK
    assert Reply(Function.OFF, 0).accepted
    assert not Reply(Function.OFF, 1).accepted


def test_scanner_drops_a_reply_with_bad_crc() -> None:
    scanner = ReplyScanner()
    assert scanner.feed(bytes.fromhex("3d 01 01 00 2a 0d 0a")) == []
    assert scanner.skipped == 7
    assert scanner.pending == b""


def test_scanner_drops_a_reply_with_bad_tail() -> None:
    assert scan(bytes.fromhex("3d 01 01 00 29 0d 0b")) == []


def test_scanner_drops_frames_with_unknown_function_code() -> None:
    body = bytes.fromhex("3d 03 01 00")
    assert scan(body + bytes([crc8(body)]) + b"\r\n") == []


def test_scanner_requires_the_length_byte_to_be_one() -> None:
    body = bytes.fromhex("3d 01 03 05")
    scanner = ReplyScanner()
    assert scanner.feed(body + bytes([crc8(body)]) + b"\r\n") == []
    assert scanner.skipped == 7


@pytest.mark.parametrize("start_hex", ["3d 05", "3d 01 02", "3d 3d 03"])
def test_scanner_discards_an_impossible_start_without_waiting(start_hex: str) -> None:
    scanner = ReplyScanner()
    start = bytes.fromhex(start_hex)
    assert scanner.feed(start) == []
    assert scanner.pending == b""
    assert scanner.skipped == len(start)


def test_scanner_reports_failure_status() -> None:
    (reply,) = scan(encode_reply(Function.ON, 1))
    assert reply == Reply(Function.ON, 1)
    assert not reply.accepted


def test_scanner_reports_unknown_status_as_not_accepted() -> None:
    (reply,) = scan(encode_reply(Function.OFF, 0x7F))
    assert reply.status == 0x7F
    assert not reply.accepted


def test_scanner_resynchronises_after_garbage() -> None:
    scanner = ReplyScanner()
    assert scanner.feed(b"\x00\xff\x3d\x3d\x01" + REPLY_OFF_OK) == [Reply(Function.OFF, 0)]
    assert scanner.skipped == 5


def test_scanner_splits_two_replies_stuck_together() -> None:
    assert scan(REPLY_ON_OK + REPLY_OFF_OK) == [Reply(Function.ON, 0), Reply(Function.OFF, 0)]


def test_scanner_keeps_an_incomplete_reply_pending() -> None:
    scanner = ReplyScanner()
    assert scanner.feed(REPLY_ON_OK[:4]) == []
    assert scanner.pending == REPLY_ON_OK[:4]
    assert scanner.skipped == 0
    assert scanner.feed(REPLY_ON_OK[4:]) == [Reply(Function.ON, 0)]
    assert scanner.pending == b""


def test_scanner_reset_forgets_pending_bytes_and_counters() -> None:
    scanner = ReplyScanner()
    scanner.feed(b"\x00" + REPLY_ON_OK[:3])
    scanner.reset()
    assert scanner.pending == b""
    assert scanner.skipped == 0
    assert scanner.feed(REPLY_ON_OK[3:]) == []


def test_scanner_ignores_the_echo_of_an_on_command() -> None:
    # A 9-byte ON command is never a valid 7-byte reply, so an echoing link cannot fake one.
    assert scan(ON_5_PWM_1023 + REPLY_ON_OK) == [Reply(Function.ON, 0)]


# --- judging a reply against the command that was sent ---------------------------------


def test_judge_accepts_a_matching_success_reply() -> None:
    assert judge(Reply(Function.ON, 0), ON_5_PWM_1023) is Verdict.ACCEPTED
    assert judge(Reply(Function.OFF, 0), OFF_5) is Verdict.ACCEPTED


def test_judge_reports_rejection_for_any_non_zero_status() -> None:
    assert judge(Reply(Function.ON, 1), ON_5_PWM_1023) is Verdict.REJECTED
    assert judge(Reply(Function.ON, 0x7F), ON_5_PWM_1023) is Verdict.REJECTED


def test_judge_flags_a_reply_for_the_other_function() -> None:
    assert judge(Reply(Function.OFF, 0), ON_5_PWM_1023) is Verdict.WRONG_FUNCTION
    assert judge(Reply(Function.ON, 0), OFF_5) is Verdict.WRONG_FUNCTION


@pytest.mark.parametrize("led_id", range(1, 23))
def test_off_request_is_byte_identical_to_off_reply_with_that_status(led_id: int) -> None:
    # Known ambiguity of the protocol: on an echoing link the request reads as "OFF failed".
    request = encode_off(led_id)
    assert request == encode_reply(Function.OFF, led_id)
    (looks_like_reply,) = scan(request)
    assert judge(looks_like_reply, request) is Verdict.ECHO


def test_off_1_rejected_with_the_documented_failure_status_reads_as_echo() -> None:
    # Status 01 is the only documented failure code, and "OFF 1" is the baud-probe command:
    # this rejection cannot be told apart from an echo. Callers must report the resulting
    # timeout as "echo or rejection".
    assert judge(Reply(Function.OFF, 1), OFF_1) is Verdict.ECHO


def test_judge_does_not_treat_other_off_rejections_as_echo() -> None:
    assert judge(Reply(Function.OFF, 1), OFF_5) is Verdict.REJECTED


# --- properties ------------------------------------------------------------------------

led_ids = st.integers(min_value=1, max_value=22)
pwms = st.integers(min_value=0, max_value=1023)
valid_replies = st.builds(
    encode_reply, st.sampled_from(list(Function)), st.integers(min_value=0, max_value=255)
)
# Noise that is rich in the bytes the scanner cares about.
noise = st.lists(
    st.sampled_from([0x3D, 0x01, 0x02, 0x00, 0x0D, 0x0A, 0x29, 0x94, 0xFF]), max_size=12
).map(bytes)
streams = st.lists(st.one_of(valid_replies, noise, st.binary(max_size=8)), max_size=10).map(
    b"".join
)


@given(led_ids, pwms)
def test_on_command_round_trips(led_id: int, pwm: int) -> None:
    assert parse_command(encode_on(led_id, pwm)) == Command(Function.ON, led_id, pwm)


@given(led_ids)
def test_off_command_round_trips(led_id: int) -> None:
    assert parse_command(encode_off(led_id)) == Command(Function.OFF, led_id, None)


@given(streams, st.data())
def test_scanning_does_not_depend_on_how_the_stream_is_chunked(
    stream: bytes, data: st.DataObject
) -> None:
    whole = ReplyScanner()
    expected = whole.feed(stream)

    cuts = sorted(data.draw(st.lists(st.integers(min_value=0, max_value=len(stream)), max_size=8)))
    chunked = ReplyScanner()
    replies: list[Reply] = []
    for start, end in zip([0, *cuts], [*cuts, len(stream)], strict=True):
        replies += chunked.feed(stream[start:end])

    assert replies == expected
    assert chunked.pending == whole.pending
    assert chunked.skipped == whole.skipped


def replies_by_definition(stream: bytes) -> list[Reply]:
    """Independent oracle: every 7-byte window that passes all five checks is a reply.

    Two valid replies can never overlap, so no ordering rule is needed.
    """
    return [
        Reply(Function(window[1]), window[3])
        for window in (stream[i : i + 7] for i in range(len(stream) - 6))
        if window[0] == 0x3D
        and window[1] in (1, 2)
        and window[2] == 1
        and reference_crc8(window[:4]) == window[4]
        and window[5:] == b"\r\n"
    ]


@given(streams)
def test_scanner_finds_exactly_the_replies_the_definition_admits(stream: bytes) -> None:
    assert scan(stream) == replies_by_definition(stream)


@given(valid_replies, st.integers(min_value=0, max_value=7 * 8 - 1))
def test_any_single_bit_flip_in_a_reply_is_rejected(frame: bytes, bit: int) -> None:
    damaged = bytearray(frame)
    damaged[bit // 8] ^= 1 << (bit % 8)
    assert scan(bytes(damaged)) == []


@given(led_ids, pwms, st.data())
def test_any_single_bit_flip_in_a_command_is_rejected(
    led_id: int, pwm: int, data: st.DataObject
) -> None:
    frame = data.draw(st.sampled_from([encode_on(led_id, pwm), encode_off(led_id)]))
    bit = data.draw(st.integers(min_value=0, max_value=len(frame) * 8 - 1))
    damaged = bytearray(frame)
    damaged[bit // 8] ^= 1 << (bit % 8)
    with pytest.raises(ValueError, match="command"):
        parse_command(bytes(damaged))
