# LectureScribe-AI Workflow

This document explains the current website, API, transcription pipeline, cache, job storage, and deployment workflow.

## User Experience

The public website is:

```text
https://lecturescribe.app
```

The root URL opens the end-user interface directly. Users do not need Swagger or API knowledge.

The website contains:

- A form for entering a YouTube lecture URL.
- Fast Output and Better Formatting modes.
- A hidden job-status panel that appears after submission.
- Live pipeline stages, progress, estimated time, and rotating waiting notes.
- A transcript result panel with a Copy button.
- A separate Previous Jobs page at `/app/jobs`.

The old `/app` path still opens the main interface for compatibility.

Public Swagger, ReDoc, and OpenAPI schema routes are disabled:

- `/docs` returns `404`.
- `/redoc` returns `404`.
- `/openapi.json` returns `404`.

Disabling these pages removes developer controls from the end-user website. It is not an authentication system; the job endpoints still exist because the frontend needs them.

## Main Files

### `api.py`

The FastAPI server:

- Serves the frontend.
- Validates job requests.
- Creates and stores jobs.
- Runs one background transcription job at a time.
- Reports progress to the frontend.
- Saves job history to `jobs.json`.
- Returns completed transcript text.

### `front/`

The browser interface:

- `index.html`: new job, status, and transcript result.
- `jobs.html`: previous jobs.
- `app.js`: submission, polling, progress, estimates, and result loading.
- `jobs.js`: previous-job listing and navigation.
- `styles.css`: responsive interface styling.

### `MainCode_FasterWhisper.py`

The main pipeline:

- Extracts the YouTube video ID.
- Checks transcript cache.
- Downloads audio when required.
- Runs Faster Whisper.
- Tries Groq cleaning.
- Uses Ollama when Better Formatting allows fallback.
- Saves cache entries.
- Deletes the MP3 after successful cleaning.

### `url_to_mp3.py`

This module:

- Validates YouTube URLs.
- Extracts the video ID.
- Removes playlist parameters.
- Downloads audio with `yt-dlp`.
- Converts the audio to MP3 with FFmpeg.

### `clean_with_Llama.py`

This module contains both cleaner integrations:

- Groq cloud cleaner using `openai/gpt-oss-120b`.
- Local Ollama cleaner using `llama3.1:8b-instruct-q4_K_M`.
- Transcript chunking and duplicate cleanup.

The Groq key is loaded from:

```text
.env
```

The key must be stored as:

```text
GROQ_API_KEY=your_key_here
```

The `.env` file must not be committed to GitHub.

## Starting The Project

Install dependencies:

```powershell
pip install -r requirements.txt
```

Start Ollama if Better Formatting must have a local fallback:

```powershell
ollama serve
```

Ensure the configured model exists:

```powershell
ollama pull llama3.1:8b-instruct-q4_K_M
```

Start the API:

```powershell
python api.py
```

Equivalent Uvicorn command:

```powershell
uvicorn api:app --host 127.0.0.1 --port 8000
```

Do not use `--reload`. The project creates and changes MP3, transcript, cache, and job files while processing. Reload mode may restart the API during a job.

Local website:

```text
http://127.0.0.1:8000
```

Health check:

```text
http://127.0.0.1:8000/health
```

## Permanent Domain And Cloudflare Tunnel

The public domain uses a named Cloudflare Tunnel:

```text
lecturescribe.app
```

Tunnel name:

```text
lecturescribe
```

The domain routes through Cloudflare to:

```text
http://127.0.0.1:8000
```

Both hostnames are routed to the tunnel:

```text
lecturescribe.app
www.lecturescribe.app
```

Start the named tunnel:

```powershell
cloudflared tunnel --url http://127.0.0.1:8000 run lecturescribe
```

The public website works only while all of these are running:

1. The laptop is powered on and connected to the internet.
2. `python api.py` is running.
3. The named Cloudflare Tunnel is running.
4. Ollama is running when local cleaner fallback is needed.

Cloudflare provides HTTPS and hides the laptop's public IP. No router port forwarding is required.

## Frontend Request

When the user submits the form, `front/app.js` sends:

```http
POST /jobs
Content-Type: application/json
```

Example body:

```json
{
  "youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "clean": true,
  "skip_audio_cache": false,
  "use_cached_outputs": true,
  "language": "ar"
}
```

The frontend currently sends `language: "ar"`. The Whisper prompt instructs the model to preserve the original spoken language, Arabic script, and English technical terms, but the explicit language setting is still Arabic.

## Output Modes

### Fast Output

Frontend value:

```json
{
  "clean": false
}
```

Workflow:

1. Reuse a cached cleaned transcript if one exists.
2. Otherwise reuse a raw Whisper transcript if one exists.
3. Otherwise download and transcribe the lecture.
4. Try the Groq cleaner.
5. If Groq succeeds, return the cleaned result.
6. If Groq is unavailable, return the raw Whisper transcript.
7. Do not wait for Ollama fallback.

### Better Formatting

Frontend value:

```json
{
  "clean": true
}
```

Workflow:

1. Reuse a cached cleaned transcript if one exists.
2. Otherwise reuse a raw Whisper transcript if one exists.
3. Otherwise download and transcribe the lecture.
4. Try the Groq cleaner.
5. If Groq succeeds, return its cleaned result.
6. If Groq fails or is unavailable, run the local Ollama cleaner.
7. Save the cleaned result in the persistent transcript cache.
8. Delete the downloaded MP3 after successful cleaning.

## Complete Pipeline

For a new lecture:

1. The user enters a YouTube URL.
2. The frontend sends `POST /jobs`.
3. `validate_youtube_url(...)` rejects invalid URLs.
4. `submit_job(...)` creates a UUID job ID.
5. The job is saved as `queued`.
6. `ThreadPoolExecutor(max_workers=1)` starts it in the background.
7. `process_youtube_url(...)` extracts the YouTube video ID.
8. The pipeline checks `transcript_cache.json`.
9. It also searches existing Whisper and Ollama output files.
10. If no suitable cache exists, the MP3 is downloaded.
11. Faster Whisper transcribes the lecture.
12. The raw transcript is saved.
13. Groq is attempted first.
14. Ollama is used only when Better Formatting is selected and Groq is unavailable.
15. A successful cleaned transcript is saved.
16. The transcript cache is updated.
17. The downloaded MP3 is deleted after successful cleaning.
18. The job becomes `completed`.
19. The frontend requests and displays the transcript.

## Cache Workflow

The persistent transcript cache is:

```text
transcript_cache.json
```

The cache key is the YouTube `video_id`, not the exact URL. These URL forms therefore refer to the same cached video:

```text
https://www.youtube.com/watch?v=VIDEO_ID
https://youtu.be/VIDEO_ID
https://www.youtube.com/watch?v=VIDEO_ID&list=...
```

When `use_cached_outputs` is `true`, the lookup order is:

1. Valid cleaned entry in `transcript_cache.json`.
2. Existing matching file in `OutputForOllama/`.
3. Existing matching raw file in `OutputForWhisper/`.
4. Download and run the complete pipeline.

A cleaned cache entry is accepted only when its saved cleaned transcript path still exists. Missing files make the entry stale, so normal processing runs again.

Important result flags:

- `cache_key`: YouTube video ID.
- `used_cached_raw_transcript`: raw Whisper output was reused.
- `used_cached_cleaned_transcript`: cleaned output was reused.
- `cleaner_provider`: `groq`, `ollama`, or `null`.
- `cleaner_error`: Groq failure information when relevant.
- `audio_deleted`: whether MP3 deletion succeeded.
- `audio_delete_error`: deletion error, if any.

Setting `use_cached_outputs` to `false` bypasses transcript output reuse.

## File Storage

Downloaded MP3:

```text
downloads/
```

Raw Faster Whisper transcript:

```text
OutputForWhisper/
```

Cleaned Groq or Ollama transcript:

```text
OutputForOllama/
```

Persistent job history:

```text
jobs.json
```

Persistent transcript cache:

```text
transcript_cache.json
```

Raw and cleaned text files are kept. The MP3 is deleted only after a cleaner successfully creates the final output. If deletion fails, the job can still complete and records `audio_delete_error`.

## Job Progress

Possible job statuses:

- `queued`
- `running`
- `completed`
- `failed`

Progress stages:

- `queued`
- `checking_cache`
- `cache_hit`
- `downloading`
- `transcribing`
- `formatting`
- `saving`
- `completed`
- `failed`

Each job includes fields such as:

- `stage`
- `stage_label`
- `progress_percent`
- `current_step`
- `total_steps`
- `estimated_stage_seconds`
- `chunk_index`
- `chunk_total`

The frontend polls the job every 2.5 seconds and updates the progress panel. Estimated times are based on defaults and recent completed jobs, so they are approximate.

Only one job runs at a time because:

```python
ThreadPoolExecutor(max_workers=1)
```

Additional requests remain queued until the current job finishes.

## API Routes Used By The Frontend

### `GET /`

Returns the end-user website.

### `GET /app`

Compatibility path for the main website.

### `GET /app/jobs`

Returns the Previous Jobs page.

### `GET /health`

Response:

```json
{
  "status": "ok"
}
```

### `POST /jobs`

Creates a background job and immediately returns:

```json
{
  "job_id": "JOB_UUID",
  "status": "queued",
  "status_url": "/jobs/JOB_UUID",
  "transcript_url": "/jobs/JOB_UUID/transcript"
}
```

### `GET /jobs`

Returns saved jobs:

```json
{
  "jobs": []
}
```

Opening `/jobs` does not create a job because the browser performs a `GET` request. Job creation requires `POST /jobs`.

### `GET /jobs/{job_id}`

Returns job status, progress, request information, errors, and result paths.

### `GET /jobs/{job_id}/transcript`

Returns transcript content as plain text.

Cleaned transcript:

```text
/jobs/JOB_UUID/transcript?kind=cleaned
```

Raw transcript:

```text
/jobs/JOB_UUID/transcript?kind=raw
```

If a job has no cleaned output, the frontend automatically requests the raw transcript.

## Job Persistence And Restart Behavior

Jobs are saved to:

```text
jobs.json
```

Completed and failed jobs remain visible after an API restart.

A queued or running job cannot resume after the Python process stops because its background thread no longer exists. On startup, the API marks such jobs as failed with:

```text
API server restarted before this job finished.
```

The server must stay running for an active job to complete.

## Common Problems

### The public domain does not load

Confirm:

1. `python api.py` is running.
2. Port `8000` responds locally.
3. The `lecturescribe` Cloudflare Tunnel is running.
4. The laptop has internet access.

### Whisper finishes but no cleaner runs

Check the job result:

- If Fast Output was selected and Groq failed, raw Whisper output is expected.
- If Better Formatting was selected, Groq should be attempted first and Ollama should be the fallback.
- Confirm `GROQ_API_KEY` exists in `.env`.
- Confirm Ollama is running and the configured model is installed.

### A repeated URL returns an older result

The transcript cache is enabled by default. Submit with:

```json
{
  "use_cached_outputs": false
}
```

to force the pipeline to process it again.

### Transcript endpoint returns `404`

Possible causes:

- Incorrect job ID.
- Transcript file was manually removed.
- Requested `kind=cleaned` when only raw output exists.
- Job failed before producing the requested file.

Check `GET /jobs/{job_id}` for the current status and result paths.

### Server stops during a job

Do not run Uvicorn with `--reload`. Also keep the terminal/process alive and prevent the laptop from sleeping.

## Current Limitations

- Jobs and cache use JSON files instead of a database.
- Only one job runs at a time.
- Running jobs cannot resume after process or laptop restart.
- The configured Faster Whisper model and FFmpeg paths are machine-specific.
- Faster Whisper is configured for CUDA.
- The public website depends on this laptop and the Cloudflare Tunnel.
- Removing documentation pages does not replace API authentication or rate limiting.

For a production deployment, the next improvements should be authentication, request rate limiting, SQLite or PostgreSQL storage, a task queue, and a dedicated GPU server.
