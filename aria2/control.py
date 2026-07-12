"""Control file management for resumable downloads.

Stores download progress as a .aria2.json sidecar file so interrupted
downloads can be resumed without re-downloading completed segments.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SegmentState:
    """State of a single download segment."""

    index: int
    start: int       # byte offset (inclusive)
    end: int         # byte offset (inclusive)
    downloaded: int = 0  # bytes downloaded for this segment


@dataclass
class DownloadState:
    """Complete download state serialized to .aria2.json."""

    url: str
    output_path: str
    total_size: int
    segment_count: int
    segments: list[dict]   # serialized SegmentState as dicts
    etag: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class ControlFile:
    """Manages the .aria2.json control file for a download.

    Provides thread-safe read and atomic write of download progress
    so that interrupted downloads can resume from the last saved state.
    """

    def __init__(self, output_path: str) -> None:
        """Initialise control file path.

        Args:
            output_path: Path to the download output file. The control
                         file will be at ``output_path + '.aria2.json'``.
        """
        self.output_path = output_path
        self.control_path = output_path + ".aria2.json"
        self.tmp_path = self.control_path + ".tmp"
        self._lock = threading.Lock()

    def exists(self) -> bool:
        """Check whether a control file already exists on disk."""
        return os.path.isfile(self.control_path)

    def load(self) -> DownloadState | None:
        """Load an existing control file.

        Returns:
            A ``DownloadState`` if the file exists and is valid, or
            ``None`` otherwise.
        """
        if not self.exists():
            return None

        try:
            with open(self.control_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            state = DownloadState(
                url=data["url"],
                output_path=data["output_path"],
                total_size=data["total_size"],
                segment_count=data["segment_count"],
                segments=data["segments"],
                etag=data.get("etag"),
                created_at=data.get("created_at", time.time()),
                updated_at=data.get("updated_at", time.time()),
            )
            return state
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _build_json(self, state: DownloadState) -> dict:
        """Convert a DownloadState to a JSON-serialisable dict."""
        state.updated_at = time.time()
        return {
            "url": state.url,
            "output_path": state.output_path,
            "total_size": state.total_size,
            "segment_count": state.segment_count,
            "segments": state.segments,
            "etag": state.etag,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }

    def save(self, state: DownloadState) -> None:
        """Atomically write the download state to the control file.

        Writes to a temporary file first, then atomically replaces the
        target via ``os.replace()``.  Thread-safe — callable from any
        segment worker thread.
        """
        with self._lock:
            data = self._build_json(state)
            try:
                with open(self.tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(self.tmp_path, self.control_path)
            except OSError:
                # If the write or replace fails, the worst case is we
                # lose some progress data; the download can still
                # continue or resume from the previous state.
                pass

    def delete(self) -> None:
        """Delete the control file after a successful download."""
        with self._lock:
            try:
                if os.path.isfile(self.control_path):
                    os.remove(self.control_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Convenience helpers used by DownloadManager
    # ------------------------------------------------------------------

    def create_fresh_state(
        self,
        url: str,
        total_size: int,
        segments: list[SegmentState],
        etag: str | None = None,
    ) -> DownloadState:
        """Build a fresh ``DownloadState`` from segment definitions.

        Args:
            url: Download URL.
            total_size: Total file size in bytes.
            segments: List of ``SegmentState`` objects.
            etag: Optional HTTP ETag from the server.
        """
        seg_dicts = [
            {
                "index": s.index,
                "start": s.start,
                "end": s.end,
                "downloaded": s.downloaded,
            }
            for s in segments
        ]
        return DownloadState(
            url=url,
            output_path=self.output_path,
            total_size=total_size,
            segment_count=len(segments),
            segments=seg_dicts,
            etag=etag,
        )

    @staticmethod
    def segments_from_state(state: DownloadState) -> list[SegmentState]:
        """Reconstruct ``SegmentState`` objects from a ``DownloadState``."""
        return [
            SegmentState(
                index=s["index"],
                start=s["start"],
                end=s["end"],
                downloaded=s["downloaded"],
            )
            for s in state.segments
        ]
