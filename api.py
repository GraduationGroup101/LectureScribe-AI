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
    result = job.get("result")
    if isinstance(result, dict):
        result.pop("llama_folder_filenames", None)


def load_jobs() -> dict[str, dict]:
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
    with jobs_lock:
        jobs[job_id].update(changes)
        save_jobs_unlocked()


def estimate_stage_seconds(stage: str, clean: bool) -> int:
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

    return {
        "stage": stage,
        "stage_label": details.get("detail") or defaults["label"],
        "progress_percent": progress,
        "current_step": min(step, total_steps),
        "total_steps": total_steps,
        "stage_started_at": time(),
        "estimated_stage_seconds": estimate_stage_seconds(stage, clean),
        "chunk_index": details.get("chunk_index"),
        "chunk_total": details.get("chunk_total"),
        "updated_at": time(),
    }


def update_job_progress(job_id: str, request_data: dict, stage: str, details: dict | None = None) -> None:
    update_job(job_id, status="running", **build_progress_update(stage, request_data, details))


def get_job_or_404(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return deepcopy(job)


def run_transcription_job(job_id: str, request_data: dict) -> None:
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


@app.get("/")
def root() -> dict:
    return {
        "name": "LectureScribe-AI API",
        "app": "/app",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/app", response_class=FileResponse)
def frontend() -> FileResponse:
    return FileResponse(Path("front") / "index.html")


@app.get("/app/jobs", response_class=FileResponse)
def frontend_jobs() -> FileResponse:
    return FileResponse(Path("front") / "jobs.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
def create_transcription_job(request: TranscriptionRequest) -> dict:
    return submit_job(request.model_dump())


@app.get("/jobs")
def list_jobs() -> dict:
    with jobs_lock:
        return {"jobs": deepcopy(list(jobs.values()))}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return get_job_or_404(job_id)


@app.get("/jobs/{job_id}/transcript")
def get_transcript(job_id: str, kind: str = "cleaned") -> PlainTextResponse:
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
