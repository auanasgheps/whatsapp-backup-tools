import atexit
import logging
import sys
import time
import typing


def _enable_windows_vt() -> None:
    """Enable Virtual Terminal Processing on Windows console handles if available."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class _ConsoleFilter(logging.Filter):
    """Filter out progress milestone log records from console when TTY is active."""

    def __init__(self, is_tty: bool):
        super().__init__()
        self._is_tty = is_tty

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, 'is_progress', False) and self._is_tty:
            return False
        return True


class ProgressReporter:
    """
    Reports progress for long-running operations.

    In interactive terminals (TTY), renders an in-place status line with carriage return.
    In non-interactive environments or file logs, emits periodic milestone messages.
    """

    def __init__(
        self,
        description: str,
        total: int,
        logger: logging.Logger,
        stream: typing.TextIO,
        min_interval_seconds: float,
    ):
        self._description = description
        self._total = total
        self._logger = logger
        self._stream = stream
        self._min_interval_seconds = min_interval_seconds

        self._last_update_time = 0.0
        self._last_len = 0
        self._last_rendered_line = ""
        self._cursor_hidden = False
        self._is_tty = hasattr(stream, "isatty") and stream.isatty()
        self._last_milestone = -1
        self._finished = False

        self._console_filter = None
        self._hooked_handler = None
        self._orig_emit = None

        if self._is_tty:
            _enable_windows_vt()
            atexit.register(self._show_cursor)

        self._setup_interleaving_guard()

    def _hide_cursor(self) -> None:
        """Hide terminal cursor to prevent update strobing."""
        if self._is_tty and self._stream is not None and not self._cursor_hidden:
            self._stream.write("\033[?25l")
            self._stream.flush()
            self._cursor_hidden = True

    def _show_cursor(self) -> None:
        """Restore terminal cursor."""
        if self._cursor_hidden and self._stream is not None:
            self._stream.write("\033[?25h")
            self._stream.flush()
            self._cursor_hidden = False

    def _setup_interleaving_guard(self) -> None:
        """Prevent console log messages from colliding with the in-place status line."""
        if not self._is_tty or self._logger is None or not hasattr(self._logger, "handlers"):
            return

        self._console_filter = _ConsoleFilter(self._is_tty)

        for handler in self._logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.addFilter(self._console_filter)
                if self._hooked_handler is None:
                    self._hooked_handler = handler
                    self._orig_emit = handler.emit

                    reporter_self = self

                    def wrapped_emit(record: logging.LogRecord) -> None:
                        if reporter_self._last_len > 0 and not getattr(record, 'is_progress', False):
                            if reporter_self._stream is not None:
                                reporter_self._show_cursor()
                                reporter_self._stream.write("\r" + " " * reporter_self._last_len + "\r")
                                reporter_self._stream.flush()
                            reporter_self._last_len = 0
                            reporter_self._last_rendered_line = ""
                        if reporter_self._orig_emit:
                            reporter_self._orig_emit(record)

                    handler.emit = wrapped_emit

    def _teardown_interleaving_guard(self) -> None:
        """Restore original handler emit, show cursor, and remove console filter."""
        try:
            atexit.unregister(self._show_cursor)
        except Exception:
            pass
        self._show_cursor()

        if self._hooked_handler and self._orig_emit:
            self._hooked_handler.emit = self._orig_emit
            self._hooked_handler = None
            self._orig_emit = None

        if self._console_filter and self._logger is not None and hasattr(self._logger, "handlers"):
            for handler in self._logger.handlers:
                try:
                    handler.removeFilter(self._console_filter)
                except ValueError:
                    pass
            self._console_filter = None

    def _format_percentage(self, current: int, total: int) -> float:
        """Calculate percentage safely."""
        if total <= 0:
            return 100.0
        return min(100.0, max(0.0, (current / total) * 100.0))

    def _format_stats(self, stats: dict[str, int]) -> str:
        """Format operational counters into a human-readable string."""
        if not stats:
            return ""
        return " | " + " | ".join(f"{key}: {val:,}" for key, val in stats.items())

    def _format_status_line(self, current: int, stats: dict[str, int]) -> str:
        """Build status line for interactive terminal display."""
        pct = self._format_percentage(current, self._total)
        stats_str = self._format_stats(stats)
        if self._total > 0:
            return f"{self._description}: {pct:.1f}% ({current:,}/{self._total:,}){stats_str}"
        return f"{self._description}: {current:,}{stats_str}"

    def _format_milestone_log(self, current: int, stats: dict[str, int]) -> str:
        """Build milestone log string for file and non-interactive output."""
        pct = self._format_percentage(current, self._total)
        stats_str = self._format_stats(stats)
        if self._total > 0:
            return f"{self._description}: {current}/{self._total} ({pct:.1f}%){stats_str}"
        return f"{self._description}: {current}{stats_str}"

    def _should_log_milestone(self, current: int, total: int) -> bool:
        """Determine if the current row triggers a milestone log entry."""
        if total <= 0:
            return current != self._last_milestone

        if total >= 1000:
            step = 1000
        else:
            step = max(1, total // 5)

        if current == total:
            return current != self._last_milestone

        if current % step == 0 and current != self._last_milestone:
            return True

        return False

    def update(self, current: int, stats: dict[str, int]) -> None:
        """Update progress with current item count and stats dictionary."""
        if self._finished:
            return

        now = time.monotonic()
        is_first_or_last = (current == 1 or current == self._total)

        if self._should_log_milestone(current, self._total):
            self._last_milestone = current
            if self._logger is not None:
                log_msg = self._format_milestone_log(current, stats)
                self._logger.info(log_msg, extra={'is_progress': True})

        if self._is_tty and self._stream is not None and (
            is_first_or_last or (now - self._last_update_time >= self._min_interval_seconds)
        ):
            line = self._format_status_line(current, stats)
            if line == self._last_rendered_line and not is_first_or_last:
                return

            self._hide_cursor()
            self._last_update_time = now
            self._last_rendered_line = line

            padding = max(0, self._last_len - len(line))
            self._stream.write("\r" + line + (" " * padding))
            self._stream.flush()
            self._last_len = len(line)

    def finish(self, stats: dict[str, int]) -> None:
        """Finalize progress display, writing final line and newline."""
        if self._finished:
            return
        self._finished = True

        if self._total > 0 and self._last_milestone != self._total:
            self._last_milestone = self._total
            if self._logger is not None:
                log_msg = self._format_milestone_log(self._total, stats)
                self._logger.info(log_msg, extra={'is_progress': True})

        if self._is_tty and self._stream is not None:
            self._show_cursor()
            line = self._format_status_line(self._total, stats)
            padding = max(0, self._last_len - len(line))
            self._stream.write("\r" + line + (" " * padding) + "\n")
            self._stream.flush()
            self._last_len = 0

        self._teardown_interleaving_guard()
