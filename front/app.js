const form = document.querySelector("#job-form");
const urlInput = document.querySelector("#youtube-url");
const submitButton = document.querySelector("#submit-button");
const statusPanel = document.querySelector("#status-panel");
const resultPanel = document.querySelector("#result-panel");
const statusBadge = document.querySelector("#job-status");
const stepLabel = document.querySelector("#step-label");
const stepCount = document.querySelector("#step-count");
const progressFill = document.querySelector("#progress-fill");
const etaEl = document.querySelector("#eta");
const elapsedEl = document.querySelector("#elapsed");
const jobIdEl = document.querySelector("#job-id");
const jobModeEl = document.querySelector("#job-mode");
const jobCacheEl = document.querySelector("#job-cache");
const chunkRow = document.querySelector("#chunk-row");
const chunkStatus = document.querySelector("#chunk-status");
const jobMessage = document.querySelector("#job-message");
const waitingNote = document.querySelector("#waiting-note");
const resultSummary = document.querySelector("#result-summary");
const transcriptOutput = document.querySelector("#transcript-output");
const copyButton = document.querySelector("#copy-transcript");

const notesByStage = {
  queued: [
    "Your request is waiting for the current job to finish.",
    "Only one lecture runs at a time so the GPU and Ollama stay stable.",
  ],
  checking_cache: [
    "If this lecture was processed before, the result can appear much faster.",
    "The cache check avoids downloading and transcribing the same video again.",
  ],
  downloading: [
    "The audio is being prepared before Whisper can start.",
    "Longer videos may take a little more time to download.",
  ],
  transcribing: [
    "Whisper is listening through the lecture and writing the transcript.",
    "This is usually the longest step. Keep this page open.",
    "The first run for a lecture is slower; cached lectures are faster next time.",
  ],
  formatting: [
    "The cleaner is improving structure, punctuation, and readability.",
    "Formatting takes extra time because the transcript is cleaned in chunks.",
    "The text is being formatted first, then translated into English.",
  ],
  saving: [
    "The transcript is being saved and the cache is being updated.",
    "The MP3 can be deleted after the cleaned transcript is safely stored.",
  ],
};

let activeJobId = null;
let activeClean = false;
let pollTimer = null;
let clockTimer = null;
let noteTimer = null;
let activeStage = null;
let activeStageStartedAt = Date.now();
let activeJobStartedAt = Date.now();
let activeEstimateSeconds = 0;
let noteIndex = 0;

function selectedMode() {
  const value = new FormData(form).get("mode");
  return {
    clean: value === "formatted",
    label: value === "formatted" ? "Better formatting" : "Fast output",
  };
}

function show(element) {
  element.classList.remove("is-hidden");
}

function hide(element) {
  element.classList.add("is-hidden");
}

function setStatus(status) {
  const value = status || "queued";
  statusBadge.textContent = value;
  statusBadge.className = `status-badge ${value.toLowerCase()}`;
}

function setMessage(message, isError = false) {
  jobMessage.textContent = message;
  jobMessage.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function formatDuration(seconds) {
  const safeSeconds = Math.max(0, Math.round(seconds || 0));
  if (safeSeconds < 60) {
    return `${safeSeconds}s`;
  }
  const minutes = Math.floor(safeSeconds / 60);
  const rest = safeSeconds % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

function updateLiveClock() {
  const elapsedSeconds = (Date.now() - activeJobStartedAt) / 1000;
  const stageElapsed = (Date.now() - activeStageStartedAt) / 1000;
  const remaining = Math.max(0, activeEstimateSeconds - stageElapsed);
  elapsedEl.textContent = formatDuration(elapsedSeconds);
  etaEl.textContent = activeEstimateSeconds && remaining > 5 ? `about ${formatDuration(remaining)}` : "Almost done";
}

function rotateNote() {
  const notes = notesByStage[activeStage] || ["Keep this page open while your transcript is being prepared."];
  waitingNote.textContent = notes[noteIndex % notes.length];
  noteIndex += 1;
}

function resetTimers() {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  if (clockTimer) {
    clearInterval(clockTimer);
  }
  if (noteTimer) {
    clearInterval(noteTimer);
  }
  pollTimer = null;
  clockTimer = null;
  noteTimer = null;
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data?.detail || response.statusText;
    throw new Error(detail);
  }
  return data;
}

async function requestText(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      // Keep the HTTP status text when the body is not JSON.
    }
    throw new Error(detail);
  }
  return response.text();
}

async function submitJob(event) {
  event.preventDefault();

  const mode = selectedMode();
  const youtubeUrl = urlInput.value.trim();
  if (!youtubeUrl) {
    return;
  }

  resetTimers();
  submitButton.disabled = true;
  activeClean = mode.clean;
  activeStage = "queued";
  activeStageStartedAt = Date.now();
  activeJobStartedAt = Date.now();
  activeEstimateSeconds = 30;
  noteIndex = 0;

  hide(resultPanel);
  show(statusPanel);
  transcriptOutput.textContent = "Waiting for transcript...";
  copyButton.disabled = true;
  jobModeEl.textContent = mode.label;
  jobCacheEl.textContent = "-";
  jobIdEl.textContent = "-";
  stepLabel.textContent = "Submitting job...";
  stepCount.textContent = "-";
  progressFill.style.width = "0%";
  setStatus("queued");
  setMessage("Job submitted.");
  rotateNote();
  updateLiveClock();

  try {
    const created = await requestJson("/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        youtube_url: youtubeUrl,
        clean: mode.clean,
        skip_audio_cache: false,
        use_cached_outputs: true,
        language: "ar",
      }),
    });

    activeJobId = created.job_id;
    jobIdEl.textContent = activeJobId;
    startPolling();
  } catch (error) {
    setStatus("failed");
    setMessage(error.message, true);
    submitButton.disabled = false;
  }
}

function startPolling() {
  pollTimer = setInterval(() => pollJob(activeJobId), 2500);
  clockTimer = setInterval(updateLiveClock, 1000);
  noteTimer = setInterval(rotateNote, 6000);
  pollJob(activeJobId);
}

async function pollJob(jobId) {
  if (!jobId) {
    return;
  }

  try {
    const job = await requestJson(`/jobs/${jobId}`);
    renderJob(job);

    if (job.status === "completed") {
      resetTimers();
      submitButton.disabled = false;
      await loadTranscript(job);
    }

    if (job.status === "failed") {
      resetTimers();
      submitButton.disabled = false;
      setMessage(job.error || "Job failed.", true);
    }
  } catch (error) {
    resetTimers();
    submitButton.disabled = false;
    setStatus("failed");
    setMessage(error.message, true);
  }
}

function renderJob(job) {
  const stage = job.stage || job.status || "queued";
  if (stage !== activeStage) {
    activeStage = stage;
    activeStageStartedAt = Date.now();
    noteIndex = 0;
    rotateNote();
  }
  if (job.started_at) {
    activeJobStartedAt = job.started_at * 1000;
  }
  if (job.stage_started_at) {
    activeStageStartedAt = job.stage_started_at * 1000;
  }
  activeEstimateSeconds = Number(job.estimated_stage_seconds || 0);

  setStatus(job.status);
  jobIdEl.textContent = job.job_id;
  jobModeEl.textContent = job.request?.clean === false ? "Fast output" : "Better formatting";
  stepLabel.textContent = job.stage_label || "Processing lecture.";
  stepCount.textContent = `${job.current_step ?? "-"} of ${job.total_steps ?? "-"}`;
  progressFill.style.width = `${Math.max(0, Math.min(100, job.progress_percent || 0))}%`;

  const usedCache = Boolean(job.result?.used_cached_cleaned_transcript || job.result?.used_cached_raw_transcript);
  jobCacheEl.textContent = usedCache || stage === "cache_hit" ? "Used" : "Not used";

  if (job.chunk_total) {
    show(chunkRow);
    chunkStatus.textContent = `Cleaning chunk ${job.chunk_index || 0} of ${job.chunk_total}`;
  } else {
    hide(chunkRow);
  }

  if (job.status === "queued") {
    setMessage("Waiting in queue.");
  } else if (job.status === "running") {
    setMessage(job.stage_label || "Processing lecture.");
  } else if (job.status === "completed") {
    setMessage("Transcript ready.");
    progressFill.style.width = "100%";
  }

  updateLiveClock();
}

async function loadTranscript(job) {
  const hasCleanedTranscript = Boolean(job.result?.cleaned_transcript_path);
  const kind = hasCleanedTranscript ? "cleaned" : "raw";
  show(resultPanel);
  if (hasCleanedTranscript) {
    const provider = job.result?.cleaner_provider;
    resultSummary.textContent = provider
      ? `Cleaned transcript is ready using ${provider}.`
      : "Cleaned transcript is ready.";
  } else {
    resultSummary.textContent = "Whisper transcript is ready.";
  }

  try {
    const text = await requestText(`/jobs/${job.job_id}/transcript?kind=${kind}`);
    transcriptOutput.textContent = text || "Transcript is empty.";
    copyButton.disabled = !text;
  } catch (error) {
    transcriptOutput.textContent = error.message;
    copyButton.disabled = true;
  }
}

async function loadJobFromQuery(jobId) {
  show(statusPanel);
  hide(resultPanel);
  setStatus("queued");
  setMessage("Loading job...");

  try {
    const job = await requestJson(`/jobs/${jobId}`);
    activeJobId = job.job_id;
    activeClean = job.request?.clean !== false;
    activeStage = job.stage || job.status || "queued";
    activeStageStartedAt = Date.now();
    activeJobStartedAt = (job.started_at || job.submitted_at || Date.now() / 1000) * 1000;
    activeEstimateSeconds = Number(job.estimated_stage_seconds || 0);
    renderJob(job);

    if (job.status === "completed") {
      await loadTranscript(job);
    } else if (job.status === "running" || job.status === "queued") {
      startPolling();
    } else if (job.status === "failed") {
      setMessage(job.error || "Job failed.", true);
    }
  } catch (error) {
    setStatus("failed");
    setMessage(error.message, true);
  }
}

function applyQueryParams() {
  const params = new URLSearchParams(window.location.search);
  const url = params.get("url");
  const mode = params.get("mode");
  const jobId = params.get("job_id");

  if (url) {
    urlInput.value = url;
  }
  if (mode) {
    const input = document.querySelector(`input[name="mode"][value="${mode}"]`);
    if (input) {
      input.checked = true;
    }
  }
  if (jobId) {
    loadJobFromQuery(jobId);
  }
}

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(transcriptOutput.textContent);
  setMessage("Transcript copied.");
});

form.addEventListener("submit", submitJob);
applyQueryParams();
