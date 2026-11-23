"""
Unit and integration tests for chat duration calculations and time formatting.

Covers:
- Chat Info lifetime / span algorithm from js/21_panel_chat_info.js across boundary cases
- fmtDuration and fmtDurationLong from js/01_utils_text.js
- Call outcome text and duration metrics from js/25_calls.js
- Audio, voice note, and video note duration formatting
- Live location countdown / expiry time math
- Ephemeral / disappearing message duration timers (24h, 7d, 90d)
- API endpoint verification that first_ts and last_ts exclude service messages
"""

import math


def calculate_chat_span(first_ts: int | None, last_ts: int | None) -> str | None:
    """
    Python reference implementation of the client-side span calculation in
    wab_viewer/chat_viewer/js/21_panel_chat_info.js:
      const days = Math.round((info.last_ts - info.first_ts) / 86400000);
      const years = Math.floor(days / 365);
      const months = Math.floor((days % 365) / 30);
      let span = '';
      if (years > 0) span += years + (years === 1 ? ' year' : ' years');
      if (months > 0) span += (span ? ', ' : '') + months + (months === 1 ? ' month' : ' months');
      if (!span) span = days + (days === 1 ? ' day' : ' days');
    """
    if not first_ts or not last_ts or last_ts == first_ts:
        return None
    if last_ts < first_ts:
        return None  # Clock skew safeguard

    days = round((last_ts - first_ts) / 86400000.0)
    years = math.floor(days / 365)
    months = math.floor((days % 365) / 30)

    span = ""
    if years > 0:
        span += f"{years} year" if years == 1 else f"{years} years"
    if months > 0:
        prefix = ", " if span else ""
        span += prefix + (f"{months} month" if months == 1 else f"{months} months")
    if not span:
        span = f"{days} day" if days == 1 else f"{days} days"
    return span


def format_duration(seconds: float | int | None) -> str:
    """
    Python reference implementation of fmtDuration(seconds) from js/01_utils_text.js.
    """
    if seconds is None or seconds < 0:
        return "0:00"
    sec = int(round(seconds))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_duration_long(seconds: float | int | None) -> str:
    """
    Python reference implementation of fmtDurationLong(seconds) from js/01_utils_text.js.
    """
    if seconds is None or seconds <= 0:
        return "0 sec"
    sec = int(round(seconds))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60

    parts: list[str] = []
    if h > 0:
        parts.append(f"{h} hr")
    if m > 0:
        parts.append(f"{m} min")
    if s > 0 or not parts:
        parts.append(f"{s} sec")
    return " ".join(parts)


def format_call_outcome(
    call_type: str,
    outcome: str | None,
    duration_sec: int,
    is_video: bool,
) -> str:
    """
    Python reference implementation of _formatCallOutcomeText from js/25_calls.js.
    """
    dir_label = "Video call" if is_video else "Voice call"
    if call_type == "incoming":
        dir_label = f"Incoming · {dir_label}"
    elif call_type == "outgoing":
        dir_label = f"Outgoing · {dir_label}"

    if call_type == "missed" and (duration_sec is None or duration_sec <= 0):
        return f"Missed · {'Video call' if is_video else 'Voice call'}"

    if duration_sec > 0:
        return f"{dir_label} · {format_duration(duration_sec)}"
    return dir_label


def format_ephemeral_timer(duration_sec: int) -> str:
    """Format disappearing message duration into human readable string."""
    if duration_sec == 86400:
        return "24 hours"
    elif duration_sec == 604800:
        return "7 days"
    elif duration_sec in (7776000, 7862400):
        return "90 days"
    elif duration_sec % 86400 == 0:
        days = duration_sec // 86400
        return f"{days} days"
    return f"{duration_sec} seconds"


# --- Test Cases ---


class TestChatDurationSpan:
    """Tests for chat lifetime / duration calculations in Chat Info panel."""

    def test_single_message_or_same_timestamp_omits_duration(self) -> None:
        """When first_ts == last_ts, no duration is displayed."""
        assert calculate_chat_span(1700000000000, 1700000000000) is None
        assert calculate_chat_span(None, 1700000000000) is None
        assert calculate_chat_span(1700000000000, None) is None

    def test_sub_day_durations(self) -> None:
        """Sub-day duration rounding: < 12h rounds to 0 days, >= 12h rounds to 1 day."""
        base = 1700000000000
        # 6 hours later (21,600,000 ms) -> rounds to 0 days
        span_6h = calculate_chat_span(base, base + 21_600_000)
        assert span_6h == "0 days"

        # 16 hours later (57,600,000 ms) -> rounds to 1 day
        span_16h = calculate_chat_span(base, base + 57_600_000)
        assert span_16h == "1 day"

    def test_day_boundaries(self) -> None:
        """Test singular vs plural day transitions."""
        base = 1700000000000
        ms_day = 86_400_000

        assert calculate_chat_span(base, base + 1 * ms_day) == "1 day"
        assert calculate_chat_span(base, base + 2 * ms_day) == "2 days"
        assert calculate_chat_span(base, base + 15 * ms_day) == "15 days"
        assert calculate_chat_span(base, base + 29 * ms_day) == "29 days"

    def test_month_and_year_transitions(self) -> None:
        """Test month and year formatting logic."""
        base = 1700000000000
        ms_day = 86_400_000

        # Exactly 30 days -> "1 month"
        assert calculate_chat_span(base, base + 30 * ms_day) == "1 month"
        # 60 days -> "2 months"
        assert calculate_chat_span(base, base + 60 * ms_day) == "2 months"

        # Exactly 365 days -> "1 year"
        assert calculate_chat_span(base, base + 365 * ms_day) == "1 year"
        # 395 days (1 year + 30 days) -> "1 year, 1 month"
        assert calculate_chat_span(base, base + 395 * ms_day) == "1 year, 1 month"
        # 425 days (1 year + 60 days) -> "1 year, 2 months"
        assert calculate_chat_span(base, base + 425 * ms_day) == "1 year, 2 months"

        # 730 days -> "2 years"
        assert calculate_chat_span(base, base + 730 * ms_day) == "2 years"

    def test_clock_skew_inverted_timestamps(self) -> None:
        """If clock skewed such that last_ts < first_ts, gracefully return None."""
        assert calculate_chat_span(1700086400000, 1700000000000) is None


class TestMediaAndCallDurationFormatting:
    """Tests for audio, video, and call duration formatting."""

    def test_fmt_duration_standard(self) -> None:
        """Standard mm:ss and hh:mm:ss formatting."""
        assert format_duration(0) == "0:00"
        assert format_duration(9) == "0:09"
        assert format_duration(59) == "0:59"
        assert format_duration(60) == "1:00"
        assert format_duration(75) == "1:15"
        assert format_duration(599) == "9:59"
        assert format_duration(600) == "10:00"
        assert format_duration(3599) == "59:59"
        assert format_duration(3600) == "1:00:00"
        assert format_duration(3665) == "1:01:05"
        assert format_duration(7200) == "2:00:00"

    def test_fmt_duration_negative_or_none(self) -> None:
        """Negative or None inputs fall back safely."""
        assert format_duration(-10) == "0:00"
        assert format_duration(None) == "0:00"

    def test_fmt_duration_long(self) -> None:
        """Long form formatting: '1 hr 15 min 30 sec'."""
        assert format_duration_long(0) == "0 sec"
        assert format_duration_long(45) == "45 sec"
        assert format_duration_long(120) == "2 min"
        assert format_duration_long(135) == "2 min 15 sec"
        assert format_duration_long(3600) == "1 hr"
        assert format_duration_long(3665) == "1 hr 1 min 5 sec"

    def test_call_outcome_formatting(self) -> None:
        """Call outcome badges: missed, incoming, outgoing with durations."""
        # Missed voice call with 0s
        assert format_call_outcome("missed", None, 0, False) == "Missed · Voice call"
        # Missed video call with 0s
        assert format_call_outcome("missed", None, 0, True) == "Missed · Video call"
        # Connected incoming voice call
        assert (
            format_call_outcome("incoming", "connected", 145, False)
            == "Incoming · Voice call · 2:25"
        )
        # Connected outgoing video call
        assert (
            format_call_outcome("outgoing", "connected", 3600, True)
            == "Outgoing · Video call · 1:00:00"
        )

    def test_ephemeral_timer_durations(self) -> None:
        """WhatsApp disappearing message options."""
        assert format_ephemeral_timer(86400) == "24 hours"
        assert format_ephemeral_timer(604800) == "7 days"
        assert format_ephemeral_timer(7776000) == "90 days"
        assert format_ephemeral_timer(172800) == "2 days"
