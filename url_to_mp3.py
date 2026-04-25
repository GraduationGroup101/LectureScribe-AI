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

def is_youtube_url(url: str) -> bool:
    return isinstance(url, str) and bool(YOUTUBE_REGEX.search(url))


# --------------------------------------------------
# 2) Force SINGLE video URL (Solution 2)
#    Removes playlist parameters and keeps v=VIDEO_ID
# --------------------------------------------------
def force_single_video_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # If no video id, return as-is
    if "v" not in query:
        return url

    clean_query = urlencode({"v": query["v"][0]})

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
    if not is_youtube_url(youtube_url):
        raise ValueError(" Input is NOT a valid YouTube URL")

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
    youtube_link = input("Enter YouTube lecture link: ").strip()

    try:
        mp3_file = download_youtube_mp3(youtube_link)
        print(f"\n MP3 ready at:\n{mp3_file}")
    except Exception as e:
        print(f"\n Error: {e}")
