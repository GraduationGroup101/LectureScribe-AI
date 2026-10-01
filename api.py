from concurrent.futures import ThreadPoolExecutor
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
import hmac
import json
from ipaddress import ip_address
import logging
from math import ceil
import os
from pathlib import Path
import re
import sys
from threading import Lock, RLock, Thread
from time import monotonic, sleep, time
from urllib.parse import urlsplit
from uuid import uuid4

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import requests

from MainCode_FasterWhisper import process_youtube_url

try:
    from MainCode_FasterWhisper import FORMAT_VERSION
except ImportError:  # Older pipeline builds do not export it; this is the contract value.
    FORMAT_VERSION = "source-language-v2"

from url_to_mp3 import extract_youtube_video_id, validate_youtube_url


logger = logging.getLogger("lecturescribe.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Resume jobs that were still waiting in the queue when the server stopped."""
    resume_queued_jobs()
    yield


app = FastAPI(
    title="LectureScribe-AI API",
    description="Transcribe lectures with OpenRouter Whisper and local fallback, then format with OpenRouter or Ollama.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/front", StaticFiles(directory="front"), name="front")


@app.middleware("http")
async def prevent_job_response_caching(request: Request, call_next):
    """Keep job responses out of HTTP caches."""
    response = await call_next(request)
    if request.url.path.startswith("/jobs"):
        response.headers["Cache-Control"] = "no-store"
    return response

executor = ThreadPoolExecutor(max_workers=1)
JOBS_FILE = Path("jobs.json")
jobs: dict[str, dict] = {}
# Reentrant because progress estimates read the registry while an update holds the lock.
jobs_lock = RLock()
def _limit_from_env(name: str, default: int) -> int:
    """Read a positive integer limit from the environment, keeping the default otherwise."""
    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def _env_flag(name: str) -> bool:
    """True when an environment variable is set to a truthy word such as `true` or `1`."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Callers that omit `language` (older gateways and scripts) keep the historical Arabic default.
DEFAULT_LANGUAGE = "ar"
LANGUAGE_PATTERN = re.compile(r"^(?:auto|[a-z]{2})$")
CONTENT_LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{1,8})?$")
# Keys `process_youtube_url` accepts; anything else in a request (such as callback_url) stays in the API.
PIPELINE_KEYS = ("youtube_url", "clean", "skip_audio_cache", "use_cached_outputs", "language")
# Lecture facts exposed at the top level of every job, filled at submit and from the pipeline.
CONTRACT_FIELDS = (
    "title",
    "video_id",
    "language",
    "mode",
    "detected_language",
    "format_version",
    "video_duration_seconds",
)
ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed"}

JOB_RATE_WINDOW_SECONDS = 3600
JOB_RATE_PER_IP = _limit_from_env("JOB_RATE_PER_IP", 3)
JOB_RATE_PER_USER = _limit_from_env("JOB_RATE_PER_USER", 6)
JOB_RATE_GLOBAL = _limit_from_env("JOB_RATE_GLOBAL", 60)
JOB_MAX_ACTIVE = _limit_from_env("JOB_MAX_ACTIVE", 10)
JOB_HISTORY_LIMIT = _limit_from_env("JOB_HISTORY_LIMIT", 500)
# Answers from a saved transcript cost no work but each adds a job record, so they
# have their own hourly budget, kept well under JOB_HISTORY_LIMIT: reuses alone can
# never push unfetched results out of the history. Past it, a request is queued
# like any other and counts against the normal limits.
JOB_REUSE_PER_CLIENT = _limit_from_env("JOB_REUSE_PER_CLIENT", 20)
JOB_REUSE_GLOBAL = _limit_from_env("JOB_REUSE_GLOBAL", 200)
# Progress events arrive several times per chunk; rewriting jobs.json for each one is wasted I/O.
JOBS_SAVE_INTERVAL_SECONDS = 2.0
CALLBACK_TIMEOUT_SECONDS = 30
# Wait before each delivery attempt. The first wait also lets a gateway store the job it
# just created; the later ones ride out a gateway outage of about a quarter of an hour.
CALLBACK_RETRY_DELAYS = (5, 30, 120, 300, 600)
MAX_CALLBACKS_PER_JOB = 20
KEEPALIVE_DEFAULT_INTERVAL_SECONDS = 240
# After the last job finishes, keep the instance awake this long so a gateway can still
# fetch the result. Render's free plan wipes the disk when it stops the instance.
KEEPALIVE_DEFAULT_GRACE_SECONDS = 900
GATEWAY_KEY_HEADER = "X-Gateway-Key"
GATEWAY_USER_HEADER = "X-Gateway-User"
GATEWAY_USER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
job_submissions: deque[float] = deque()
# Recent submissions per client key: "ip:<address>" for public callers and
# "user:<account>" for students identified by a trusted gateway.
job_submissions_by_client: dict[str, deque[float]] = {}
# Recent answers from saved transcripts, overall and per client key (see JOB_REUSE_*).
job_reuses: deque[float] = deque()
job_reuses_by_client: dict[str, deque[float]] = {}
# Finish notifications per job: (url, fingerprint of the gateway key that asked for it).
# Held in memory only, so no gateway secret ever reaches jobs.json.
job_callbacks: dict[str, list[tuple[str, str]]] = {}
# Finish notices still being delivered, per job (guarded by jobs_lock). While a job
# has one, the keep-alive keeps running and the history never forgets the job.
callback_deliveries: dict[str, int] = {}
last_jobs_save = 0.0
# When the last job reached a terminal state (guarded by jobs_lock).
last_finished_at = 0.0
keepalive_lock = Lock()
keepalive_running = False


def gateway_keys() -> set[str]:
    """Shared secrets of trusted gateways, from the comma-separated GATEWAY_KEYS variable."""
    return {key.strip() for key in os.environ.get("GATEWAY_KEYS", "").split(",") if key.strip()}


def matching_gateway_key(request: Request) -> str | None:
    """Return the configured gateway key the request presents, or None."""
    presented = request.headers.get(GATEWAY_KEY_HEADER, "")
    if not presented:
        return None
    # Compare bytes: compare_digest rejects non-ASCII str, which a caller could send.
    presented_bytes = presented.encode("utf-8", "surrogateescape")
    for key in gateway_keys():
        if hmac.compare_digest(presented_bytes, key.encode("utf-8")):
            return key
    return None


def is_trusted_gateway(request: Request) -> bool:
    """True when the request carries a configured gateway key."""
    return matching_gateway_key(request) is not None


def key_fingerprint(key: str) -> str:
    """Stable, non-reversible reference to a gateway key."""
    return sha256(key.encode("utf-8")).hexdigest()


def gateway_key_for_fingerprint(fingerprint: str) -> str | None:
    """Find the configured gateway key with this fingerprint, if it is still configured."""
    return next((key for key in gateway_keys() if hmac.compare_digest(key_fingerprint(key), fingerprint)), None)


def client_key(request: Request) -> tuple[str, int]:
    """Identify who is submitting and how many jobs an hour they may start.

    A gateway such as EduFusion submits for many students from one address. When
    it presents a valid key and names the account in X-Gateway-User, the limit is
    applied to that student instead of the shared address. A gateway without an
    account name shares one "gateway" bucket sized for users; the header is
    ignored (and never trusted) without a valid key.
    """
    if is_trusted_gateway(request):
        user = request.headers.get(GATEWAY_USER_HEADER, "").strip()
        if GATEWAY_USER_PATTERN.fullmatch(user):
            return f"user:{user}", JOB_RATE_PER_USER
        return f"gateway:{client_ip(request)}", JOB_RATE_PER_USER
    return f"ip:{client_ip(request)}", JOB_RATE_PER_IP


def client_ip(request: Request) -> str:
    """Use Render's Cloudflare client IP; ignore caller-supplied proxy headers locally."""
    if os.environ.get("RENDER") == "true":
        forwarded = request.headers.get("cf-connecting-ip", "")
        try:
            return str(ip_address(forwarded))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def accepted_callback(callback_url: str | None, gateway_key: str | None) -> tuple[str, str] | None:
    """Return `(url, key fingerprint)` when a finish notification may be sent, else None.

    Only a caller holding a gateway key may ask for one, and only to an https URL
    (plain http is allowed when CALLBACK_ALLOW_HTTP=true, for local tests). Anything
    else is ignored rather than rejected so older gateways keep working.
    """
    if not callback_url or not gateway_key:
        return None
    url = callback_url.strip()
    if len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    allowed = {"https", "http"} if _env_flag("CALLBACK_ALLOW_HTTP") else {"https"}
    if parts.scheme.lower() not in allowed or not parts.hostname:
        return None
    return url, key_fingerprint(gateway_key)


def prune_window_unlocked(overall: deque[float], by_client: dict[str, deque[float]], now: float) -> None:
    """Drop timestamps older than the rate window from an overall and a per-client log."""
    cutoff = now - JOB_RATE_WINDOW_SECONDS
    while overall and overall[0] <= cutoff:
        overall.popleft()
    for client, timestamps in list(by_client.items()):
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if not timestamps:
            del by_client[client]


def reuse_allowed_unlocked(key: str, now: float) -> bool:
    """Whether this caller may be answered from a saved transcript now; records the use.

    The overall budget never exceeds half of JOB_HISTORY_LIMIT, so reuse records can
    not flush the history within an hour even when the limits are configured low.
    """
    prune_window_unlocked(job_reuses, job_reuses_by_client, now)
    overall_cap = min(JOB_REUSE_GLOBAL, max(1, JOB_HISTORY_LIMIT // 2))
    if len(job_reuses) >= overall_cap or len(job_reuses_by_client.get(key, ())) >= JOB_REUSE_PER_CLIENT:
        return False
    job_reuses.append(now)
    job_reuses_by_client.setdefault(key, deque()).append(now)
    return True


def check_job_admission_unlocked(key: str, limit: int, now: float) -> None:
    """Reject excess submissions while the caller holds jobs_lock."""
    prune_window_unlocked(job_submissions, job_submissions_by_client, now)

    if sum(job.get("status") in ACTIVE_STATUSES for job in jobs.values()) >= JOB_MAX_ACTIVE:
        raise HTTPException(
            status_code=429,
            detail="The job queue is full. Try again after a job finishes.",
            headers={"Retry-After": "60"},
        )

    for timestamps, cap, message in (
        (job_submissions, JOB_RATE_GLOBAL, "Server job limit reached. Try again later."),
        (job_submissions_by_client.get(key, ()), limit, "You have reached your hourly lecture limit. Try again later."),
    ):
        if len(timestamps) >= cap:
            retry_after = max(1, ceil(timestamps[0] + JOB_RATE_WINDOW_SECONDS - now))
            raise HTTPException(
                status_code=429,
                detail=message,
                headers={"Retry-After": str(retry_after)},
            )

TOTAL_STEPS = {
    True: 5,
    False: 4,
}
# Stage defaults match the base of each stage's per-chunk range, so the first
# chunk event never moves the bar backwards.
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
        "progress": 35,
        "step": 3,
        "estimate": 360,
    },
    "formatting": {
        "label": "Now formatting your transcript.",
        "progress": 72,
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
        clean: True for AI formatting ("formatted"); False for the deterministic
            Whisper-only layout ("fast").
        skip_audio_cache: Forces a new audio download.
        use_cached_outputs: Allows transcript cache reuse.
        language: `auto` to detect the spoken language, or a two-letter ISO 639-1 code.
        callback_url: Trusted gateways only; notified when the job finishes.
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
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    clean: bool = Field(
        default=True,
        description="True: AI formatting kept in the lecture's language. False: Whisper text with automatic paragraphs only.",
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
        description="'auto' to detect the spoken language, or a two-letter ISO 639-1 code such as 'ar' or 'en'.",
    )
    callback_url: str | None = Field(
        default=None,
        description="Trusted gateways only: https URL that receives a signed notice when the job finishes.",
    )

    @field_validator("language", mode="before")
    @classmethod
    def normalise_language(cls, value: object) -> str:
        """Accept `auto` or a two-letter code in any case; reject everything else with 422."""
        if isinstance(value, str):
            value = value.strip().lower()
            if LANGUAGE_PATTERN.fullmatch(value):
                return value
        raise ValueError("language must be 'auto' or a two-letter ISO 639-1 code such as 'ar' or 'en'.")


def pipeline_request(request_data: dict) -> dict:
    """Keep only the keys `process_youtube_url` understands."""
    return {key: request_data[key] for key in PIPELINE_KEYS if key in request_data}


def mode_of(request_data: dict) -> str:
    """`formatted` when the request asks for AI formatting, else `fast`."""
    return "formatted" if bool(request_data.get("clean", True)) else "fast"


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
        running jobs as failed because their worker no longer exists. Queued jobs
        never started, so they stay queued and `resume_queued_jobs` runs them again.
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
        if not isinstance(job, dict):
            continue
        clean_job_result(job)
        if job.get("status") == "running":
            job["status"] = "failed"
            job["stage"] = "failed"
            job["error"] = "API server restarted before this job finished."
            job["finished_at"] = job.get("finished_at") or time()

    return {job_id: job for job_id, job in loaded_jobs.items() if isinstance(job, dict)}


def trim_job_history_unlocked() -> None:
    """Forget the oldest finished jobs beyond JOB_HISTORY_LIMIT.

    Queued and running jobs stay, and so does a finished job whose finish notice is
    still being delivered: the gateway is about to fetch it.
    """
    finished = [(job_id, job) for job_id, job in jobs.items() if job.get("status") in TERMINAL_STATUSES]
    excess = len(finished) - JOB_HISTORY_LIMIT
    if excess <= 0:
        return
    evictable = [(job_id, job) for job_id, job in finished if job_id not in callback_deliveries]
    evictable.sort(key=lambda item: item[1].get("finished_at") or item[1].get("submitted_at") or 0)
    for job_id, _job in evictable[:excess]:
        jobs.pop(job_id, None)
        job_callbacks.pop(job_id, None)


def save_jobs_unlocked() -> None:
    """Persist the in-memory job registry while the caller owns `jobs_lock`.

    Purpose:
        Save job history atomically after every meaningful state change.
    Args:
        None; reads the global `jobs` dictionary.
    Returns:
        None.
    Workflow:
        Cleans legacy result fields, writes the jobs to a temporary JSON file, then
        replaces `jobs.json`. Values JSON cannot encode are stored as text so one odd
        result field can never block every later save.
    Connects to:
        Calls `clean_job_result`; called by `persist_jobs_unlocked` and `submit_job`.
    """
    for job in jobs.values():
        clean_job_result(job)

    tmp = JOBS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(JOBS_FILE)


def persist_jobs_unlocked(force: bool = False) -> None:
    """Save the registry now when forced, otherwise at most once per JOBS_SAVE_INTERVAL_SECONDS."""
    global last_jobs_save
    now = time()
    if not force and now - last_jobs_save < JOBS_SAVE_INTERVAL_SECONDS:
        return
    save_jobs_unlocked()
    last_jobs_save = now


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
        The registry is copied under the lock first: request threads add jobs while the
        worker estimates, and iterating the live dictionary would crash the job.
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

    if stage not in {"transcribing", "formatting", "downloading"}:
        return int(STAGE_DEFAULTS.get(stage, {}).get("estimate", 60))

    with jobs_lock:
        snapshot = [
            (job.get("request") or {}, job.get("started_at"), job.get("finished_at"))
            for job in jobs.values()
            if job.get("status") == "completed"
        ]

    completed_durations = []
    for request_data, started_at, finished_at in snapshot:
        if bool(request_data.get("clean", True)) != clean:
            continue
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
        details: Optional stage label, chunk progress and lecture facts (title,
            duration, detected language) reported by the pipeline.
    Returns:
        A dictionary of progress, step, timing, chunk and lecture fields.
    Workflow:
        Starts from `STAGE_DEFAULTS`, adjusts progress per chunk, handles the
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

    if stage == "transcribing" and details.get("chunk_total"):
        chunk_progress = max(0, min(1, float((details.get("chunk_index") or 1) - 1) / float(details["chunk_total"])))
        progress = 35 + int(chunk_progress * 35)

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
    # The pipeline reports the title right after download, so it shows while the job runs.
    for field in ("title", "detected_language"):
        if details.get(field):
            update[field] = details[field]
    for field in ("transcription_provider", "transcription_error"):
        if field in details:
            update[field] = details[field]
    if stage == "transcribing" and details.get("estimated_stage_seconds") is not None:
        update["whisper_estimate_seconds"] = details.get("estimated_stage_seconds")
    return update


def apply_progress_unlocked(job: dict, stage: str, update: dict, keep_start: bool = True) -> bool:
    """Merge a progress update into a running job; True when the stage or status changed.

    The bar never moves backwards within a job. Clients count down
    `stage_started_at + estimated_stage_seconds - now`, so:
    a whole-stage estimate keeps the stage's start time across its chunk events
    (the countdown does not restart), while an event that carries the pipeline's
    own "seconds left from now" estimate (`keep_start=False`) starts the countdown
    from this event, or the elapsed time would be subtracted twice.
    """
    try:
        previous = float(job.get("progress_percent") or 0)
    except (TypeError, ValueError):
        previous = 0
    update["progress_percent"] = int(max(previous, update["progress_percent"]))
    same_stage = job.get("stage") == stage
    if same_stage and keep_start and job.get("stage_started_at"):
        update["stage_started_at"] = job["stage_started_at"]
    changed = job.get("status") != "running" or not same_stage
    job.update(status="running", **update)
    return changed


def update_job_progress(job_id: str, request_data: dict, stage: str, details: dict | None = None) -> None:
    """Persist one progress event from the transcription pipeline.

    Purpose:
        Bridge pipeline callbacks to the API's job-status representation.
    Args:
        job_id: Job receiving the progress update.
        request_data: Original request used to determine mode and step count.
        stage: Current pipeline stage.
        details: Optional label, chunk information or lecture facts.
    Returns:
        None.
    Workflow:
        Builds normalized progress fields under the lock, marks the job running, and
        saves the registry on stage changes or at most every two seconds otherwise. A
        failed write is logged, never raised: progress is cosmetic and must not abort
        the lecture. An explicit `estimated_stage_seconds` from the pipeline is the
        time left from now, so it restarts the stage countdown.
    Connects to:
        Calls `build_progress_update` and `persist_jobs_unlocked`; passed into
        `process_youtube_url` by `run_transcription_job`.
    """
    keep_start = not (details or {}).get("estimated_stage_seconds")
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job.get("status") in TERMINAL_STATUSES:
            return
        changed = apply_progress_unlocked(
            job, stage, build_progress_update(stage, request_data, details), keep_start=keep_start
        )
        try:
            persist_jobs_unlocked(force=changed)
        except Exception as exc:
            logger.warning("Could not save progress for job %s: %s", job_id, exc)


def job_snapshot(job: dict) -> dict:
    """Deep copy of a job for a response, with every contract field present (None when unknown)."""
    snapshot = deepcopy(job)
    for field in CONTRACT_FIELDS:
        snapshot.setdefault(field, None)
    return snapshot


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


PIPELINE_ERROR_HINTS = (
    (
        ("read-only file system",),
        "The server could not write its download session files. "
        "Check the writable temporary directory and retry.",
    ),
    (
        ("http error 403", "unable to download video data", "unable to extract", "nsig extraction failed"),
        "YouTube refused the download. This is almost always a stale extractor: "
        "run `pip install -U yt-dlp` and restart the server.",
    ),
    (
        ("sign in to confirm", "confirm you're not a bot"),
        "YouTube asked this server to prove it is not a bot. Pass browser cookies to yt-dlp, "
        "or try again from a different network.",
    ),
    (
        ("private video", "video unavailable", "removed by the uploader", "not available in your country"),
        "This video cannot be reached: it is private, deleted, or region-locked. "
        "Try a different upload of the same lecture.",
    ),
    (
        ("members-only", "join this channel"),
        "This video is members-only, so its audio cannot be downloaded.",
    ),
    (
        ("age-restricted", "age restricted", "inappropriate for some users"),
        "This video is age-restricted, which blocks anonymous download.",
    ),
    (
        ("ffmpeg", "ffprobe"),
        "FFmpeg is missing or not on PATH, so the audio could not be converted to MP3.",
    ),
    (
        ("cuda", "cudnn", "out of memory"),
        "The GPU rejected the transcription. Free VRAM, or run Whisper on the CPU.",
    ),
)


def describe_pipeline_error(exc: Exception) -> str:
    """Turn a pipeline exception into a message that names the recovery.

    Purpose:
        Give the browser a failure the reader can act on instead of a raw traceback string.
    Args:
        exc: The exception raised by `process_youtube_url`.
    Returns:
        A one-or-two sentence message, always ending with the raw detail for debugging.
    Workflow:
        Matches the lowercased exception text against known failure signatures and
        prefixes the matching hint; falls back to the bare class and message.
    Connects to:
        Called by `run_transcription_job` when the pipeline raises.
    """
    raw = f"{type(exc).__name__}: {exc}".strip()
    haystack = raw.lower()
    for needles, hint in PIPELINE_ERROR_HINTS:
        if any(needle in haystack for needle in needles):
            return f"{hint} ({raw})"
    return raw


def sign_callback(key: str, timestamp: str, job_id: str, status: str) -> str:
    """HMAC-SHA256 signature of `<timestamp>.<job_id>.<status>`, as sent in X-LectureScribe-Signature."""
    digest = hmac.new(key.encode("utf-8"), f"{timestamp}.{job_id}.{status}".encode("utf-8"), sha256).hexdigest()
    return f"sha256={digest}"


def deliver_callback(url: str, fingerprint: str, job_id: str, status: str) -> bool:
    """POST one signed finish notice, retrying transient failures; True once delivered.

    Purpose:
        Tell a gateway that a job it submitted has finished, so it can store the
        transcript even when no browser is watching.
    Args:
        url: Callback URL the gateway supplied with the job.
        fingerprint: Fingerprint of the gateway key that authenticated the submission.
        job_id: Finished job.
        status: `completed` or `failed`.
    Returns:
        True when the gateway answered 2xx, otherwise False. Never raises.
    Workflow:
        Waits CALLBACK_RETRY_DELAYS[i] before attempt i, signs a fresh timestamp each
        time, and stops early on a permanent 4xx answer.
    Connects to:
        Started on a daemon thread by `dispatch_callbacks`.
    """
    payload = {"event": "job.finished", "job_id": job_id, "status": status}
    for attempt, delay in enumerate(CALLBACK_RETRY_DELAYS, start=1):
        try:
            if delay:
                sleep(delay)
            key = gateway_key_for_fingerprint(fingerprint)
            if key is None:
                logger.warning("Callback for job %s skipped: its gateway key is no longer configured.", job_id)
                return False
            timestamp = str(int(time()))
            response = requests.post(
                url,
                json=payload,
                headers={
                    "X-LectureScribe-Timestamp": timestamp,
                    "X-LectureScribe-Signature": sign_callback(key, timestamp, job_id, status),
                },
                timeout=CALLBACK_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except Exception as exc:
            logger.warning("Callback attempt %s for job %s failed: %s", attempt, job_id, exc)
            continue
        if 200 <= response.status_code < 300:
            return True
        logger.warning("Callback attempt %s for job %s got HTTP %s", attempt, job_id, response.status_code)
        if 400 <= response.status_code < 500 and response.status_code not in {408, 425, 429}:
            return False
    return False


def start_background(target, *args) -> None:
    """Run `target(*args)` on a daemon thread that never blocks shutdown."""
    Thread(target=target, args=args, daemon=True).start()


def release_callback_delivery(job_id: str) -> None:
    """Record that one finish notice of a job is no longer being delivered."""
    with jobs_lock:
        remaining = callback_deliveries.get(job_id, 0) - 1
        if remaining > 0:
            callback_deliveries[job_id] = remaining
        else:
            callback_deliveries.pop(job_id, None)


def deliver_tracked_callback(url: str, fingerprint: str, job_id: str, status: str) -> None:
    """Deliver one finish notice, then release the keep-alive and history hold it had."""
    try:
        deliver_callback(url, fingerprint, job_id, status)
    finally:
        release_callback_delivery(job_id)


def dispatch_callbacks(job_id: str, status: str | None, callbacks: list[tuple[str, str]]) -> None:
    """Send the finish notice to every registered callback without ever raising into the job.

    Each delivery is counted before its thread starts and the keep-alive is (re)started,
    so the instance stays awake, and the job stays listed, until every retry has run.
    """
    if not callbacks or status not in TERMINAL_STATUSES:
        return
    for url, fingerprint in callbacks:
        with jobs_lock:
            callback_deliveries[job_id] = callback_deliveries.get(job_id, 0) + 1
        try:
            start_background(deliver_tracked_callback, url, fingerprint, job_id, status)
        except Exception as exc:
            release_callback_delivery(job_id)
            logger.warning("Could not start the callback for job %s: %s", job_id, exc)
    ensure_keepalive()


def add_callback_unlocked(job_id: str, callback: tuple[str, str] | None) -> None:
    """Register a finish notice for a job once; the caller holds jobs_lock."""
    if callback is None:
        return
    registered = job_callbacks.setdefault(job_id, [])
    if callback not in registered and len(registered) < MAX_CALLBACKS_PER_JOB:
        registered.append(callback)


def finish_job(job_id: str, **changes) -> None:
    """Move a job to its terminal state, persist it, and send its finish notices.

    The state change and the removal of the callbacks happen under one lock, so a
    duplicate submission can never attach a callback to a job that already finished.
    Callbacks are dispatched even when writing jobs.json fails. The finish time starts
    the keep-alive's grace period.
    """
    global last_finished_at
    callbacks: list[tuple[str, str]] = []
    status = changes.get("status")
    try:
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                return
            job.update(changes)
            status = job.get("status")
            last_finished_at = time()
            callbacks = job_callbacks.pop(job_id, [])
            trim_job_history_unlocked()
            persist_jobs_unlocked(force=True)
    finally:
        dispatch_callbacks(job_id, status, callbacks)


def result_contract_fields(job: dict, result: dict) -> dict:
    """Top-level lecture facts for a completed job, preferring what the pipeline reported.

    `mode` becomes the mode of the transcript actually produced: when no model could
    format a `formatted` request, the pipeline returns its deterministic layout as
    `fast`, and reporting it as `formatted` would let it be reused as AI formatting.
    The request (`request.clean`, `result.requested_mode`) still shows what was asked.
    """
    fields = {
        "title": result.get("title") or job.get("title"),
        "video_id": result.get("video_id") or job.get("video_id"),
        "language": job.get("language") or result.get("language"),
        "mode": result.get("mode") or job.get("mode"),
        "detected_language": result.get("detected_language") or job.get("detected_language"),
        # Never stamped by the API: only output from the versioned pipeline carries a version.
        "format_version": result.get("format_version"),
        "video_duration_seconds": result.get("video_duration_seconds") or job.get("video_duration_seconds"),
    }
    for field in ("title", "video_id", "language", "mode", "detected_language", "format_version"):
        result.setdefault(field, fields[field])
    return fields


def complete_job(job_id: str, request_data: dict, result: dict | None) -> None:
    """Store a successful pipeline result and its lecture facts on the job."""
    result = dict(result or {})
    with jobs_lock:
        fields = result_contract_fields(jobs.get(job_id) or {}, result)
    finish_job(
        job_id,
        status="completed",
        result=result,
        finished_at=time(),
        **build_progress_update("completed", request_data),
        **fields,
    )


def fail_job(job_id: str, request_data: dict, message: str) -> None:
    """Mark a job failed, keeping the progress it had reached so the UI shows where it stopped."""
    with jobs_lock:
        last_progress = jobs.get(job_id) or {}
        reached = {
            "progress_percent": last_progress.get("progress_percent", 0),
            "current_step": last_progress.get("current_step", 0),
        }
    failed_progress = build_progress_update("failed", request_data, {"detail": message})
    failed_progress.update(reached)
    finish_job(job_id, status="failed", error=message, finished_at=time(), **failed_progress)


def abandon_job(job_id: str, request_data: dict, exc: BaseException) -> None:
    """Fail a job whose own bookkeeping raised, so it never stays queued or running.

    A stuck job would also hold a JOB_MAX_ACTIVE slot until the next restart.
    """
    global last_finished_at
    message = f"The server hit an internal error while running this job ({type(exc).__name__}: {exc})."
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job.get("status") in TERMINAL_STATUSES:
            return
    try:
        fail_job(job_id, request_data, message)
        return
    except Exception as second:
        logger.error("Could not record the failure of job %s: %s", job_id, second)
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None and job.get("status") not in TERMINAL_STATUSES:
            job.update(status="failed", stage="failed", error=message, finished_at=time())
            last_finished_at = time()
        callbacks = job_callbacks.pop(job_id, [])
    dispatch_callbacks(job_id, "failed", callbacks)


def run_transcription_job(job_id: str, request_data: dict) -> None:
    """Execute one transcription job inside the background thread pool.

    Purpose:
        Keep long-running download, Whisper, and cleaning work outside HTTP request time.
    Args:
        job_id: Existing queued job identifier.
        request_data: The job's request; only pipeline keys reach `process_youtube_url`.
    Returns:
        None; completion or failure is persisted in the job registry.
    Workflow:
        Marks the job running, starts the pipeline with a progress callback, converts
        pipeline exceptions into failed state, or saves the completed result. Any error
        in this function's own state updates still ends the job as failed.
    Connects to:
        Calls `process_youtube_url`, `update_job_progress`, `complete_job`,
        `fail_job` and `abandon_job`; submitted by `submit_job`.
    """
    try:
        with jobs_lock:
            job = jobs[job_id]
            apply_progress_unlocked(job, "checking_cache", build_progress_update("checking_cache", request_data))
            job["started_at"] = time()
            persist_jobs_unlocked(force=True)

        try:
            result = process_youtube_url(
                **pipeline_request(request_data),
                progress_callback=lambda stage, details: update_job_progress(
                    job_id,
                    request_data,
                    stage,
                    details,
                ),
            )
        except Exception as exc:
            fail_job(job_id, request_data, describe_pipeline_error(exc))
            return

        complete_job(job_id, request_data, result)
    except BaseException as exc:
        abandon_job(job_id, request_data, exc)
        if not isinstance(exc, Exception):
            raise


def find_active_job_unlocked(video_id: str, language: str, mode: str) -> dict | None:
    """A queued or running job for the same lecture, language and mode, if any."""
    for job in jobs.values():
        if (
            job.get("status") in ACTIVE_STATUSES
            and job.get("video_id") == video_id
            and job.get("language") == language
            and job.get("mode") == mode
        ):
            return job
    return None


def transcript_files_exist(result: dict) -> bool:
    """True when the result names at least one transcript and every named file exists."""
    paths = [result.get("raw_transcript_path"), result.get("cleaned_transcript_path")]
    present = [path for path in paths if path]
    return bool(present) and all(Path(path).is_file() for path in present)


def find_reusable_job_unlocked(video_id: str, language: str, mode: str) -> dict | None:
    """Newest completed job of the current pipeline version whose transcript files still exist."""
    candidates = [
        job
        for job in jobs.values()
        if job.get("status") == "completed"
        and job.get("video_id") == video_id
        and job.get("language") == language
        and job.get("mode") == mode
        and job.get("format_version") == FORMAT_VERSION
        and isinstance(job.get("result"), dict)
    ]
    candidates.sort(key=lambda job: job.get("finished_at") or 0, reverse=True)
    for job in candidates:
        result = job["result"]
        if result.get("mode", mode) != mode:
            continue
        if mode == "formatted" and not result.get("cleaned_transcript_path"):
            continue
        if transcript_files_exist(result):
            return job
    return None


def reuse_completed_job_unlocked(source: dict, job_id: str, request_data: dict, via_gateway: bool) -> dict:
    """Create a job that is already completed from an earlier job's saved transcript."""
    global last_finished_at
    now = time()
    last_finished_at = now
    result = deepcopy(source["result"])
    result.update(
        audio_path=None,
        used_cached_cleaned_transcript=bool(result.get("cleaned_transcript_path")),
        used_cached_raw_transcript=bool(result.get("raw_transcript_path")),
    )
    job = {
        "job_id": job_id,
        "status": "completed",
        **build_progress_update("completed", request_data, {"detail": "Reused a saved transcript."}),
        "submitted_at": now,
        "started_at": now,
        "finished_at": now,
        "error": None,
        "request": deepcopy(request_data),
        "result": result,
        **{field: source.get(field) for field in CONTRACT_FIELDS},
        "cached": True,
        "source_job_id": source.get("job_id"),
        "via_gateway": via_gateway,
    }
    jobs[job_id] = job
    return job


def job_response(job: dict, **extra) -> dict:
    """The POST /jobs answer for a job."""
    job_id = job["job_id"]
    return {
        "job_id": job_id,
        "status": job["status"],
        "status_url": f"/jobs/{job_id}",
        "transcript_url": f"/jobs/{job_id}/transcript",
        "submitted_at": job.get("submitted_at"),
        **extra,
    }


def submit_job(
    request_data: dict,
    key: str,
    limit: int,
    *,
    via_gateway: bool = False,
    callback: tuple[str, str] | None = None,
) -> dict:
    """Validate, persist, and queue a new transcription request.

    Purpose:
        Implement the shared job-submission logic behind the public POST route.
    Args:
        request_data: Validated request fields represented as a dictionary.
        key: Client key from `client_key` (address or gateway-identified account).
        limit: Hourly job allowance for that key.
        via_gateway: True when a trusted gateway submitted the job.
        callback: `(url, key fingerprint)` to notify when the job finishes.
    Returns:
        Job ID, status, submission time and polling/transcript endpoint paths;
        `deduplicated: true` when an identical job was already queued or running,
        `cached: true` when a saved transcript answered the request at once.
    Raises:
        HTTPException: With status 400 for an invalid URL or 429 when busy/limited.
    Workflow:
        Validates the URL, then under the lock: returns an identical active job, or
        completes immediately from an earlier job's saved files, or checks admission,
        creates and persists a queued job and submits `run_transcription_job` to the
        single-worker executor. Only new queued jobs count against the job limits;
        answers from saved files have their own hourly budget (JOB_REUSE_*), and a
        caller past it is queued like any other request.
    Connects to:
        Calls URL validation, progress building, persistence, background execution and
        the keep-alive; called by `create_transcription_job`.
    """
    is_valid, error = validate_youtube_url(request_data["youtube_url"])
    if not is_valid:
        raise HTTPException(status_code=400, detail=error)

    request_data = pipeline_request(request_data)
    request_data.setdefault("language", DEFAULT_LANGUAGE)
    video_id = extract_youtube_video_id(request_data["youtube_url"])
    language = request_data["language"]
    mode = mode_of(request_data)
    job_id = str(uuid4())

    with jobs_lock:
        active = find_active_job_unlocked(video_id, language, mode)
        if active is not None:
            add_callback_unlocked(active["job_id"], callback)
            return job_response(active, deduplicated=True)

        source = None
        if request_data.get("use_cached_outputs", True):
            source = find_reusable_job_unlocked(video_id, language, mode)
        if source is not None and not reuse_allowed_unlocked(key, monotonic()):
            source = None
        if source is not None:
            response = job_response(
                reuse_completed_job_unlocked(source, job_id, request_data, via_gateway),
                cached=True,
            )
            trim_job_history_unlocked()
        else:
            now = monotonic()
            check_job_admission_unlocked(key, limit, now)
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
                "title": None,
                "video_id": video_id,
                "language": language,
                "mode": mode,
                "detected_language": None,
                "format_version": None,
                "video_duration_seconds": None,
                "via_gateway": via_gateway,
            }
            add_callback_unlocked(job_id, callback)
            job_submissions.append(now)
            job_submissions_by_client.setdefault(key, deque()).append(now)
            response = job_response(jobs[job_id])
        try:
            persist_jobs_unlocked(force=True)
        except Exception as exc:  # The job lives in memory; the next save writes it.
            logger.warning("Could not save job %s: %s", job_id, exc)

    if source is not None:
        dispatch_callbacks(job_id, "completed", [callback] if callback else [])
        return response

    executor.submit(run_transcription_job, job_id, request_data)
    ensure_keepalive()
    return response


def resume_queued_jobs() -> int:
    """Queue again the jobs that were waiting when the server stopped; returns how many."""
    with jobs_lock:
        waiting = sorted(
            (
                (job.get("submitted_at") or 0, job_id, pipeline_request(job.get("request") or {}))
                for job_id, job in jobs.items()
                if job.get("status") == "queued"
            ),
            key=lambda item: (item[0], item[1]),
        )
    for _submitted_at, job_id, request_data in waiting:
        executor.submit(run_transcription_job, job_id, request_data)
    if waiting:
        ensure_keepalive()
    return len(waiting)


def count_jobs_by_status() -> dict[str, int]:
    """Number of jobs per status, read under the lock."""
    counts: dict[str, int] = {}
    with jobs_lock:
        for job in jobs.values():
            status = job.get("status") or "unknown"
            counts[status] = counts.get(status, 0) + 1
    return counts


def keepalive_url() -> str | None:
    """The public health URL to ping while jobs run, or None when the keep-alive is off.

    Render injects RENDER_EXTERNAL_URL. Its free plan stops an instance after about
    15 minutes without inbound requests, even mid-job; work in background threads and
    outbound calls do not count, but a request through the public URL does.
    """
    if _env_flag("KEEPALIVE_DISABLED"):
        return None
    base = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    return f"{base}/health" if base else None


def has_active_jobs() -> bool:
    """True while any job is queued or running."""
    with jobs_lock:
        return any(job.get("status") in ACTIVE_STATUSES for job in jobs.values())


def keepalive_grace_seconds() -> int:
    """KEEPALIVE_GRACE_SECONDS (0 allowed), else KEEPALIVE_DEFAULT_GRACE_SECONDS."""
    value = os.environ.get("KEEPALIVE_GRACE_SECONDS", "").strip()
    return int(value) if value.isdigit() else KEEPALIVE_DEFAULT_GRACE_SECONDS


def keepalive_needed() -> bool:
    """Whether the instance must stay awake: a job is queued or running, a finish notice
    is still being delivered, or the last job finished less than the grace period ago,
    so a gateway can still fetch the result before Render stops the instance and its
    disk (and every finished transcript) is wiped."""
    if has_active_jobs():
        return True
    with jobs_lock:
        if callback_deliveries:
            return True
        finished_at = last_finished_at
    return time() - finished_at < keepalive_grace_seconds()


def keepalive_loop() -> None:
    """Ping the public health URL every KEEPALIVE_INTERVAL_SECONDS while `keepalive_needed`.

    The decision to stop and the `keepalive_running` flag change under one lock, so
    a job or finish notice started at that moment either keeps this loop alive or
    starts a new one.
    """
    global keepalive_running
    while True:
        sleep(_limit_from_env("KEEPALIVE_INTERVAL_SECONDS", KEEPALIVE_DEFAULT_INTERVAL_SECONDS))
        url = keepalive_url()
        with keepalive_lock:
            if url is None or not keepalive_needed():
                keepalive_running = False
                return
        try:
            requests.get(url, timeout=10)
        except Exception as exc:
            logger.warning("Keep-alive ping to %s failed: %s", url, exc)


def ensure_keepalive() -> bool:
    """Start the keep-alive loop when it is enabled and not already running; True if started."""
    global keepalive_running
    if keepalive_url() is None:
        return False
    with keepalive_lock:
        if keepalive_running:
            return False
        keepalive_running = True
    try:
        start_background(keepalive_loop)
    except Exception as exc:
        with keepalive_lock:
            keepalive_running = False
        logger.warning("Could not start the keep-alive: %s", exc)
        return False
    return True


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
    return FileResponse(
        Path("front") / "jobs.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health() -> dict:
    """Report whether the API process is running and how busy it is.

    Purpose:
        Provide a lightweight endpoint for Render's health check, the keep-alive,
        gateways and manual checks.
    Args:
        None.
    Returns:
        `{"status": "ok", "active_jobs": <running>, "queued_jobs": <waiting>}`.
    Workflow:
        Counts job states in memory; touches no model, disk or network.
    Connects to:
        Independent of the transcription pipeline.
    """
    counts = count_jobs_by_status()
    return {"status": "ok", "active_jobs": counts.get("running", 0), "queued_jobs": counts.get("queued", 0)}


@app.post("/jobs", status_code=202)
def create_transcription_job(request: TranscriptionRequest, http_request: Request) -> dict:
    """Accept a transcription request and queue it for background processing.

    Purpose:
        Expose asynchronous job creation to the frontend and API clients.
    Args:
        request: FastAPI-validated `TranscriptionRequest` body.
        http_request: Incoming request used to identify the client and gateway.
    Returns:
        Job metadata and URLs (see `submit_job`).
    Workflow:
        Converts the Pydantic model to a dictionary, keeps the callback only for a
        trusted gateway, and delegates job creation.
    Connects to:
        Calls `submit_job`; polled later through `get_job`.
    """
    data = request.model_dump()
    gateway_key = matching_gateway_key(http_request)
    callback = accepted_callback(data.pop("callback_url", None), gateway_key)
    return submit_job(
        data,
        *client_key(http_request),
        via_gateway=gateway_key is not None,
        callback=callback,
    )


@app.get("/jobs")
def list_jobs(request: Request) -> dict:
    """Return snapshots of the persisted jobs this caller may list.

    Purpose:
        Populate the previous-jobs frontend and support API history queries.
    Args:
        request: Incoming request; a valid gateway key also lists gateway jobs.
    Returns:
        A dictionary containing a list of deep-copied job records.
    Workflow:
        Jobs submitted through a trusted gateway (students' lectures) are listed only
        to a caller with a gateway key; reading one by its ID stays open because the
        random ID is only known to whoever submitted it.
    Connects to:
        Reads jobs loaded by `load_jobs` and updated by background workers.
    """
    include_gateway_jobs = is_trusted_gateway(request)
    with jobs_lock:
        return {
            "jobs": [
                job_snapshot(job)
                for job in jobs.values()
                if include_gateway_jobs or not job.get("via_gateway")
            ]
        }


def jobs_ahead_unlocked(job: dict) -> int:
    """How many jobs the single worker will run before this queued one."""
    submitted_at = job.get("submitted_at") or 0
    return sum(
        1
        for other in jobs.values()
        if other is not job
        and (
            other.get("status") == "running"
            or (other.get("status") == "queued" and (other.get("submitted_at") or 0) < submitted_at)
        )
    )


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    """Return the current state of one transcription job.

    Purpose:
        Support frontend polling for status, progress, errors, and final result metadata.
    Args:
        job_id: UUID returned by job submission.
    Returns:
        A snapshot of the matching job; queued jobs also carry `jobs_ahead`.
    Raises:
        HTTPException: With status 404 when the job is unknown.
    Workflow:
        Copies the job under the lock and counts the jobs ahead of a queued one.
    Connects to:
        Polled by the submission and jobs frontends and by gateways.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        snapshot = job_snapshot(job)
        if snapshot.get("status") == "queued":
            snapshot["jobs_ahead"] = jobs_ahead_unlocked(job)
    return snapshot


@app.get("/jobs/{job_id}/transcript")
def get_transcript(job_id: str, kind: str = "cleaned") -> PlainTextResponse:
    """Return the raw or cleaned transcript text for a completed job.

    Purpose:
        Deliver transcript content directly to the frontend or API clients.
    Args:
        job_id: UUID of the completed transcription job.
        kind: `cleaned` for the formatted Markdown text or `raw` for the Whisper text.
    Returns:
        UTF-8 plain-text response with `Content-Language` (when the spoken language is
        known) and `X-Transcript-Format` (`markdown` or `plain`) headers.
    Raises:
        HTTPException: For an invalid kind, unknown job, unfinished job, missing result
            path, or missing transcript file.
    Workflow:
        Validates selection and job state, chooses the result path, verifies the file,
        and reads it as UTF-8, marking any undecodable bytes instead of dropping them.
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
        raise HTTPException(status_code=404, detail=f"No {kind} transcript for this job.")

    path = Path(transcript_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"The {kind} transcript is no longer stored on this server.")

    headers = {"X-Transcript-Format": "markdown" if kind == "cleaned" else "plain"}
    language = job.get("detected_language") or result.get("detected_language")
    if isinstance(language, str) and CONTENT_LANGUAGE_PATTERN.fullmatch(language):
        headers["Content-Language"] = language
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"), headers=headers)


jobs.update(load_jobs())
trim_job_history_unlocked()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
