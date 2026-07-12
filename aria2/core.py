"""Core download manager with multi-segment parallel downloading and resume.

Implements a thread-pool-based downloader that splits files into
segments, downloads them concurrently via HTTP Range requests, and
persists progress via a JSON control file for resumability.
"""

import concurrent.futures
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .control import ControlFile, DownloadState, SegmentState
from .progress import ProgressDisplay, _format_size, _format_speed, _format_time

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_UA = "aria2/1.0"
CHUNK_SIZE = 256 * 1024          # 256 KB per read chunk
CONTROL_FLUSH_INTERVAL = 1.0     # seconds between control-file saves


# ---------------------------------------------------------------------------
# Segment worker
# ---------------------------------------------------------------------------

def _download_segment(
    url: str,
    output_path: str,
    segment: SegmentState,
    control: ControlFile,
    download_state: DownloadState,
    progress: ProgressDisplay,
    shutdown_flag: threading.Event,
    max_retries: int,
    max_redirects: int,
    segment_lock: threading.Lock,
    chunk_size: int = CHUNK_SIZE,
) -> bool:
    """Download a single byte-range segment to the output file.

    Writes data directly at the correct file offset via ``seek()``.
    Updates the shared progress display and control file periodically.

    Returns ``True`` when the segment finishes successfully, ``False``
    if it could not be completed (all retries exhausted or shutdown).
    """
    range_start = segment.start + segment.downloaded
    range_end = segment.end

    # Nothing to download
    if range_start > range_end:
        return True

    # We open the output file once per segment thread for efficiency.
    # The file must already exist and be pre-allocated.
    try:
        fd = os.open(output_path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    except FileNotFoundError:
        # Output file doesn't exist yet — shouldn't happen, but guard
        return False

    attempt = 0
    last_error: Exception | None = None
    last_control_flush = time.time()
    seg_downloaded = segment.downloaded

    try:
        while not shutdown_flag.is_set() and range_start <= range_end:
            try:
                attempt += 1

                req = urllib.request.Request(
                    url,
                    headers={
                        "Range": f"bytes={range_start}-{range_end}",
                        "User-Agent": DEFAULT_UA,
                        "Connection": "keep-alive",
                    },
                )
                resp = _OPENER.open(req, timeout=30)

                with resp:
                    # Read chunks and write at correct offset
                    while not shutdown_flag.is_set():
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break

                        write_offset = segment.start + seg_downloaded
                        os.lseek(fd, write_offset, os.SEEK_SET)
                        written = os.write(fd, chunk)
                        seg_downloaded += written
                        range_start = segment.start + seg_downloaded

                        # Update shared state
                        with segment_lock:
                            segment.downloaded = seg_downloaded
                            download_state.segments[segment.index]["downloaded"] = seg_downloaded

                        progress.update_segment(segment.index, seg_downloaded)

                        # Periodic control file flush
                        now = time.time()
                        if now - last_control_flush >= CONTROL_FLUSH_INTERVAL:
                            control.save(download_state)
                            last_control_flush = now

                # Segment completed
                if range_start > range_end:
                    with segment_lock:
                        segment.downloaded = seg_downloaded
                        download_state.segments[segment.index]["downloaded"] = seg_downloaded
                    control.save(download_state)
                    return True

                return True

            except urllib.error.HTTPError as e:
                last_error = e
                if 400 <= e.code < 500:
                    # Client error — don't retry
                    control.save(download_state)
                    return False
                # Server error — retry
                if attempt >= max_retries:
                    break

            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_error = e
                if attempt >= max_retries:
                    break

            # Backoff with jitter
            delay = (2 ** min(attempt - 1, 5)) + random.uniform(0, 1)
            shutdown_flag.wait(timeout=min(delay, 30))

        # ---- out of the loop ----

        if range_start > range_end:
            return True   # completed within retry loop

    finally:
        os.close(fd)

    # Exhausted retries or shutdown
    if shutdown_flag.is_set():
        control.save(download_state)

    if last_error is not None:
        # Don't print during download - it breaks the progress display
        # Error info is visible from the segment's 0% progress bar
        pass

    return False


def _make_ssl_context() -> ssl.SSLContext:
    """Create a permissive SSL context for downloading."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL_CONTEXT = _make_ssl_context()


def _make_opener() -> urllib.request.OpenerDirector:
    """Build an opener that uses our SSL context."""
    https_handler = urllib.request.HTTPSHandler(context=_SSL_CONTEXT)
    return urllib.request.build_opener(https_handler)


# Shared opener for all requests
_OPENER = _make_opener()


# ---------------------------------------------------------------------------
# HEAD / metadata
# ---------------------------------------------------------------------------

def _head_request(url: str, max_redirects: int = 10) -> dict[str, Any]:
    """Send a HEAD request and return response metadata.

    Returns a dict with keys ``content_length``, ``etag``,
    ``accept_ranges``, and ``final_url`` (after redirects).
    ``content_length`` is ``None`` if the server didn't report one.
    """
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": DEFAULT_UA},
    )
    try:
        resp = _OPENER.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        # Some servers reject HEAD — try GET with a tiny range instead
        if e.code in (403, 405):
            return _probe_via_range(url)
        raise

    with resp:
        cl = resp.headers.get("Content-Length")
        return {
            "content_length": int(cl) if cl else None,
            "etag": resp.headers.get("ETag"),
            "accept_ranges": resp.headers.get("Accept-Ranges", "none"),
            "final_url": resp.url or url,
        }


def _probe_via_range(url: str) -> dict[str, Any]:
    """Fallback probe: use a tiny Range GET when HEAD is rejected."""
    req = urllib.request.Request(
        url,
        headers={"Range": "bytes=0-0", "User-Agent": DEFAULT_UA},
    )
    resp = _OPENER.open(req, timeout=30)
    with resp:
        content_range = resp.headers.get("Content-Range", "")
        total = None
        if "/" in content_range:
            total_str = content_range.rsplit("/", 1)[-1]
            if total_str != "*":
                total = int(total_str)
        return {
            "content_length": total,
            "etag": resp.headers.get("ETag"),
            "accept_ranges": resp.headers.get("Accept-Ranges", "none"),
            "final_url": resp.url or url,
        }


# ---------------------------------------------------------------------------
# Segment partitioning
# ---------------------------------------------------------------------------

def _partition(total_size: int, segment_count: int) -> list[SegmentState]:
    """Split a file into *segment_count* equal byte ranges.

    Returns a list of ``SegmentState`` objects, each with ``start``
    and ``end`` (both inclusive) set.
    """
    if segment_count < 1:
        segment_count = 1

    seg_size = total_size // segment_count
    segments: list[SegmentState] = []

    for i in range(segment_count):
        start = i * seg_size
        end = total_size - 1 if i == segment_count - 1 else (i + 1) * seg_size - 1
        segments.append(SegmentState(index=i, start=start, end=end))

    return segments


def _total_downloaded(dl: list[SegmentState]) -> int:
    return sum(s.downloaded for s in dl)


# ---------------------------------------------------------------------------
# DownloadManager
# ---------------------------------------------------------------------------

class DownloadManager:
    """Orchestrate a resumable, multi-segment HTTP(S) download.

    Parameters:
        url: The URL to download.
        output: Local file path for the downloaded content.
        segments: Number of concurrent download segments (default 4).
        max_retries: Max retry attempts per segment chunk (default 5).
        max_redirects: HTTP redirect limit (default 10).
        resume: When ``False``, ignores any existing control file and
                starts a fresh download.  Defaults to ``True``.
    """

    def __init__(
        self,
        url: str,
        output: str,
        segments: int = 4,
        max_retries: int = 5,
        max_redirects: int = 10,
        resume: bool = True,
    ) -> None:
        self.url = url
        self.output = output
        self.segment_count = max(segments, 1)
        self.max_retries = max_retries
        self.max_redirects = max_redirects
        self.allow_resume = resume

        self.control = ControlFile(output)
        self._shutdown = threading.Event()
        self._segment_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> bool:
        """Execute the download and return ``True`` on success.

        This is the main entry point — call it after constructing
        a ``DownloadManager``.
        """
        self._install_signal_handler()

        # 1. Probe the server
        try:
            meta = _head_request(self.url, self.max_redirects)
        except urllib.error.HTTPError as e:
            print(f"Error: HTTP {e.code} — {e.reason}", file=sys.stderr)
            return False
        except urllib.error.URLError as e:
            print(f"Error: {e.reason}", file=sys.stderr)
            return False

        content_length = meta["content_length"]
        etag = meta.get("etag")
        final_url = meta["final_url"]
        supports_ranges = meta.get("accept_ranges", "none").lower() != "none"

        if content_length is None:
            print("Warning: Server did not report Content-Length. "
                  "Resume will not be available.", flush=True)
            return self._download_sequential(final_url)

        total_size = content_length

        if total_size == 0:
            print("Warning: File size is 0 bytes — creating empty file.")
            with open(self.output, "wb"):
                pass
            return True

        # 2. Determine segments (new or resume)
        segments = self._resolve_segments(total_size, etag, supports_ranges)

        # 3. Pre-allocate output file
        self._pre_allocate(total_size)

        # 4. Build initial download state
        dl_state = self.control.create_fresh_state(
            url=final_url,
            total_size=total_size,
            segments=segments,
            etag=etag,
        )
        self.control.save(dl_state)

        # 5. Print download info BEFORE starting progress display
        already_done = _total_downloaded(segments)
        print(f"  Downloading: {os.path.basename(self.output)}")
        print(f"  Size: {_format_size(total_size)}  "
              f"Segments: {len(segments)}  "
              f"Resume: {_format_size(already_done)} already done")
        print()

        # 6. Start progress display (after all prints are done)
        progress = ProgressDisplay(total_size, len(segments))
        progress.start()

        # 7. Launch segment workers

        start_time = time.time()
        success = True

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(segments)
            ) as executor:
                futures = []
                for seg in segments:
                    # Skip fully downloaded segments
                    if seg.downloaded >= (seg.end - seg.start + 1):
                        continue
                    f = executor.submit(
                        _download_segment,
                        url=final_url,
                        output_path=self.output,
                        segment=seg,
                        control=self.control,
                        download_state=dl_state,
                        progress=progress,
                        shutdown_flag=self._shutdown,
                        max_retries=self.max_retries,
                        max_redirects=self.max_redirects,
                        segment_lock=self._segment_lock,
                    )
                    futures.append((seg, f))

                # Wait for all segments to finish
                for seg, f in futures:
                    try:
                        result = f.result()
                        if not result and not self._shutdown.is_set():
                            success = False
                    except Exception as exc:
                        # Don't print during download - it breaks the progress display
                        success = False

                # Final speed sample
                total = _total_downloaded(segments)
                progress.add_sample(total)

        except KeyboardInterrupt:
            self._shutdown.set()
            # Give threads a moment to finalize
            time.sleep(0.5)

        finally:
            progress.stop()

        elapsed = time.time() - start_time
        total = _total_downloaded(segments)

        if self._shutdown.is_set():
            # Ctrl+C — save state and exit
            with self._segment_lock:
                dl_state.segments = [
                    {
                        "index": s.index,
                        "start": s.start,
                        "end": s.end,
                        "downloaded": s.downloaded,
                    }
                    for s in segments
                ]
            self.control.save(dl_state)
            print(f"\n\n  Downloaded {_format_size(total)}/{_format_size(total_size)} "
                  f"(resume with the same command)", flush=True)
            return False

        # Check completeness
        if total >= total_size:
            self.control.delete()
            speed = total / elapsed if elapsed > 0 else 0
            print(f"\n\n  Done!  "
                  f"{_format_size(total)} in {_format_time(elapsed)}  "
                  f"({_format_speed(speed)})", flush=True)
            return True
        else:
            if success:
                # Segments finished but we're somehow short — shouldn't happen
                self.control.delete()
                return True
            print(f"\n\n  Incomplete.  "
                  f"{_format_size(total)}/{_format_size(total_size)} downloaded.  "
                  f"Re-run to resume.", flush=True)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_segments(
        self, total_size: int, etag: str | None, supports_ranges: bool
    ) -> list[SegmentState]:
        """Determine segment layout: new or resumed from control file."""
        if not self.allow_resume:
            if self.control.exists():
                self.control.delete()
            return _partition(total_size, self.segment_count)

        state = self.control.load()

        if state is None:
            return _partition(total_size, self.segment_count)

        # Validate existing control file
        if state.total_size != total_size:
            print("  (server Content-Length changed — restarting download)", flush=True)
            self.control.delete()
            return _partition(total_size, self.segment_count)

        if state.etag and etag and state.etag != etag:
            print("  (server ETag changed — restarting download)", flush=True)
            self.control.delete()
            return _partition(total_size, self.segment_count)

        old_segments = self.control.segments_from_state(state)
        old_total = _total_downloaded(old_segments)

        if old_total >= total_size:
            # Already complete — just delete control file
            print("  (download already complete — removing control file)", flush=True)
            self.control.delete()
            return old_segments

        # Segment count may have changed — if so, re-partition and
        # redistribute progress proportionally
        if state.segment_count != self.segment_count:
            print(f"  (segment count changed {state.segment_count} → "
                  f"{self.segment_count} — redistributing progress)", flush=True)
            return self._redistribute(old_segments, total_size)

        return old_segments

    def _redistribute(
        self, old_segments: list[SegmentState], total_size: int
    ) -> list[SegmentState]:
        """Re-partition the file while preserving the downloaded byte count.

        When the user changes ``--segments`` on a resume, we create new
        segment boundaries and credit each new segment with the number of
        bytes already downloaded that fall within its range.
        """
        new_segments = _partition(total_size, self.segment_count)
        # Build a simple byte-range completion bitmap from old segments
        # We approximate: for each new segment, count bytes from old
        # segments whose range overlaps.
        for new_seg in new_segments:
            credited = 0
            for old in old_segments:
                overlap_start = max(new_seg.start, old.start + old.downloaded)
                overlap_end = min(new_seg.end, old.end)
                if overlap_start <= overlap_end:
                    credited += (overlap_end - overlap_start + 1)
            new_seg.downloaded = min(credited, new_seg.end - new_seg.start + 1)

        return new_segments

    def _pre_allocate(self, total_size: int) -> None:
        """Pre-allocate the output file to ``total_size`` bytes.

        Uses ``os.truncate()`` which creates a sparse file on most
        filesystems — no actual disk blocks are written.
        """
        # Create the output directory if needed
        out_dir = os.path.dirname(self.output)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if not os.path.exists(self.output):
            # Create + truncate
            with open(self.output, "wb") as f:
                f.truncate(total_size)
        else:
            # Existing file — ensure it's at least the right size
            cur = os.path.getsize(self.output)
            if cur < total_size:
                with open(self.output, "ab") as f:
                    f.truncate(total_size)

    def _download_sequential(self, final_url: str) -> bool:
        """Fallback: sequential download when Content-Length is unknown."""
        print("  Downloading (no resume support)...", flush=True)

        try:
            req = urllib.request.Request(
                final_url,
                headers={"User-Agent": DEFAULT_UA},
            )
            resp = _OPENER.open(req, timeout=60)
        except urllib.error.URLError as e:
            print(f"Error: {e.reason}", file=sys.stderr)
            return False

        with resp:
            with open(self.output, "wb") as f:
                total = 0
                start = time.time()
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    elapsed = time.time() - start
                    speed = total / elapsed if elapsed > 0 else 0
                    sys.stdout.write(
                        f"\r  {_format_size(total)}  "
                        f"{_format_speed(speed)}  elapsed {_format_time(elapsed)}"
                    )
                    sys.stdout.flush()

        elapsed = time.time() - start
        print(f"\n\n  ✓ Done!  {_format_size(total)} in {_format_time(elapsed)}", flush=True)
        return True

    def _install_signal_handler(self) -> None:
        """Install SIGINT + SIGTERM handlers that set the shutdown flag."""
        orig_int = signal.getsignal(signal.SIGINT)
        orig_term = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):
            self._shutdown.set()
            if signum == signal.SIGINT:
                orig = orig_int
            else:
                orig = orig_term
            if orig and orig not in (signal.SIG_DFL, signal.SIG_IGN):
                orig(signum, frame)

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except ValueError:
            # Can't set signal handler in a non-main thread; no big deal
            pass




# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def download(
    url: str,
    output: str | None = None,
    segments: int = 4,
    retries: int = 5,
    resume: bool = True,
) -> bool:
    """Download a file with resume support (programmatic API).

    Args:
        url: The URL to download.
        output: Local file path.  Derived from the URL when ``None``.
        segments: Concurrent segment count (default 4).
        retries: Max retries per segment chunk (default 5).
        resume: Allow resuming from a previous partial download.

    Returns:
        ``True`` if the download completed successfully.
    """
    if output is None:
        from .cli import extract_filename
        output = ex