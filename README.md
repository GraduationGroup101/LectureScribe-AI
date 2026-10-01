<div align="center">

# 🎓 LectureScribe AI

### Turn long YouTube lectures into clean, searchable text

**Faster-Whisper · FastAPI · FFmpeg · OpenRouter · Ollama · Background jobs · Persistent transcript cache**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Whisper](https://img.shields.io/badge/Faster--Whisper-large--v3-5A45FF)](https://github.com/SYSTRAN/faster-whisper)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-Audio-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![AI](https://img.shields.io/badge/LLM-OpenRouter_%7C_Ollama-111111)](#ai-cleaning)

🌐 **Live project:** [lecturescribe.app](https://lecturescribe.app)

</div>

---

## The idea

Students often have hours of recorded lectures but no practical way to search, review, or reuse what was said. LectureScribe AI converts a YouTube lecture into a readable transcript and keeps the result available for future requests.

The system is designed as a real processing pipeline rather than a one-shot script: it validates URLs, queues work, downloads audio, transcribes with Faster-Whisper, optionally cleans the result with an LLM, tracks live progress, caches outputs, and exposes everything through a browser interface.

---

## What makes it interesting

| Capability | Implementation |
| --- | --- |
| 🎙️ **Accurate transcription** | OpenRouter Whisper `large-v3` by default, with local Faster-Whisper fallback |
| 🌍 **Keeps the spoken language** | Arabic stays Arabic, English stays English; `auto` detects the language |
| ⚡ **Two processing modes** | Fast Output (Whisper + automatic paragraphs) or Better Formatting (AI headings and paragraphs) |
| 🤖 **Cloud + local AI** | OpenRouter for formatting with Ollama fallback in Better Formatting mode |
| 📥 **YouTube ingestion** | URL validation + `yt-dlp` + FFmpeg audio conversion |
| 🧵 **Background processing** | FastAPI job creation, queueing, stage tracking, and live progress |
| ♻️ **Persistent cache** | Reuse keyed by video, language, mode and format version across URL variants |
| 🧹 **Automatic cleanup** | Temporary MP3 files are deleted after successful processing |
| 🕘 **Job history** | Persistent job metadata and a Previous Jobs interface |
| 🌐 **Browser experience** | End-user web interface instead of CLI-only execution |

---

## Pipeline

```mermaid
flowchart LR
    A[YouTube URL] --> B[Validate + Extract Video ID]
    B --> C{Cached transcript?}
    C -- Yes --> H[Return saved transcript]
    C -- No --> D[yt-dlp Download]
    D --> E[FFmpeg → MP3]
    E --> F[OpenRouter Whisper with local fallback]
    F --> G{Formatting mode}
    G -- Fast --> I[Deterministic paragraphs, no LLM]
    G -- Better --> J[OpenRouter → Ollama fallback]
    I --> K[Save transcript]
    J --> K
    K --> L[Delete temporary MP3]
    L --> H
```

---

## Processing modes

Both modes keep every sentence in the language it was spoken. Nothing is
translated: Arabic stays in Arabic script, and English technical terms said in
English stay in Latin script.

### ⚡ Fast Output (`"clean": false`)

Whisper transcript with automatic paragraphs (no AI rewriting).

- Reuses cached output when available.
- Transcribes new lectures with OpenRouter Whisper, falling back to local Faster-Whisper if unavailable.
- Splits the text into sentences and paragraphs with deterministic rules; no LLM is called.

### ✨ Better Formatting (`"clean": true`)

AI formatting with headings and paragraphs, kept in the lecture's own language.

- Runs the same transcription pipeline.
- Formats with OpenRouter first, then local Ollama when it is enabled.
- A chunk the model mistranslates, truncates or drops falls back to the deterministic
  layout for that chunk only, so one bad chunk never discards the whole lecture.
- If no model returns a single usable chunk (no key, provider down, every chunk
  rejected), the job still completes with the deterministic layout but reports
  `mode: "fast"` (`result.requested_mode` stays `"formatted"` and
  `result.cleaner_provider` is `"formatter"`). It is cached as fast output, so the
  next formatted request runs the models again from the saved Whisper text.

### Language

`language` is `"auto"` (detect the spoken language) or a two-letter ISO 639-1 code
such as `"ar"` or `"en"`; anything else is rejected with HTTP 422. A request that
omits it is treated as Arabic, for older clients. Finished jobs report the language
the text is actually in as `detected_language`.

With `"auto"`, every five-minute audio part is first transcribed without a language.
The parts then vote by the amount of real speech each holds (Whisper's silence
fillers such as "Thank you." do not count), and only parts that came back in another
language than the majority are transcribed again in that language. A silent or
English-titled opening therefore cannot turn an Arabic lecture into English.

---

## AI cleaning

Lecture transcripts often contain repeated words, filler, broken punctuation, and technical terms mixed across Arabic and English. The cleaning stage improves readability without replacing the original speech with invented content.

The project supports:

- OpenRouter-compatible cloud models
- Local Ollama fallback
- Chunked transcript processing
- Duplicate cleanup
- Preservation of Arabic script and English technical terminology

---

## Background job model

The web API exposes each lecture as a job with observable state:

```text
status                      queued | running | completed | failed
stage, stage_label
progress_percent            never moves backwards within a job
current_step / total_steps
stage_started_at, estimated_stage_seconds
chunk_index / chunk_total
jobs_ahead                  queued jobs only: jobs the worker runs first
submitted_at, started_at, finished_at, error
title, video_id, language, mode, detected_language,
format_version, video_duration_seconds
request, result
```

`title` and `video_duration_seconds` appear as soon as the audio is downloaded.
`mode` is `formatted` or `fast`: the requested mode while the job waits or runs, and
the mode of the transcript actually produced once it completes (see Better
Formatting above; `request.clean` keeps what was asked). `format_version` is set only
for output of the current pipeline (older results report `null`).

Only one heavy transcription job is executed at a time using `ThreadPoolExecutor(max_workers=1)`, while additional jobs wait in the queue. This avoids uncontrolled GPU contention on the host machine.

- **Duplicates share one job.** A request for a lecture that is already queued or
  running with the same language and mode returns that job (`"deduplicated": true`)
  and does not use a rate-limit slot.
- **Saved output answers at once.** When an earlier job of the current pipeline
  version still has its transcript files for the same video, language and mode,
  the new job is created already completed (`"cached": true`) instead of waiting in
  the queue. Each caller may be answered this way `JOB_REUSE_PER_CLIENT` times an
  hour (default 20), and the whole service `JOB_REUSE_GLOBAL` times (default 200,
  never more than half of `JOB_HISTORY_LIMIT`); past that, the request is queued
  like any other and counts against the normal limits.
- **Restarts.** Running jobs are marked failed on restart; jobs that were still
  waiting are queued again. The newest `JOB_HISTORY_LIMIT` (default 500) finished
  jobs are kept, plus any finished job whose finish notification is still being
  delivered. Progress is written to `jobs.json` at most every 2 seconds, and at
  once when the stage or status changes.

---

## Cache strategy

The cache uses the 11-character YouTube video ID instead of the raw URL, so different URL forms can resolve to the same lecture. Entries are also keyed by the requested language, the mode and the format version, so an English result is never served for an Arabic request and output of an older pipeline is never reused.

Lookup order:

1. Valid entry in `transcript_cache.json`
2. Existing cleaned transcript
3. Existing raw transcript
4. Full download + transcription pipeline

This turns repeated requests from a compute-heavy GPU task into an immediate file lookup.

---

## Tech stack

**Backend**  
Python · FastAPI · background jobs · persistent JSON metadata

**Speech & media**  
Faster-Whisper · CUDA · FFmpeg · yt-dlp

**AI formatting**  
OpenRouter · Ollama · chunked transcript cleaning

**Frontend**  
Browser-based submission · progress UI · job history · transcript viewer

**Delivery**  
Cloudflare Tunnel · GitHub · environment-based secrets

---

## Project structure

```text
api.py                       # FastAPI server, frontend routes, jobs, callbacks, keep-alive
MainCode_FasterWhisper.py    # Transcription + cache + cleaning pipeline
url_to_mp3.py                # URL validation, yt-dlp and FFmpeg conversion
clean_with_Llama.py          # OpenRouter/Ollama cleaning logic
transcript_format.py         # Language detection and deterministic transcript layout
front/                       # Web interface and Previous Jobs UI
tests/                       # Unit tests (no network, no Whisper model)
OutputForWhisper/            # Raw transcripts
OutputForOllama/             # Cleaned transcripts
```

---

## Run locally

### Requirements

- Python 3.10+
- FFmpeg
- OpenRouter API key with transcription credits
- Faster-Whisper `large-v3` for local fallback (CUDA or CPU)
- Ollama for local fallback
- OpenRouter API key for cloud formatting (the same key)

```bash
pip install -r requirements.txt
```

Set your key in the ignored `.env` file; `.env.example` lists the supported settings:

```dotenv
OPENROUTER_API_KEY=your-key
WHISPER_BACKEND=openrouter
OPENROUTER_TRANSCRIPTION_MODEL=openai/whisper-large-v3
OPENROUTER_TRANSCRIPTION_TIMEOUT_SECONDS=120
OPENROUTER_AUDIO_CHUNK_SECONDS=300
WHISPER_LOCAL_FALLBACK=true
OLLAMA_LOCAL_FALLBACK=true
```

The formatter (Better Formatting only) also reads `OPENROUTER_MODEL`,
`OPENROUTER_MAX_TOKENS` (default 8192), `OPENROUTER_TIMEOUT_SECONDS` (default 300) and
`OPENROUTER_REASONING_EFFORT` (default `low`, so a reasoning model spends its tokens
on the transcript; `none` or `off` stops sending it, and it is dropped automatically
when the model rejects it). The Ollama fallback reads `OLLAMA_NUM_PREDICT` (default
4096) and `OLLAMA_NUM_CTX` (default 8192), sized so an Arabic chunk is not cut off.
Invalid numbers fall back to the defaults.

Both modes prefer cloud transcription and save original-language text in
`OutputForWhisper/`. Long audio is converted into temporary mono five-minute MP3
chunks, sent sequentially, and removed afterwards. A cloud failure (including missing
credentials, insufficient credits, timeout, or an empty response) retries the original
audio with local Faster-Whisper. Partial cloud transcripts are never saved as a result.
Job metadata includes `transcription_provider` and `transcription_info`; a local
fallback records its `cloud_error` there. Existing cache hits still skip transcription.

For local fallback, `WHISPER_DEVICE=auto` selects CUDA when available, otherwise CPU;
`WHISPER_COMPUTE_TYPE=auto` selects `int8_float16` on CUDA or `int8` on CPU. Use
`WHISPER_MODEL_SIZE` or `WHISPER_MODEL_PATH` to override the local model.
Set `WHISPER_BACKEND=local` to run only locally. Set `WHISPER_LOCAL_FALLBACK=false`
on cloud servers too small to run a local model.

Cloud transcription can run without importing the local Whisper/CUDA libraries.
It still requires FFmpeg for audio download/conversion and chunking.

### Render preview

The root `Dockerfile` installs FFmpeg, Node.js, and the cloud-only dependencies in
`requirements-render.txt`. Create a Docker web service from this repository on the
`main` branch, using the Free plan and `/health` as the health check. Set
`OPENROUTER_API_KEY` as a secret in Render; do not upload `.env`.
The public `POST /jobs` endpoint accepts at most 3 jobs per client IP an hour
(`JOB_RATE_PER_IP`). Server-wide, 60 jobs an hour (`JOB_RATE_GLOBAL`) and 10
active or queued jobs (`JOB_MAX_ACTIVE`) are accepted. Excess requests receive
HTTP 429 and a `Retry-After` header. Polling and transcript reads are not rate
limited. These in-memory limits reset when the process restarts and assume the
Dockerfile's single Uvicorn worker.

**Per-student limits for a gateway.** EduFusion submits for many students from
one address, so it identifies itself with a shared secret and names the account:

- Set `GATEWAY_KEYS` to one or more comma-separated random secrets in Render.
- The gateway sends `X-Gateway-Key: <secret>` and `X-Gateway-User: <account>`
  (letters, digits, `.`, `_`, `:`, `-`; up to 120 characters).
- Each account then gets its own allowance of `JOB_RATE_PER_USER` jobs an hour
  (default 6) instead of the address limit. The server-wide and queue limits
  still apply. A gateway request without an account name shares one bucket of
  the same size; the account header is ignored without a valid key.
- Jobs submitted with a valid key are hidden from the public `GET /jobs` list (and
  the History page); a caller presenting the key sees them. Reading one job or its
  transcript by ID stays open, because the random ID is known only to its submitter.
  Keys are never written to `jobs.json`.

**Finish notifications for a gateway.** A gateway's POST may include
`"callback_url": "https://…"`. It is honoured only with a valid `X-Gateway-Key` and
an `https://` URL (set `CALLBACK_ALLOW_HTTP=true` to allow `http://` for local
tests); otherwise it is silently ignored. When the job completes or fails, the
service POSTs `{"event":"job.finished","job_id":"…","status":"completed|failed"}` with:

```text
X-LectureScribe-Timestamp: <unix seconds>
X-LectureScribe-Signature: sha256=<hex HMAC-SHA256(key, "<timestamp>.<job_id>.<status>")>
```

`key` is the gateway key that submitted the job. Each callback is tried up to 5
times (after about 5 s, 30 s, 2 min, 5 min and 10 min, 30-second timeout, so a
gateway outage of about a quarter of an hour is ridden out) on a background thread;
a permanent 4xx answer stops the retries, and a duplicate submission adds its
callback to the running job. The gateway should reject signatures older than a few
minutes and then read the job and transcript.

**Keep-alive while jobs run.** Render Free stops an instance after about 15 minutes
without inbound requests, even mid-job, and its disk (every finished transcript) is
wiped with it. The service requests its own `${RENDER_EXTERNAL_URL}/health` every
`KEEPALIVE_INTERVAL_SECONDS` (default 240) while any job is queued or running, while
a finish notification is still being delivered, and for `KEEPALIVE_GRACE_SECONDS`
(default 900; `0` turns it off) after the last job finished, so a gateway can still
fetch the result. Render sets `RENDER_EXTERNAL_URL` automatically; the keep-alive is
off when it is unset or `KEEPALIVE_DISABLED=true`.

**YouTube blocks downloads from datacenter addresses** ("Sign in to confirm you're
not a bot"), including Render. Give yt-dlp a way in through the environment:

- `YTDLP_COOKIES_FILE`: path to a Netscape-format cookies export from a signed-in
  (preferably throwaway) YouTube account. On Render, upload it as a **Secret File**
  named `youtube-cookies.txt` and set the variable to `/etc/secrets/youtube-cookies.txt`.
  The downloader copies it into a private temporary directory because yt-dlp saves
  cookie updates on exit. The same copy is used for metadata and audio extraction,
  then deleted even if downloading fails; the uploaded secret is never modified.
  See the yt-dlp wiki on exporting cookies; refresh the file when YouTube signs the
  account out.
- `YTDLP_PROXY`: a residential or trusted proxy URL (`http://user:pass@host:port`,
  `socks5://host:port`).
- `YTDLP_PLAYER_CLIENTS`: comma-separated player clients to try, e.g. `android,web_embedded`.

Without one of these, cloud transcription of new videos fails with that error; cached
transcripts are still served.

Render Free spins down after 15 idle minutes. Its filesystem is ephemeral, so
`jobs.json`, `transcript_cache.json`, and generated transcripts can disappear after
a restart or redeploy. It is suitable for a short demonstration, not durable job
history. EduFusion stores every finished transcript in its own database (through
the finish notification above), so its students do not depend on this disk. Public
callers can still consume OpenRouter credits within the limits, and a public job's
history entry and transcript are visible to anyone with the service URL. Rate
limiting is not access control or a guaranteed spending cap. Downloading YouTube videos
from a datacenter IP may be blocked. See [Render Free limitations](https://render.com/docs/free).

Start Ollama when using the local fallback:

```bash
ollama serve
```

Start the app:

```bash
python api.py
```

Then open:

```text
http://127.0.0.1:8000
```

> Do not use Uvicorn `--reload` while jobs are running; generated job/cache files can trigger a restart during processing.

---

## API examples

For the Oracle VM deployment kit, SSH upload command, persistent state layout,
and Cloudflare domain setup, see [Oracle deployment](deploy/oracle/README.md).

Create a job:

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=VIDEO_ID","clean":true,"language":"auto"}'
```

The answer (HTTP 202) is
`{"job_id", "status", "status_url", "transcript_url", "submitted_at"}`, plus
`"deduplicated": true` or `"cached": true` when an existing job or saved transcript
answered the request.

Useful endpoints:

```text
GET /health                               {"status":"ok","active_jobs":<running>,"queued_jobs":<waiting>}
GET /jobs                                 public jobs (gateway jobs need X-Gateway-Key)
GET /jobs/{job_id}
GET /jobs/{job_id}/transcript             formatted Markdown (X-Transcript-Format: markdown)
GET /jobs/{job_id}/transcript?kind=raw    Whisper text with paragraph breaks (X-Transcript-Format: plain)
```

Transcripts are `text/plain; charset=utf-8` and carry `Content-Language` when the
spoken language is known. The formatted kind uses a small Markdown subset:
`#`/`##`/`###` headings, blank-line paragraphs, `- ` bullets, `1. ` items and `**bold**`.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Run from the repository root (the API mounts `./front` and reads `./jobs.json`).
The tests need no network and no local Whisper model.

---

## Engineering lessons

Building LectureScribe AI required solving more than speech-to-text. The project combines:

- long-running background work,
- GPU-bound processing,
- media conversion,
- cloud/local model fallback,
- caching and idempotency,
- progress reporting,
- cleanup of temporary assets,
- and a usable web experience around the pipeline.

That combination is what turns a transcription script into an actual application.

---

<div align="center">

### Graduation Project — LectureScribe AI

Built by **GraduationGroup101**

[Live Website](https://lecturescribe.app) · [Repository](https://github.com/GraduationGroup101/LectureScribe-AI)

</div>
