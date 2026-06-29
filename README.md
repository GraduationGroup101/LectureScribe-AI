# LectureScribe AI

LectureScribe AI converts YouTube lectures into readable text. It downloads the
lecture audio, transcribes it with Faster-Whisper, optionally formats the
transcript with OpenRouter or Ollama, and keeps the text output for future cache hits.

Public website:

```text
https://lecturescribe.app
```

## Features

- Browser interface for submitting YouTube lecture URLs.
- Fast Output and Better Formatting modes.
- Live job stage, progress percentage, chunk progress, and estimated time.
- Persistent job history and a Previous Jobs page.
- YouTube video-ID cache across different URL formats.
- Original-language Faster-Whisper transcription.
- OpenRouter cloud cleaning with local Ollama fallback.
- Automatic MP3 deletion after a job completes successfully.
- Raw and cleaned transcript files remain available for reuse.

## Processing Modes

### Fast Output

Fast Output sends:

```json
{
  "clean": false
}
```

The pipeline reuses cached output when available. For a new transcript, it runs
Faster-Whisper and then tries OpenRouter. If OpenRouter is unavailable or its quota is
exhausted, the raw Whisper transcript is returned immediately. Ollama is not
used as a fallback in this mode.

### Better Formatting

Better Formatting sends:

```json
{
  "clean": true
}
```

The pipeline tries OpenRouter first. If OpenRouter is unavailable, it uses the local Ollama
model. The cleaned transcript is saved in the persistent cache.

## Workflow

1. The user submits a YouTube URL.
2. The API validates the URL and creates a UUID job.
3. The pipeline extracts the YouTube video ID.
4. It checks the cleaned transcript cache.
5. It checks existing files in `OutputForOllama/` and `OutputForWhisper/`.
6. If no reusable output exists, `yt-dlp` downloads the audio.
7. FFmpeg converts the audio to MP3.
8. Faster-Whisper creates the raw transcript.
9. OpenRouter is attempted for formatting.
10. Better Formatting uses Ollama if OpenRouter fails.
11. Transcript paths and cache metadata are saved.
12. The MP3 is deleted after successful job completion.
13. The frontend displays the cleaned transcript, or raw Whisper output when no
    cleaner result is available.

Failed or interrupted jobs can retain their MP3 because the audio may still be
needed to diagnose or retry the failed operation.

## Project Structure

| Path | Purpose |
| --- | --- |
| `api.py` | FastAPI server, frontend routes, background jobs, persistence, and progress |
| `MainCode_FasterWhisper.py` | Main cache, transcription, cleaning, and cleanup pipeline |
| `url_to_mp3.py` | YouTube validation, video-ID extraction, yt-dlp, and MP3 conversion |
| `clean_with_Llama.py` | OpenRouter and Ollama cleaners, chunking, and duplicate removal |
| `front/` | End-user website and Previous Jobs interface |
| `OutputForWhisper/` | Raw Faster-Whisper transcripts |
| `OutputForOllama/` | Cleaned OpenRouter or Ollama transcripts |
| `jobs.json` | Persistent API job history, ignored by Git |
| `transcript_cache.json` | Persistent cache keyed by YouTube video ID, ignored by Git |
| `downloads/` | Temporary MP3 storage, ignored by Git |

## Requirements

- Python 3.10 or newer.
- NVIDIA CUDA GPU for the current Faster-Whisper configuration.
- FFmpeg.
- A local Faster-Whisper `large-v3` model.
- Ollama for Better Formatting fallback.
- An OpenRouter API key for cloud formatting.

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

Install the configured Ollama model:

```powershell
ollama pull llama3.1:8b-instruct-q4_K_M
```

Create a local `.env` file:

```text
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=openai/gpt-oss-120b
OPENROUTER_MAX_TOKENS=8192
OPENROUTER_TIMEOUT_SECONDS=300
```

Do not commit `.env`.

## Machine-Specific Configuration

The current project contains Windows-specific paths in
`MainCode_FasterWhisper.py` and `url_to_mp3.py`:

- `MODEL_PATH`
- The FFmpeg directory added to `PATH`

Update these values when running the project on another computer.

Current Faster-Whisper configuration:

```python
MODEL_SIZE = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "int8_float16"
DEFAULT_LANGUAGE = "ar"
```

The initial prompt asks Whisper to preserve the original spoken language, use
Arabic script for Arabic speech, and retain English technical terms.

## Run Locally

Start Ollama:

```powershell
ollama serve
```

Start the API and website:

```powershell
python api.py
```

Open:

```text
http://127.0.0.1:8000
```

Health check:

```text
http://127.0.0.1:8000/health
```

Do not use Uvicorn `--reload` while jobs are running. Generated transcript,
cache, and job files can trigger a restart and interrupt the background worker.

You can also run the pipeline without the API:

```powershell
python MainCode_FasterWhisper.py
```

## API

Interactive API documentation is intentionally disabled because the public root
serves the end-user website.

Create a job:

```powershell
curl.exe -X POST http://127.0.0.1:8000/jobs `
  -H "Content-Type: application/json" `
  -d '{"youtube_url":"https://www.youtube.com/watch?v=VIDEO_ID","clean":true}'
```

Check one job:

```text
GET /jobs/JOB_UUID
```

List all jobs:

```text
GET /jobs
```

Get the preferred transcript:

```text
GET /jobs/JOB_UUID/transcript
```

Get raw Faster-Whisper output:

```text
GET /jobs/JOB_UUID/transcript?kind=raw
```

Job responses include:

- `status`
- `stage`
- `stage_label`
- `progress_percent`
- `current_step`
- `total_steps`
- `estimated_stage_seconds`
- `chunk_index`
- `chunk_total`

Only one job runs at a time. Additional jobs remain queued because the API uses
`ThreadPoolExecutor(max_workers=1)`.

## Cache And Storage

The cache key is the 11-character YouTube video ID, so watch, short, and playlist
URL variants resolve to the same lecture.

When cache reuse is enabled, lookup order is:

1. Valid entry in `transcript_cache.json`.
2. Matching cleaned file in `OutputForOllama/`.
3. Matching raw file in `OutputForWhisper/`.
4. Full download and transcription pipeline.

Set `use_cached_outputs` to `false` to bypass transcript reuse.

The MP3 is temporary. Successful downloaded jobs delete it, including Fast
Output jobs that return only raw Whisper text. Successful cache-hit jobs also
remove stale MP3 files associated with the same video ID.

## Public Domain

The website is exposed from the local laptop through the named Cloudflare
Tunnel `lecturescribe`:

```powershell
cloudflared tunnel --url http://127.0.0.1:8000 run lecturescribe
```

The public website works only while:

1. The laptop is powered on and online.
2. `python api.py` is running.
3. The Cloudflare tunnel is running.
4. Ollama is running when local fallback is required.

## Current Limitations

- Job history and transcript cache use JSON files instead of a database.
- Only one job is processed at a time.
- Running jobs cannot resume after the API process or laptop restarts.
- The API endpoints are public and do not currently require authentication.
- The public deployment depends on the laptop and Cloudflare Tunnel.
- Faster-Whisper and FFmpeg paths are machine-specific.

For production use, add authentication, rate limiting, database storage, a
durable task queue, and a dedicated GPU server.
