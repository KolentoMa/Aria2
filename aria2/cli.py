"""CLI argument parsing and entry point for aria2."""

import argparse
import os
import sys
from urllib.parse import unquote, urlparse


def _fix_encoding() -> None:
    """Ensure stdout uses UTF-8 on Windows (avoids UnicodeEncodeError)."""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def extract_filename(url: str) -> str:
    """Extract a reasonable filename from a URL."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename:
        # Fallback to hostname if URL has no path
        host = parsed.hostname or "download"
        # Replace dots/special chars in hostname for a cleaner default
        filename = f"{host.replace('.', '_')}.downloaded"
    return filename


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="aria2",
        description="A lightweight resumable download manager with multi-segment support.",
        epilog="Examples:\n"
        "  aria2 https://example.com/file.zip\n"
        "  aria2 -s 8 https://example.com/file.zip\n"
        "  aria2 -o myfile.zip -r 10 https://example.com/file.zip",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "url",
        help="URL of the file to download",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output filename (default: extracted from URL)",
    )
    parser.add_argument(
        "-s", "--segments",
        type=int,
        default=4,
        help="Number of concurrent download segments (default: 4, max: 32)",
    )
    parser.add_argument(
        "-r", "--retries",
        type=int,
        default=5,
        help="Max retries per segment (default: 5)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        default=False,
        help="Force fresh download, ignore existing control file",
    )
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=10,
        help="Max HTTP redirects to follow (default: 10)",
    )

    if argv is None:
        argv = sys.argv[1:]

    args = parser.parse_args(argv)

    # Validate segment count
    if args.segments < 1:
        parser.error("--segments must be at least 1")
    if args.segments > 32:
        parser.error("--segments max is 32")

    if args.retries < 0:
        parser.error("--retries must be >= 0")

    return args


def main() -> None:
    """Entry point for the aria2 CLI."""
    _fix_encoding()
    args = parse_args()

    # Determine output filename
    output = args.output or extract_filename(args.url)

    # Avoid importing core until we're actually running
    from .core import DownloadManager

    manager = DownloadManager(
        url=args.url,
        output=output,
        segments=args.segments,
        max_retries=args.retries,
        max_redirects=args.max_redirects,
        resume=not args.no_resume,
    )

    try:
        success = manager.run()
    except KeyboardInterrupt:
        print("\nDownload interrupted. Progress saved — re-run to resume.")
        sys.exit(1)

    if not success:
        sys.exit(1)
