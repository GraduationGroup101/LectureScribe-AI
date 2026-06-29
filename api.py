from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
from threading import Lock
from time import time
from uuid import uuid4

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from MainCode_FasterWhisper import (
    DEFAULT_LANGUAGE,
    list_ollama_output_filenames,
    process_youtube_url,
)
from url_to_mp3 import validate_youtube_url


app = FastAPI(
    title="LectureScribe-AI API",
    description="Download lecture audio, transcribe it with faster-whisper, and optionally clean it with Groq or Ollama.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/front", StaticFiles(directory="front"), name="front")

executor = ThreadPoolExecutor(max_workers=1)
JOBS_FILE = Path("jobs.json")
jobs: dict[str, dict] = {}
jobs_lock = Lock()
TOTAL_STEPS = {
    True: 5,
    False: 4,
}
STAGE_DEFAULTS = {
    "queued": {
        "label": "Waiting for the current job to finish.",
        "progress": 0,
        "step": 0,
        "estimate": 30,
    },
    "checking_cache": {
        "label": "Checking if this lecture was processed before.",
        "progress": 5,
        "step": 1,
        "estimate": 10,
    },
    "cache_hit": {
        "label": "Using a saved transcript from cache.",
        "progress": 95,
        "step": 4,
        "estimate": 5,
    },
    "downloading": {
        "label": "Downloading audio from YouTube.",
        "progress": 15,
        "step": 2,
        "estimate": 60,
    },
    "transcribing": {
        "label": "Whisper is transcribing your lecture.",
        "progress": 45,
        "step": 3,
        "estimate": 360,
    },
    "formatting": {
        "label": "Now formatting your transcript.",
        "progress": 78,
        "step": 4,
        "estimate": 240,
    },
    "saving": {
        "label": "Saving the transcript and updating cache.",
        "progress": 95,
        "step": 5,
        "estimate": 15,
    },
    "completed": {
        "label": "Transcript is ready.",
        "progress": 100,
        "step": 5,
        "estimate": 0,
    },
    "failed": {
        "label": "The job failed.",
        "progress": 100,
        "step": 0,
        "estimate": 0,
    },
}


class TranscriptionRequest(BaseModel):
    """Validate and document the JSON body accepted by `POST /jobs`.

    Purpose:
        Define the public API contract for starting a transcription job.
    Args:
        youtube_url: YouTube lecture URL.
        clean: Enables the better-format mode with Ollama fallback.
        skip_audio_cache: Forces a new audio download.
        use_cached_outputs: Allows transcript cache reuse.
        language: Whisper language code.
    Returns:
        A validated Pydantic model instance.
    Workflow:
        FastAPI constructs this model from request JSON and returns validation errors
        before the route executes when fields are invalid.
    Connects to:
        Consumed by `create_transcription_job`, then converted for `submit_job`.
    """
    youtube_url: str = Field(
        ...,
        description="YouTube video URL to download and transcribe.",
        example="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    clean: bool = Field(
        default=True,
        description="Use better formatting mode. Groq is tried first; Ollama is used as fallback only when this is true.",
    )
    skip_audio_cache: bool = Field(
        default=False,
        description="Force yt-dlp to download audio again instead of reusing cached MP3 files.",
    )
    use_cached_outputs: bool = Field(
        default=True,
        description="Reuse existing raw/cleaned transcript files when they already exist.",
    )
    language: str = Field(
        default=DEFAULT_LANGUAGE,
        description="Whisper language code. The current project default is Arabic.",
    )


def clean_job_result(job: dict) -> None:
    """Remove obsolete folder inventory data from an individual job result.

    Purpose:
        Keep each persisted and returned job compact while the inventory remains global.
    Args:
        job: Mutable job dictionary that may contain a result dictionary.
    Returns:
        None; the supplied dictionary is modified in place.
    Workflow:
        Reads `job["result"]` and removes the legacy `llama_folder_filenames` field.
    Connects to:
        Called by `load_jobs` and `save_jobs_unlocked`.
    """
    result = job.get("result")
    if isinstance(result, dict):
        result.pop("llama_folder_filenames", None)


def load_jobs() -> dict[str, dict]:
    """Load persisted jobs and normalize state after an API restart.

    Purpose:
        Preserve completed job history and make interrupted jobs explicit.
    Args:
        None.
    Returns:
        A dictionary keyed by job ID, or an empty dictionary for missing/invalid data.
    Workflow:
        Reads `jobs.json`, validates its shape, cleans legacy result fields, and marks
        queued or running jobs as failed because their worker no longer exists.
    Connects to:
        Calls `clean_job_result`; its output initializes the global `jobs` registry.
    """
    if not JOBS_FILE.exists():
        return {}

    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    loaded_jobs = data.get("jobs", {})
    if not isinstance(loaded_jobs, dict):
        return {}

    for job in loaded_jobs.values():
        clean_job_result(job)
        if job.get("status") in {"queued", "running"}:
            job["status"] = "failed"
            job["error"] = "API server restarted before this job finished."
            job["finished_at"] = job.get("finished_at") or time()

    return loaded_jobs


def save_jobs_unlocked() -> None:
    """Persist the in-memory job registry while the caller owns `jobs_lock`.

    Purpose:
        Save job history atomically after every meaningful state change.
    Args:
        None; reads the global `jobs` dictionary.
    Returns:
        None.
    Workflow:
        Cleans legacy result fields, writes jobs plus the cleaned-output filename
        inventory to a temporary JSON file, then replaces `jobs.json`.
    Connects to:
        Calls `clean_job_result` and `list_ollama_output_filenames`; called by
        `update_job` and `submit_job`.
    """
    for job in jobs.values():
        clean_job_result(job)

    tmp = JOBS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "jobs": jobs,
                "llama_folder_filenames": list_ollama_output_filenames(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(JOBS_FILE)


def update_job(job_id: str, **changes) -> None:
    """Apply changes to one job and persist the registry safely.

    Purpose:
        Centralize synchronized job mutation for worker and request threads.
    Args:
        job_id: Identifier of the existing job to update.
        **changes: Job fields and values to merge.
    Returns:
        None.
    Workflow:
        Acquires `jobs_lock`, updates the selected dictionary, and saves all jobs.
    Connects to:
        Calls `save_jobs_unlocked`; used by progress and background-job functions.
    """
    with jobs_lock:
        jobs[job_id].update(changes)
        save_jobs_unlocked()


def estimate_stage_seconds(stage: str, clean: bool, details: dict | None = None) -> int:
    """Estimate remaining seconds for a pipeline stage.

    Purpose:
        Give waiting users a practical time estimate in the frontend.
    Args:
        stage: Current pipeline stage identifier.
        clean: Whether the job uses better-format mode.
        details: Optional pipeline metadata, such as video duration.
    Returns:
        Estimated duration in whole seconds.
    Workflow:
        Calculates a recent average from up to eight matching completed jobs, applies a
        stage ratio, and falls back to configured defaults when history is unavailable.
    Connects to:
        Reads the global `jobs` registry; called by `build_progress_update`.
    """
    details = details or {}
    explicit_estimate = details.get("estimated_stage_seconds")
    if explicit_estimate:
        return max(0, int(explicit_estimate))

    video_duration = details.get("video_duration_seconds")
    if stage == "transcribing" and video_duration:
        try:
            return max(30, int(round(float(video_duration) / 3)))
        except (TypeError, ValueError):
            pass

    completed_durations = []
    for job in jobs.values():
        if job.get("status") != "completed":
            continue
        if bool(job.get("request", {}).get("clean", True)) != clean:
            continue
        started_at = job.get("started_at")
        finished_at = job.get("finished_at")
        if started_at and finished_at and finished_at > started_at:
            completed_durations.append(finished_at - started_at)

    if completed_durations:
        average_total = sum(completed_durations[-8:]) / min(len(completed_durations), 8)
        if stage == "transcribing":
            return max(90, int(average_total * (0.65 if clean else 0.8)))
        if stage == "formatting":
            return max(60, int(average_total * 0.3))
        if stage == "downloading":
            return max(20, int(average_total * 0.1))

    return int(STAGE_DEFAULTS.get(stage, {}).get("estimate", 60))


def build_progress_update(stage: str, request_data: dict, details: dict | None = None) -> dict:
    """Build the normalized progress fields stored on a job.

    Purpose:
        Translate low-level pipeline events into frontend-ready labels and percentages.
    Args:
        stage: Pipeline stage identifier.
        request_data: Original job request, including mode selection.
        details: Optional stage label and chunk-progress overrides.
    Returns:
        A dictionary of progress, step, timing, and chunk fields.
    Workflow:
        Starts from `STAGE_DEFAULTS`, adjusts formatting progress per chunk, handles the
        shorter fast-mode step count, and adds timestamps and estimates.
    Connects to:
        Calls `estimate_stage_seconds`; used during job creation, progress, completion,
        and failure updates.
    """
    details = details or {}
    clean = bool(request_data.get("clean", True))
    defaults = STAGE_DEFAULTS.get(stage, STAGE_DEFAULTS["queued"])
    progress = int(defaults["progress"])
    step = int(defaults["step"])
    total_steps = TOTAL_STEPS[clean]

    if stage == "formatting":
        chunk_index = details.get("chunk_index")
        chunk_total = details.get("chunk_total")
        if chunk_total:
            chunk_progress = max(0, min(1, float(chunk_index or 0) / float(chunk_total)))
            progress = 72 + int(chunk_progress * 22)

    if not clean and stage in {"saving", "completed"}:
        step = total_steps

    update = {
        "stage": stage,
        "stage_label": details.get("detail") or defaults["label"],
        "progress_percent": progress,
        "current_step": min(step, total_steps),
        "total_steps": total_steps,
        "stage_started_at": time(),
        "estimated_stage_seconds": estimate_stage_seconds(stage, clean, details),
        "chunk_index": details.get("chunk_index"),
        "chunk_total": details.get("chunk_total"),
        "updated_at": time(),
    }
    if details.get("video_duration_seconds") is not None:
        update["video_duration_seconds"] = details.get("video_duration_seconds")
    if stage == "transcribing" and details.get("estimated_stage_seconds") is not None:
        update["whisper_estimate_seconds"] = details.get("estimated_stage_seconds")
    return update


def update_job_progress(job_id: str, request_data: dict, stage: str, details: dict | None = None) -> None:
    """Persist one progress event from the transcription pipeline.

    Purpose:
        Bridge pipeline callbacks to the API's job-status representation.
    Args:
        job_id: Job receiving the progress update.
        request_data: Original request used to determine mode and step count.
        stage: Current pipeline stage.
        details: Optional label or chunk information.
    Returns:
        None.
    Workflow:
        Builds normalized progress fields, marks the job running, and saves the update.
    Connects to:
        Calls `build_progress_update` and `update_job`; passed into
        `process_youtube_url` by `run_transcription_job`.
    """
    update_job(job_id, status="running", **build_progress_update(stage, request_data, details))


def get_job_or_404(job_id: str) -> dict:
    """Return a thread-safe snapshot of a job or raise an HTTP 404 error.

    Purpose:
        Share consistent job lookup behavior across public API routes.
    Args:
        job_id: Requested job identifier.
    Returns:
        A deep copy of the stored job dictionary.
    Raises:
        HTTPException: With status 404 when the job does not exist.
    Workflow:
        Locks the job registry, looks up the ID, and copies the result before returning.
    Connects to:
        Used by `get_job` and `get_transcript`.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return deepcopy(job)


def run_transcription_job(job_id: str, request_data: dict) -> None:
    """Execute one transcription job inside the background thread pool.

    Purpose:
        Keep long-running download, Whisper, and cleaning work outside HTTP request time.
    Args:
        job_id: Existing queued job identifier.
        request_data: Keyword arguments accepted by `process_youtube_url`.
    Returns:
        None; completion or failure is persisted in the job registry.
    Workflow:
        Marks the job running, starts the pipeline with a progress callback, converts
        exceptions into failed state, or saves the completed result.
    Connects to:
        Calls `process_youtube_url`, `update_job_progress`, `build_progress_update`, and
        `update_job`; submitted by `submit_job`.
    """
    update_job(
        job_id,
        status="running",
        started_at=time(),
        **build_progress_update("checking_cache", request_data),
    )

    try:
        result = process_youtube_url(
            **request_data,
            progress_callback=lambda stage, details: update_job_progress(
                job_id,
                request_data,
                stage,
                details,
            ),
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=time(),
            **build_progress_update("failed", request_data, {"detail": f"{type(exc).__name__}: {exc}"}),
        )
        return

    update_job(
        job_id,
        status="completed",
        result=result,
        finished_at=time(),
        **build_progress_update("completed", request_data),
    )


def submit_job(request_data: dict) -> dict:
    """Validate, persist, and queue a new transcription request.

    Purpose:
        Implement the shared job-submission logic behind the public POST route.
    Args:
        request_data: Validated request fields represented as a dictionary.
    Returns:
        Job ID, initial status, and polling/transcript endpoint paths.
    Raises:
        HTTPException: With status 400 when the YouTube URL is invalid.
    Workflow:
        Validates the URL, creates a UUID and queued job record, saves it under lock,
        and submits `run_transcription_job` to the single-worker executor.
    Connects to:
        Calls URL validation, progress building, persistence, and background execution;
        called by `create_transcription_job`.
    """
    is_valid, error = validate_youtube_url(request_data["youtube_url"])
    if not is_valid:
        raise HTTPException(status_code=400, detail=error)

    job_id = str(uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            **build_progress_update("queued", request_data),
            "submitted_at": time(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "request": deepcopy(request_data),
            "result": None,
        }
        save_jobs_unlocked()

    executor.submit(run_transcription_job, job_id, request_data)

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
        "transcript_url": f"/jobs/{job_id}/transcript",
    }


@app.get("/", response_class=FileResponse)
def root() -> FileResponse:
    """Serve the end-user job submission page at the public site root.

    Purpose:
        Make the main domain open the frontend instead of API metadata.
    Args:
        None.
    Returns:
        File response containing `front/index.html`.
    Workflow:
        Resolves the frontend file and lets FastAPI stream it.
    Connects to:
        Uses static assets mounted under `/front`; mirrors `frontend`.
    """
    return FileResponse(Path("front") / "index.html")


@app.get("/app", response_class=FileResponse)
def frontend() -> FileResponse:
    """Serve the job submission frontend on the compatibility `/app` route.

    Purpose:
        Preserve existing links while the site root is now the primary frontend URL.
    Args:
        None.
    Returns:
        File response containing `front/index.html`.
    Workflow:
        Resolves and returns the same HTML page as `root`.
    Connects to:
        Uses assets under `/front`; retained for compatibility with older navigation.
    """
    return FileResponse(Path("front") / "index.html")


@app.get("/app/jobs", response_class=FileResponse)
def frontend_jobs() -> FileResponse:
    """Serve the previous-jobs frontend page.

    Purpose:
        Let end users browse, refresh, and open persisted jobs.
    Args:
        None.
    Returns:
        File response containing `front/jobs.html`.
    Workflow:
        Resolves the jobs page and lets FastAPI serve it.
    Connects to:
        The page calls `list_jobs`, `get_job`, and transcript endpoints through JavaScript.
    """
    return FileResponse(Path("front") / "jobs.html")


@app.get("/health")
def health() -> dict:
    """Report whether the API process is running.

    Purpose:
        Provide a lightweight endpoint for Cloudflare, monitoring, and manual checks.
    Args:
        None.
    Returns:
        `{"status": "ok"}` while the process can handle requests.
    Workflow:
        Returns a constant response without accessing models, disk state, or the network.
    Connects to:
        Independent of the transcription pipeline.
    """
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
def create_transcription_job(request: TranscriptionRequest) -> dict:
    """Accept a transcription request and queue it for background processing.

    Purpose:
        Expose asynchronous job creation to the frontend and API clients.
    Args:
        request: FastAPI-validated `TranscriptionRequest` body.
    Returns:
        Initial queued-job metadata and URLs.
    Workflow:
        Converts the Pydantic model to a dictionary and delegates job creation.
    Connects to:
        Calls `submit_job`; polled later through `get_job`.
    """
    return submit_job(request.model_dump())


@app.get("/jobs")
def list_jobs() -> dict:
    """Return snapshots of all persisted jobs.

    Purpose:
        Populate the previous-jobs frontend and support API history queries.
    Args:
        None.
    Returns:
        A dictionary containing a list of deep-copied job records.
    Workflow:
        Locks the registry while copying values so callers cannot mutate shared state.
    Connects to:
        Reads jobs loaded by `load_jobs` and updated by background workers.
    """
    with jobs_lock:
        return {"jobs": deepcopy(list(jobs.values()))}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    """Return the current state of one transcription job.

    Purpose:
        Support frontend polling for status, progress, errors, and final result metadata.
    Args:
        job_id: UUID returned by job submission.
    Returns:
        A snapshot of the matching job.
    Raises:
        HTTPException: With status 404 when the job is unknown.
    Workflow:
        Delegates synchronized lookup and copying to `get_job_or_404`.
    Connects to:
        Calls `get_job_or_404`; polled by the submission and jobs frontends.
    """
    return get_job_or_404(job_id)


@app.get("/jobs/{job_id}/transcript")
def get_transcript(job_id: str, kind: str = "cleaned") -> PlainTextResponse:
    """Return the raw or cleaned transcript text for a completed job.

    Purpose:
        Deliver transcript content directly to the frontend or API clients.
    Args:
        job_id: UUID of the completed transcription job.
        kind: `cleaned` for the model output or `raw` for Faster-Whisper text.
    Returns:
        UTF-8 plain-text response containing the selected transcript.
    Raises:
        HTTPException: For an invalid kind, unknown job, unfinished job, missing result
            path, or missing transcript file.
    Workflow:
        Validates selection and job state, chooses the result path, verifies the file,
        and reads it with tolerant UTF-8 decoding.
    Connects to:
        Calls `get_job_or_404`; reads paths produced by `process_youtube_url`.
    """
    if kind not in {"cleaned", "raw"}:
        raise HTTPException(status_code=400, detail="kind must be 'cleaned' or 'raw'.")

    job = get_job_or_404(job_id)
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}.")

    result = job.get("result") or {}
    path_key = "cleaned_transcript_path" if kind == "cleaned" else "raw_transcript_path"
    transcript_path = result.get(path_key)
    if not transcript_path:
        raise HTTPException(status_code=404, detail=f"No {kind} transcript path for this job.")

    path = Path(transcript_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Transcript file not found: {path}")

    return PlainTextResponse(path.read_text(encoding="utf-8", errors="ignore"))


jobs.update(load_jobs())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
