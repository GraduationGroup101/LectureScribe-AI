import os
import re
import sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from pathlib import Path
import yt_dlp

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

os.environ["PATH"] += os.pathsep + r"C:\Users\Mahmoud\Downloads\Compressed\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"

# --------------------------------------------------
# 1) Check if URL is a YouTube link
# --------------------------------------------------
YOUTUBE_REGEX = re.compile(
    r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/'
)
YOUTUBE_VIDEO_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{11}$")

def is_youtube_url(url: str) -> bool:
    """Check whether a value identifies a supported YouTube video.

    Purpose:
        Provide a simple Boolean validation helper for callers.
    Args:
        url: Candidate YouTube URL.
    Returns:
        True when a valid video ID can be extracted; otherwise False.
    Workflow:
        Delegates all parsing and validation to `extract_youtube_video_id`.
    Connects to:
        Calls `extract_youtube_video_id`; available to API or CLI validation code.
    """
    return extract_youtube_video_id(url) is not None


def extract_youtube_video_id(url: str) -> str | None:
    """Extract the 11-character video ID from a supported YouTube URL.

    Purpose:
        Normalize common YouTube URL formats to one stable cache key.
    Args:
        url: Full or scheme-less YouTube watch, short, embed, live, or Shorts URL.
    Returns:
        The validated video ID, or None when the input is unsupported or invalid.
    Workflow:
        Adds a missing scheme, parses the URL, selects the ID by host/path format,
        and validates it with `YOUTUBE_VIDEO_ID_REGEX`.
    Connects to:
        Used by validation, URL canonicalization, downloading, and transcript caching.
    """
    if not isinstance(url, str):
        return None

    url = url.strip()
    if not url or any(char.isspace() for char in url):
        return None

    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = f"https://{url}"

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if scheme not in {"http", "https"}:
        return None

    if host.startswith("www."):
        host = host[4:]

    video_id = None
    if host == "youtu.be":
        video_id = path.split("/", 1)[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif path.startswith(("shorts/", "embed/", "live/")):
            video_id = path.split("/")[1]

    if video_id and YOUTUBE_VIDEO_ID_REGEX.fullmatch(video_id):
        return video_id

    return None


def validate_youtube_url(url: str) -> tuple[bool, str]:
    """Validate a user-provided YouTube URL and produce a readable error.

    Purpose:
        Reject empty or unsupported input before starting network or pipeline work.
    Args:
        url: Candidate URL supplied by an API or CLI user.
    Returns:
        A `(valid, error_message)` tuple; the message is empty when valid.
    Workflow:
        Checks for empty input, then asks `extract_youtube_video_id` to validate it.
    Connects to:
        Called by `prompt_for_youtube_url`, `download_youtube_mp3`, and API job submission.
    """
    if not isinstance(url, str) or not url.strip():
        return False, "URL cannot be empty."

    if extract_youtube_video_id(url) is None:
        return False, (
            "Enter a valid YouTube video URL, for example: "
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    return True, ""


def prompt_for_youtube_url(prompt: str = "Enter YouTube URL: ") -> str:
    """Prompt repeatedly until the user enters a valid YouTube video URL.

    Purpose:
        Support safe interactive use of the downloader and main pipeline.
    Args:
        prompt: Text displayed for each input attempt.
    Returns:
        The validated URL string entered by the user.
    Workflow:
        Reads input, validates it, and prints the validation error before retrying.
    Connects to:
        Calls `validate_youtube_url`; used by this module and pipeline CLI entry points.
    """
    while True:
        youtube_url = input(prompt).strip()
        is_valid, error = validate_youtube_url(youtube_url)
        if is_valid:
            return youtube_url
        print(f"Invalid URL: {error}")


# --------------------------------------------------
# 2) Force SINGLE video URL (Solution 2)
#    Removes playlist parameters and keeps v=VIDEO_ID
# --------------------------------------------------
def force_single_video_url(url: str) -> str:
    """Convert any supported YouTube URL into a canonical single-video URL.

    Purpose:
        Remove playlist and tracking parameters so yt-dlp processes one video only.
    Args:
        url: Valid YouTube video URL in any supported format.
    Returns:
        A canonical `https://www.youtube.com/watch?v=VIDEO_ID` URL.
    Raises:
        ValueError: If no valid video ID can be extracted.
    Workflow:
        Extracts the ID, builds a query containing only `v`, and reconstructs the URL.
    Connects to:
        Calls `extract_youtube_video_id`; used by downloading and pipeline cache metadata.
    """
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise ValueError("Input is NOT a valid YouTube video URL")

    clean_query = urlencode({"v": video_id})

    return urlunparse((
        "https",                 # scheme
        "www.youtube.com",       # netloc
        "/watch",                # path
        "",                      # params
        clean_query,              # query
        ""                       # fragment
    ))


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Convert a video title into a Windows-safe filename component.

    Purpose:
        Prevent invalid characters and overly long YouTube titles from breaking files.
    Args:
        name: Original video title or filename component.
        max_length: Maximum number of characters to retain.
    Returns:
        A cleaned non-empty filename component.
    Workflow:
        Replaces forbidden characters, normalizes whitespace, trims trailing dots and
        spaces, limits length, and falls back to `lecture`.
    Connects to:
        Called by `download_youtube_mp3` before output paths are created.
    """
    name = name.strip()
    name = re.sub(r"[<>:\"/\\|?*\n\r\t]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or "lecture"


# --------------------------------------------------
# 3) Download YouTube audio as MP3
# --------------------------------------------------
def download_youtube_mp3(
    youtube_url: str,
    output_dir="downloads",
    skip_cache: bool = False,
) -> str:
    """Download one YouTube video's audio and convert it to MP3.

    Purpose:
        Produce the local audio input required by Faster-Whisper.
    Args:
        youtube_url: YouTube video URL to process.
        output_dir: Directory where MP3 files are stored.
        skip_cache: When True, download again instead of reusing an existing MP3.
    Returns:
        String path to the downloaded or cached MP3 file.
    Raises:
        ValueError: If the URL is invalid.
        FileNotFoundError: If yt-dlp finishes without producing an MP3.
        yt_dlp errors: When metadata extraction, downloading, or FFmpeg conversion fails.
    Workflow:
        Validates and canonicalizes the URL, fetches metadata, creates a stable filename,
        checks the audio cache, then runs yt-dlp with FFmpeg MP3 conversion.
    Connects to:
        Calls URL helpers and `sanitize_filename`; called by `process_youtube_url`.
    """
    is_valid, error = validate_youtube_url(youtube_url)
    if not is_valid:
        raise ValueError(error)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_url = force_single_video_url(youtube_url)
    print(f"Clean URL used:\n{clean_url}\n")

    info_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(clean_url, download=False)
        title = sanitize_filename(info.get("title", "lecture"))
        video_id = info.get("id") or extract_youtube_video_id(clean_url)

    filename_base = f"{video_id}_{title}" if video_id else title
    expected_mp3 = output_dir / f"{filename_base}.mp3"

    if expected_mp3.exists() and not skip_cache:
        print(f" Lecture already exists. Using cached file:\n{expected_mp3}")
        return str(expected_mp3)

    if not skip_cache:
        cached = sorted(
            output_dir.glob(f"{video_id}_*.mp3"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if cached:
            print(f" Found cached file for video id {video_id}:\n{cached[0]}")
            return str(cached[0])

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{filename_base}.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": False,
        "concurrent_fragment_downloads": 1,
        "no_warnings": False,
        "extract_flat": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)

    resolved_mp3 = output_dir / f"{filename_base}.mp3"
    if resolved_mp3.exists():
        print(f"Download completed:\n{resolved_mp3}")
        return str(resolved_mp3)

    matches = sorted(
        output_dir.glob(f"{video_id}_*.mp3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if matches:
        latest = matches[0]
        print(f"Download completed:\n{latest}")
        return str(latest)

    raise FileNotFoundError(
        f"yt-dlp finished, but no MP3 file was found in {output_dir.resolve()}"
    )


# --------------------------------------------------
# 4) Example usage
# --------------------------------------------------
if __name__ == "__main__":
    youtube_link = prompt_for_youtube_url("Enter YouTube lecture link: ")

    try:
        mp3_file = download_youtube_mp3(youtube_link)
        print(f"\n MP3 ready at:\n{mp3_file}")
    except Exception as e:
        print(f"\n Error: {e}")
