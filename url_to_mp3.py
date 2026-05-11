import os
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from pathlib import Path
import yt_dlp

os.environ["PATH"] += os.pathsep + r"C:\Users\Mahmoud\Downloads\Compressed\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"

# --------------------------------------------------
# 1) Check if URL is a YouTube link
# --------------------------------------------------
YOUTUBE_REGEX = re.compile(
    r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/'
)
YOUTUBE_VIDEO_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{11}$")

def is_youtube_url(url: str) -> bool:
    return extract_youtube_video_id(url) is not None


def extract_youtube_video_id(url: str) -> str | None:
    """Return the YouTube video id when the input is a valid video URL."""
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
    """Validate user input before yt-dlp runs."""
    if not isinstance(url, str) or not url.strip():
        return False, "URL cannot be empty."

    if extract_youtube_video_id(url) is None:
        return False, (
            "Enter a valid YouTube video URL, for example: "
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    return True, ""


def prompt_for_youtube_url(prompt: str = "Enter YouTube URL: ") -> str:
    """Keep asking until the user enters a valid YouTube video URL."""
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
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise ValueError("Input is NOT a valid YouTube video URL")

    clean_query = urlencode({"v": video_id})

    return urlunparse((
        "https",                 # scheme
        "www.youtube.com",       # netloc
        "/watch",                # path
        "",                      # params
        clean_query,             # query
        ""                       # fragment
    ))


# --------------------------------------------------
# 3) Download YouTube audio as MP3
# --------------------------------------------------
def download_youtube_mp3(youtube_url: str, output_dir="downloads") -> str:
    is_valid, error = validate_youtube_url(youtube_url)
    if not is_valid:
        raise ValueError(error)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 🔑 Solution 2: force single video URL
    clean_url = force_single_video_url(youtube_url)
    print(f" Clean URL used:\n{clean_url}\n")

    # --------------------------------------------------
    # 1) Get video info WITHOUT downloading
    # --------------------------------------------------
    info_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(clean_url, download=False)
        title = info.get("title", "lecture")

    # --------------------------------------------------
    # 2) If file exists → return it
    # --------------------------------------------------
    expected_mp3 = output_dir / f"{title}.mp3"
    if expected_mp3.exists():
        print(f"♻️ Lecture already exists. Using cached file:\n{expected_mp3}")
        return str(expected_mp3)

    # --------------------------------------------------
    # 3) Download & convert to MP3
    # --------------------------------------------------
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
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

    # yt-dlp may sanitize characters in a way that differs from the raw title.
    # Use yt-dlp's own filename resolution first, then fall back to a directory search.
    resolved_mp3 = Path(yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info)).with_suffix(".mp3")
    if resolved_mp3.exists():
        print(f" Download completed:\n{resolved_mp3}")
        return str(resolved_mp3)

    matches = sorted(
        output_dir.glob("*.mp3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if matches:
        latest = matches[0]
        print(f" Download completed:\n{latest}")
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
