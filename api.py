from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Lock
from time import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from MainCode_FasterWhisper import DEFAULT_LANGUAGE, process_youtube_url
from url_to_mp3 import validate_youtube_url


app = FastAPI(
    title="LectureScribe-AI API",
    description="Download lecture audio, transcribe it with faster-whisper, and optionally clean it with Ollama.",
    version="1.0.0",
)

executor = ThreadPoolExecutor(max_workers=1)
JOBS_FILE = Path("jobs.json")
jobs: dict[str, dict] = {}
jobs_lock = Lock()


class TranscriptionRequest(BaseModel):
    youtube_url: str = Field(
        ...,
        description="YouTube video URL to download and transcribe.",
        example="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    clean: bool = Field(
        default=True,
        description="Run the Ollama transcript cleaner after Whisper transcription.",
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
        if job.get("status") in {"queued", "running"}:
            job["status"] = "failed"
            job["error"] = "API server restarted before this job finished."
            job["finished_at"] = job.get("finished_at") or time()

    return loaded_jobs


def save_jobs_unlocked() -> None:
    tmp = JOBS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(JOBS_FILE)


def update_job(job_id: str, **changes) -> None:
    with jobs_lock:
        jobs[job_id].update(changes)
        save_jobs_unlocked()


def get_job_or_404(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return deepcopy(job)


def run_transcription_job(job_id: str, request_data: dict) -> None:
    update_job(job_id, status="running", started_at=time())

    try:
        result = process_youtube_url(**request_data)
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=time(),
        )
        return

    update_job(
        job_id,
        status="completed",
        result=result,
        finished_at=time(),
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
        "docs": "/docs",
        "health": "/health",
    }


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
