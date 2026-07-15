"""Reliable multi-segment HTTP downloader with resumable state."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import random
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .control import ControlFile, DownloadState, SegmentState
from .progress import ProgressDisplay, _format_size, _format_speed, _format_time

DEFAULT_UA = "Aria2-Python/1.1"
CHUNK_SIZE = 1024 * 1024
CONTROL_FLUSH_INTERVAL = 1.0
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


class DownloadError(Exception):
    """A download response was incomplete or unsafe to write."""


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.max_redirections = maximum
        self.max_repeats = maximum


def _make_opener(max_redirects: int) -> urllib.request.OpenerDirector:
    # Use Python's verified default TLS context. Disabling certificate checks can
    # silently turn a model download into attacker-controlled bytes.
    return urllib.request.build_opener(_LimitedRedirectHandler(max_redirects))


def _parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    match = _CONTENT_RANGE_RE.match((value or "").strip())
    if not match:
        return None
    start, end, total = match.groups()
    return int(start), int(end), None if total == "*" else int(total)


def _probe_via_range(
    opener: urllib.request.OpenerDirector, url: str
) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Range": "bytes=0-0", "User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"},
    )
    with opener.open(req, timeout=30) as resp:
        parsed = _parse_content_range(resp.headers.get("Content-Range"))
        if resp.status == 206 and parsed and parsed[0] == 0 and parsed[1] == 0:
            return {
                "content_length": parsed[2],
                "etag": resp.headers.get("ETag"),
                "supports_ranges": True,
                "final_url": resp.url or url,
            }
        length = resp.headers.get("Content-Length")
        return {
            "content_length": int(length) if length else None,
            "etag": resp.headers.get("ETag"),
            "supports_ranges": False,
            "final_url": resp.url or url,
        }


def _head_request(url: str, max_redirects: int = 10) -> dict[str, Any]:
    """Probe size and verify actual byte-range support."""
    opener = _make_opener(max_redirects)
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"},
    )
    try:
        with opener.open(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            head = {
                "content_length": int(length) if length else None,
                "etag": resp.headers.get("ETag"),
                "final_url": resp.url or url,
            }
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            raise
        return _probe_via_range(opener, url)

    # Accept-Ranges is only advisory. A one-byte request proves whether the
    # endpoint (including its redirects) really honors ranges.
    try:
        ranged = _probe_via_range(opener, url)
    except (urllib.error.HTTPError, urllib.error.URLError, DownloadError):
        ranged = {**head, "supports_ranges": False}
    if ranged.get("content_length") is None:
        ranged["content_length"] = head["content_length"]
    if not ranged.get("etag"):
        ranged["etag"] = head["etag"]
    return ranged


def _partition(total_size: int, segment_count: int) -> list[SegmentState]:
    if total_size <= 0:
        return []
    count = min(max(segment_count, 1), total_size)
    base, remainder = divmod(total_size, count)
    segments: list[SegmentState] = []
    start = 0
    for index in range(count):
        length = base + (1 if index < remainder else 0)
        end = start + length - 1
        segments.append(SegmentState(index=index, start=start, end=end))
        start = end + 1
    return segments


def _segment_size(segment: SegmentState) -> int:
    return segment.end - segment.start + 1


def _total_downloaded(segments: list[SegmentState]) -> int:
    return sum(min(max(s.downloaded, 0), _segment_size(s)) for s in segments)


def _state_segments(segments: list[SegmentState]) -> list[dict[str, int]]:
    return [
        {"index": s.index, "start": s.start, "end": s.end, "downloaded": s.downloaded}
        for s in segments
    ]


@dataclass
class _WorkerResult:
    success: bool
    error: str | None = None


def _download_segment(
    *,
    opener: urllib.request.OpenerDirector,
    url: str,
    output_path: str,
    total_size: int,
    segment: SegmentState,
    control: ControlFile,
    download_state: DownloadState,
    progress: ProgressDisplay,
    shutdown_flag: threading.Event,
    max_retries: int,
    state_lock: threading.RLock,
    chunk_size: int = CHUNK_SIZE,
) -> _WorkerResult:
    segment_length = _segment_size(segment)
    if segment.downloaded >= segment_length:
        return _WorkerResult(True)

    attempts = 0
    last_error = "unknown error"
    last_flush = time.monotonic()

    while not shutdown_flag.is_set() and segment.downloaded < segment_length:
        range_start = segment.start + segment.downloaded
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Range": f"bytes={range_start}-{segment.end}",
                    "User-Agent": DEFAULT_UA,
                    "Accept-Encoding": "identity",
                },
            )
            with opener.open(req, timeout=60) as resp:
                parsed = _parse_content_range(resp.headers.get("Content-Range"))
                if resp.status != 206 or not parsed:
                    raise DownloadError(f"server ignored Range request (HTTP {resp.status})")
                response_start, response_end, response_total = parsed
                if response_start != range_start or response_end > segment.end:
                    raise DownloadError(
                        f"invalid Content-Range: {resp.headers.get('Content-Range')!r}"
                    )
                if response_total is not None and response_total != total_size:
                    raise DownloadError(
                        f"remote size changed from {total_size} to {response_total} bytes"
                    )

                with open(output_path, "r+b", buffering=0) as output:
                    output.seek(range_start)
                    while not shutdown_flag.is_set() and segment.downloaded < segment_length:
                        remaining = segment_length - segment.downloaded
                        chunk = resp.read(min(chunk_size, remaining))
                        if not chunk:
                            break
                        written = output.write(chunk)
                        if written != len(chunk):
                            raise DownloadError("short local file write")
                        with state_lock:
                            segment.downloaded += written
                            download_state.segments[segment.index]["downloaded"] = segment.downloaded
                        progress.update_segment(segment.index, segment.downloaded)
                        now = time.monotonic()
                        if now - last_flush >= CONTROL_FLUSH_INTERVAL:
                            control.save(download_state)
                            last_flush = now

            if segment.downloaded == segment_length:
                control.save(download_state)
                return _WorkerResult(True)
            raise DownloadError(
                f"connection ended early at {segment.downloaded}/{segment_length} bytes"
            )
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if 400 <= exc.code < 500 and exc.code not in (408, 416, 429):
                break
        except (urllib.error.URLError, OSError, TimeoutError, DownloadError) as exc:
            last_error = str(getattr(exc, "reason", exc))

        attempts += 1
        if attempts > max_retries:
            break
        delay = min(2 ** min(attempts - 1, 5) + random.uniform(0, 0.5), 30)
        shutdown_flag.wait(delay)

    control.save(download_state)
    return _WorkerResult(False, last_error)


class DownloadManager:
    """Orchestrate a verified, resumable HTTP(S) download."""

    def __init__(
        self,
        url: str,
        output: str,
        segments: int = 4,
        max_retries: int = 5,
        max_redirects: int = 10,
        resume: bool = True,
        sha256: str | None = None,
    ) -> None:
        self.url = url
        self.output = os.path.abspath(output)
        self.segment_count = max(segments, 1)
        self.max_retries = max(max_retries, 0)
        self.max_redirects = max(max_redirects, 0)
        self.allow_resume = resume
        self.expected_sha256 = sha256.lower() if sha256 else None
        if self.expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
            raise ValueError("sha256 must be exactly 64 hexadecimal characters")
        self.control = ControlFile(self.output)
        self._shutdown = threading.Event()
        self._state_lock = threading.RLock()

    def run(self) -> bool:
        self._install_signal_handler()
        try:
            meta = _head_request(self.url, self.max_redirects)
        except urllib.error.HTTPError as exc:
            print(f"Error: HTTP {exc.code} — {exc.reason}", file=sys.stderr)
            return False
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"Error: {getattr(exc, 'reason', exc)}", file=sys.stderr)
            return False

        total_size = meta["content_length"]
        if total_size is None:
            print("Warning: server did not report a file size; resume is unavailable.")
            return self._download_sequential(meta["final_url"])
        if total_size == 0:
            os.makedirs(os.path.dirname(self.output) or ".", exist_ok=True)
            with open(self.output, "wb"):
                pass
            return self._verify_checksum()
        if not meta["supports_ranges"]:
            print("  Server does not support byte ranges; using one safe sequential stream.")
            return self._download_sequential(meta["final_url"], total_size)

        segments = self._resolve_segments(total_size, meta.get("etag"))
        self._pre_allocate(total_size)
        state = DownloadState(
            url=self.url,
            output_path=self.output,
            total_size=total_size,
            segment_count=len(segments),
            segments=_state_segments(segments),
            etag=meta.get("etag"),
        )
        self.control.save(state)

        already_done = _total_downloaded(segments)
        print(f"  Downloading: {os.path.basename(self.output)}")
        print(
            f"  Size: {_format_size(total_size)}  Segments: {len(segments)}  "
            f"Resume: {_format_size(already_done)} already done\n"
        )
        progress = ProgressDisplay(total_size, len(segments))
        for segment in segments:
            progress.update_segment(segment.index, segment.downloaded)
        progress.start()
        started = time.time()
        errors: list[str] = []
        opener = _make_opener(self.max_redirects)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(segments)) as executor:
                futures = [
                    executor.submit(
                        _download_segment,
                        opener=opener,
                        url=meta["final_url"],
                        output_path=self.output,
                        total_size=total_size,
                        segment=segment,
                        control=self.control,
                        download_state=state,
                        progress=progress,
                        shutdown_flag=self._shutdown,
                        max_retries=self.max_retries,
                        state_lock=self._state_lock,
                    )
                    for segment in segments
                    if segment.downloaded < _segment_size(segment)
                ]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:  # keep other segments resumable
                        errors.append(f"worker failed unexpectedly: {exc}")
                    else:
                        if not result.success and result.error:
                            errors.append(result.error)
        except KeyboardInterrupt:
            self._shutdown.set()
        finally:
            progress.stop()
            with self._state_lock:
                state.segments = _state_segments(segments)
            self.control.save(state)

        elapsed = time.time() - started
        total = _total_downloaded(segments)
        complete = all(s.downloaded == _segment_size(s) for s in segments)
        if self._shutdown.is_set() or not complete:
            if errors:
                print(f"\n  Last error: {errors[-1]}", file=sys.stderr)
            print(
                f"\n  Incomplete: {_format_size(total)}/{_format_size(total_size)}. "
                "Run the same command to resume."
            )
            return False

        if not self._verify_checksum():
            # Force a clean rewrite on the next run; never bless corrupt bytes.
            for segment in segments:
                segment.downloaded = 0
            state.segments = _state_segments(segments)
            self.control.save(state)
            return False

        self.control.delete()
        speed = total_size / elapsed if elapsed > 0 else 0
        print(
            f"\n  Done! {_format_size(total_size)} in {_format_time(elapsed)} "
            f"({_format_speed(speed)})"
        )
        return True

    def _resolve_segments(self, total_size: int, etag: str | None) -> list[SegmentState]:
        fresh = _partition(total_size, self.segment_count)
        if not self.allow_resume:
            self.control.delete()
            return fresh
        state = self.control.load()
        if state is None:
            return fresh
        if (
            state.url != self.url
            or os.path.abspath(state.output_path) != self.output
            or state.total_size != total_size
            or (state.etag and etag and state.etag != etag)
            or not os.path.isfile(self.output)
            or os.path.getsize(self.output) != total_size
        ):
            print("  Existing resume metadata does not match; restarting safely.")
            self.control.delete()
            return fresh
        try:
            old = self.control.segments_from_state(state)
            self._validate_segments(old, total_size)
        except (KeyError, TypeError, ValueError):
            self.control.delete()
            return fresh
        if state.segment_count != len(fresh):
            print("  Segment count changed; restarting safely to avoid byte gaps.")
            return fresh
        return old

    @staticmethod
    def _validate_segments(segments: list[SegmentState], total_size: int) -> None:
        expected_start = 0
        for index, segment in enumerate(segments):
            if (
                segment.index != index
                or segment.start != expected_start
                or segment.end < segment.start
                or not 0 <= segment.downloaded <= _segment_size(segment)
            ):
                raise ValueError("invalid resume segment layout")
            expected_start = segment.end + 1
        if expected_start != total_size:
            raise ValueError("resume segments do not cover the file exactly")

    def _pre_allocate(self, total_size: int) -> None:
        os.makedirs(os.path.dirname(self.output) or ".", exist_ok=True)
        mode = "r+b" if os.path.exists(self.output) else "w+b"
        with open(self.output, mode) as output:
            output.truncate(total_size)

    def _download_sequential(self, url: str, expected_size: int | None = None) -> bool:
        os.makedirs(os.path.dirname(self.output) or ".", exist_ok=True)
        opener = _make_opener(self.max_redirects)
        req = urllib.request.Request(
            url, headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"}
        )
        try:
            with opener.open(req, timeout=60) as resp, open(self.output, "wb") as output:
                total = 0
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    total += len(chunk)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Error: {getattr(exc, 'reason', exc)}", file=sys.stderr)
            return False
        if expected_size is not None and total != expected_size:
            print(f"Error: received {total} bytes, expected {expected_size}", file=sys.stderr)
            return False
        if not self._verify_checksum():
            return False
        self.control.delete()
        print(f"  Done! {_format_size(total)}")
        return True

    def _verify_checksum(self) -> bool:
        if not self.expected_sha256:
            return True
        print("  Verifying SHA-256...", flush=True)
        digest = hashlib.sha256()
        try:
            with open(self.output, "rb") as model:
                for chunk in iter(lambda: model.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            print(f"Error: cannot verify file: {exc}", file=sys.stderr)
            return False
        actual = digest.hexdigest()
        if actual != self.expected_sha256:
            print(
                f"Error: SHA-256 mismatch\n  expected: {self.expected_sha256}\n  actual:   {actual}",
                file=sys.stderr,
            )
            return False
        print(f"  SHA-256 OK: {actual}")
        return True

    def _install_signal_handler(self) -> None:
        def handler(_signum: int, _frame: Any) -> None:
            self._shutdown.set()

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except ValueError:
            pass


def download(
    url: str,
    output: str | None = None,
    segments: int = 4,
    retries: int = 5,
    resume: bool = True,
    sha256: str | None = None,
) -> bool:
    """Download a URL using the same verified engine as the CLI."""
    if output is None:
        from .cli import default_output_path

        output = default_output_path(url)
    return DownloadManager(
        url=url,
        output=output,
        segments=segments,
        max_retries=retries,
        resume=resume,
        sha256=sha256,
    ).run()
