"""Terminal progress display for the aria2 download manager.

A lightweight, dependency-free progress renderer that shows per-segment
progress bars, aggregate speed, ETA, and download percentage using ANSI
escape codes.
"""

import ctypes
import os
import shutil
import sys
import threading
import time
from collections import deque


# ---------------------------------------------------------------------------
# Character constants for progress bars
# ---------------------------------------------------------------------------

class _Chars:
    FILLED = "#"
    EMPTY = "-"
    START = "["
    END = "]"


# ---------------------------------------------------------------------------
# Byte formatting
# ---------------------------------------------------------------------------

def _format_size(num_bytes: int) -> str:
    """Format a byte count into a human-readable string."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num_bytes /= 1024.0
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
    return f"{num_bytes:.1f} PB"


def _format_speed(bytes_per_sec: float) -> str:
    """Format a bytes-per-second rate into a human-readable string."""
    return f"{_format_size(int(bytes_per_sec))}/s"


def _format_time(seconds: float) -> str:
    """Format a duration in seconds."""
    if seconds < 0 or seconds == float("inf"):
        return "--:--"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m{s:02d}s"


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_ANSI_AVAILABLE: bool | None = None


def _enable_windows_ansi() -> bool:
    """Enable ANSI escape sequence processing on Windows 10+."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.windll.kernel32

        # Enable virtual terminal processing for stdout
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if kernel32.SetConsoleMode(handle, new_mode):
                return True
    except Exception:
        pass
    return False


def _supports_ansi() -> bool:
    """Detect whether the terminal supports ANSI escape codes."""
    global _ANSI_AVAILABLE
    if _ANSI_AVAILABLE is not None:
        return _ANSI_AVAILABLE

    if not getattr(sys.stdout, "isatty", lambda: False)():
        _ANSI_AVAILABLE = False
        return False

    if sys.platform == "win32":
        # Try to enable ANSI on Windows 10+
        _ANSI_AVAILABLE = _enable_windows_ansi()
        if not _ANSI_AVAILABLE:
            # Fallback checks
            enable = (
                os.environ.get("WT_SESSION")
                or os.environ.get("ANSICON")
                or os.environ.get("ConEmuANSI") == "ON"
            )
            if not enable:
                import platform
                try:
                    ver = platform.version()
                except Exception:
                    ver = ""
                build = int(ver.split(".")[-1]) if ver else 0
                enable = build >= 16257
            _ANSI_AVAILABLE = bool(enable)
    else:
        _ANSI_AVAILABLE = True

    return _ANSI_AVAILABLE


def _cursor_up(n: int) -> str:
    """ANSI cursor-up ``n`` rows."""
    return f"\033[{n}A" if _supports_ansi() else ""


def _clear_line() -> str:
    """ANSI clear entire line."""
    return "\033[2K" if _supports_ansi() else ""


def _hide_cursor() -> str:
    return "\033[?25l" if _supports_ansi() else ""


def _show_cursor() -> str:
    return "\033[?25h" if _supports_ansi() else ""


# ---------------------------------------------------------------------------
# Progress bar helpers
# ---------------------------------------------------------------------------

def _make_bar(ratio: float, width: int = 20) -> str:
    """Build a simple ASCII progress bar string.

    Args:
        ratio: Completion ratio, 0.0 - 1.0.
        width: Total character width of the bar (excluding brackets).
    """
    filled = int(round(ratio * width))
    return _Chars.START + _Chars.FILLED * filled + _Chars.EMPTY * (width - filled) + _Chars.END


def _percentage(ratio: float) -> str:
    return f"{ratio * 100:5.1f}%"


# ---------------------------------------------------------------------------
# ProgressDisplay - the main renderer
# ---------------------------------------------------------------------------

class ProgressDisplay:
    """Thread-safe progress display for multi-segment downloads.

    Parameters:
        total_size: Total download size in bytes.
        segment_count: Number of concurrent download segments.
        refresh_interval: Seconds between display refreshes (default 0.25 s).
    """

    def __init__(
        self,
        total_size: int,
        segment_count: int,
        refresh_interval: float = 0.25,
    ) -> None:
        self._total_size = total_size
        self._segment_count = segment_count
        self._refresh_interval = refresh_interval
        self._num_lines = 0  # track how many lines we've written

        # Per-segment downloaded byte counters (indexed by segment index)
        self._segments: list[int] = [0] * segment_count
        self._lock = threading.Lock()

        # Speed tracking - rolling window of (timestamp, total_bytes)
        self._speed_window: deque[tuple[float, int]] = deque(maxlen=20)

        self._start_time = time.time()
        self._done = False
        self._thread: threading.Thread | None = None

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        """Launch the background refresh thread."""
        if _supports_ansi():
            sys.stdout.write(_hide_cursor())
            sys.stdout.flush()
        else:
            self._refresh_interval = max(self._refresh_interval, 5.0)
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the refresh thread to stop and join it."""
        self._done = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._render()
        if _supports_ansi():
            sys.stdout.write(_show_cursor())
        sys.stdout.write("\n")
        sys.stdout.flush()

    def update_segment(self, index: int, downloaded: int) -> None:
        """Update the downloaded byte count for a segment.

        Called by segment worker threads.
        """
        with self._lock:
            self._segments[index] = downloaded

    def add_sample(self, total_downloaded: int) -> None:
        """Feed a total-bytes-downloaded sample for speed calculation."""
        with self._lock:
            self._speed_window.append((time.time(), total_downloaded))

    # -- internal -------------------------------------------------------------

    def _get_state(self) -> tuple[int, float, str, str]:
        """Return (total_downloaded, speed, eta_str, perc_str)."""
        with self._lock:
            total = sum(self._segments)
            # Auto-sample the total for the speed window
            now = time.time()
            self._speed_window.append((now, total))
            # Compute speed from the rolling window
            speed = 0.0
            if len(self._speed_window) >= 2:
                t_first = self._speed_window[0][0]
                b_first = self._speed_window[0][1]
                t_last = self._speed_window[-1][0]
                b_last = self._speed_window[-1][1]
                dt = t_last - t_first
                if dt > 0:
                    speed = (b_last - b_first) / dt
            # Fallback: use elapsed wall-clock time since start
            if speed == 0 and total > 0:
                elapsed = now - self._start_time
                if elapsed > 0.5:
                    speed = total / elapsed

            # ETA
            remaining = self._total_size - total
            speed_for_eta = speed if speed > 0 else 0.0

        # Compute ETA outside lock (no shared state needed)
        if total > 0 and speed_for_eta > 0:
            eta = remaining / speed_for_eta
        else:
            eta = float("inf")

        ratio = total / self._total_size if self._total_size > 0 else 1.0
        return total, speed, _format_time(eta), _percentage(ratio)

    def _render_loop(self) -> None:
        """Main loop that redraws the progress display."""
        while not self._done:
            self._render()
            time.sleep(self._refresh_interval)

    def _render(self) -> None:
        """Paint one frame - move cursor up and redraw in place.

        Instead of clearing the whole screen (which causes flicker and
        scrollback pollution), we move the cursor up to the first line
        of our display block and overwrite each line in place.
        """
        total, speed, eta, perc = self._get_state()
        term_width = shutil.get_terminal_size().columns or 80

        lines: list[str] = []

        # Header line
        header = (
            f" aria2  {perc} | {_format_size(total)} / "
            f"{_format_size(self._total_size)} | {_format_speed(speed)} | "
            f"ETA {eta}"
        )
        lines.append(header[: term_width - 1] if len(header) > term_width else header)

        # Segment bars
        with self._lock:
            seg_copy = list(self._segments)
        base, remainder = divmod(self._total_size, max(self._segment_count, 1))
        DISPLAY_LIMIT = 8
        for index, downloaded in enumerate(seg_copy[:DISPLAY_LIMIT]):
            segment_size = base + (1 if index < remainder else 0)
            ratio = min(max(downloaded / segment_size, 0.0), 1.0) if segment_size else 1.0
            lines.append(
                f" segment {index + 1:02d} {_make_bar(ratio)} {_percentage(ratio)} "
                f"{_format_size(downloaded)}/{_format_size(segment_size)}"
            )
        if len(seg_copy) > DISPLAY_LIMIT:
            lines.append(f" ... {len(seg_copy) - DISPLAY_LIMIT} more segments")

        if not _supports_ansi():
            # Redirected output and older terminals get a compact single-line
            # status instead of thousands of scrolling progress-bar frames.
            sys.stdout.write("\r" + header[: max(term_width - 1, 1)].ljust(max(term_width - 1, 1)))
            sys.stdout.flush()
            return

        if self._num_lines:
            sys.stdout.write(_cursor_up(self._num_lines))
        for line in lines:
            rendered = line[: max(term_width - 1, 1)]
            sys.stdout.write(_clear_line() + rendered + "\n")
        self._num_lines = len(lines)
        sys.stdout.flush()
