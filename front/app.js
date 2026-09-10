const form = document.querySelector("#job-form");
const urlInput = document.querySelector("#youtube-url");
const submitButton = document.querySelector("#submit-button");
const statusPanel = document.querySelector("#status-panel");
const statusBand = document.querySelector("#status-band");
const stageTile = document.querySelector("#stage-tile");
const stageRail = document.querySelector("#stage-rail");
const routePreview = document.querySelector("#route-preview");
const resultPanel = document.querySelector("#result-panel");
const statusBadge = document.querySelector("#job-status");
const stepLabel = document.querySelector("#step-label");
const stepCount = document.querySelector("#step-count");
const progressRule = document.querySelector("#progress-rule");
const progressFill = document.querySelector("#progress-fill");
const etaLabel = document.querySelector("#eta-label");
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
const decision = document.querySelector("#decision");
const ask = document.querySelector(".ask");
const queueNote = document.querySelector("#queue-note");
const jobSourceEl = document.querySelector("#job-source");

// The route, in the order the pipeline walks it. `note` is what the stop is
// worth in wall-clock time before anything real is known about this job.
const STAGES = [
  { key: "queued", name: "In queue", icon: "queue", note: "one job at a time" },
  { key: "checking_cache", name: "Checking archive", icon: "archive", note: "seconds" },
  { key: "downloading", name: "Downloading audio", icon: "download", note: "1-3 min" },
  { key: "transcribing", name: "Transcribing", icon: "transcribe", note: "the long one" },
  { key: "formatting", name: "Cleaning text", icon: "clean", note: "2-8 min" },
  { key: "saving", name: "Saving", icon: "save", note: "seconds" },
];

// A cache hit closes the two expensive stops the way a closed route drops off
// a sign panel: struck through, still named, never silently removed.
const SKIPPED_ON_CACHE_HIT = ["downloading", "transcribing"];

const STATUS_BANDS = {
  queued: "band-quiet",
  running: "band-wayfinding",
  completed: "band-arrival",
  failed: "band-closed",
};

const STATUS_WORDS = {
  queued: "Queued",
  running: "Running",
  completed: "Arrived",
  failed: "Stopped",
};

const notesByStage = {
  queued: [
    "Another lecture is on the machine. Yours starts the moment it finishes.",
    "Only one job runs at a time so the GPU and Ollama stay stable.",
  ],
  checking_cache: [
    "If this lecture has been through here before, the text comes straight back.",
    "The archive is keyed on the video ID, so any link to the same lecture hits it.",
  ],
  downloading: [
    "Pulling the audio track before Whisper can hear it.",
    "Longer lectures take longer to fetch.",
  ],
  transcribing: [
    "Whisper is listening through the lecture and writing it down.",
    "This is the long stop. You can leave the tab and come back.",
    "The first run for a lecture is the slow one. The next request for it is not.",
  ],
  formatting: [
    "The cleaner is fixing punctuation, paragraphs, and run-on sentences.",
    "The text is cleaned in chunks, which is why this stop has its own count.",
  ],
  saving: [
    "Writing the transcript to the archive.",
    "The temporary MP3 is deleted once the text is safely stored.",
  ],
};

let activeJobId = null;
let pollTimer = null;
let clockTimer = null;
let noteTimer = null;
let activeStage = null;
let activeStageStartedAt = Date.now();
let activeJobStartedAt = Date.now();
let activeEstimateSeconds = 0;
let noteIndex = 0;
let cacheHit = false;
let lastRailSignature = "";

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

function formatDuration(seconds) {
  const safeSeconds = Math.max(0, Math.round(seconds || 0));
  if (safeSeconds < 60) {
    return `${safeSeconds}s`;
  }
  const minutes = Math.floor(safeSeconds / 60);
  const rest = safeSeconds % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

function setProgress(percent) {
  const fraction = Math.max(0, Math.min(100, percent || 0)) / 100;
  progressFill.style.setProperty("--progress", String(fraction));
}

/* The sign re-signs itself: one authored moment, and only when the message
   it carries has actually changed. */
function resign() {
  statusBand.classList.remove("resigning");
  void statusBand.offsetWidth;
  statusBand.classList.add("resigning");
}

function setStatus(status) {
  const value = (status || "queued").toLowerCase();
  statusBadge.textContent = STATUS_WORDS[value] || value;
  statusBand.className = `band ${STATUS_BANDS[value] || "band-quiet"}`;
  statusBand.dataset.status = value;
  progressRule.dataset.status = value;
}

function setStageIcon(name) {
  stageTile.innerHTML = `<svg class="pict"><use href="#i-${name}"></use></svg>`;
}

function setMessage(message, isError = false) {
  jobMessage.textContent = message;
  jobMessage.dataset.tone = isError ? "error" : "plain";
}

/* Once a job is on the machine the form is no longer the point of the page,
   so it steps down to a strip under the sign. */
function setDecisionMode(secondary) {
  decision.classList.toggle("is-secondary", secondary);
  ask.textContent = secondary ? "Start another lecture" : "Which lecture?";
  routePreview.classList.toggle("is-hidden", secondary);
}

function setSource(url) {
  if (url) {
    jobSourceEl.textContent = url.replace(/^https?:\/\/(www\.)?/, "");
    jobSourceEl.href = url;
  } else {
    jobSourceEl.textContent = "-";
    jobSourceEl.removeAttribute("href");
  }
}

function setSubmitDisabled(disabled) {
  submitButton.disabled = disabled;
  queueNote.classList.toggle("is-hidden", !disabled);
}

function stageIndex(stage) {
  return STAGES.findIndex((entry) => entry.key === stage);
}

function railStates(stage, status) {
  // `cache_hit` is not its own stop; it is the archive stop answering yes.
  const normalised = stage === "cache_hit" ? "checking_cache" : stage;
  const current = stageIndex(normalised);

  return STAGES.map((entry, index) => {
    if (cacheHit && SKIPPED_ON_CACHE_HIT.includes(entry.key)) {
      return { entry, state: "dropped", note: "not needed" };
    }
    if (status === "failed" && index === current) {
      return { entry, state: "failed", note: "stopped here" };
    }
    if (status === "completed") {
      return { entry, state: "done", note: "done" };
    }
    if (current === -1) {
      return { entry, state: "ahead", note: entry.note };
    }
    if (index < current) {
      return { entry, state: "done", note: "done" };
    }
    if (index === current) {
      return { entry, state: "current", note: "now" };
    }
    return { entry, state: "ahead", note: entry.note };
  });
}

function renderRail(stage, status) {
  const stops = railStates(stage, status);
  const signature = stops.map((stop) => `${stop.entry.key}:${stop.state}`).join("|");
  if (signature === lastRailSignature) {
    return;
  }
  lastRailSignature = signature;

  stageRail.innerHTML = stops
    .map(
      (stop) => `
        <li class="stop" data-state="${stop.state}">
          <span class="stop-tile"><svg class="pict"><use href="#i-${stop.entry.icon}"></use></svg></span>
          <span class="stop-body">
            <span class="stop-name">${stop.entry.name}</span>
            <span class="stop-note">${stop.note}</span>
          </span>
        </li>`
    )
    .join("");
}

function updateLiveClock() {
  const elapsedSeconds = (Date.now() - activeJobStartedAt) / 1000;
  const stageElapsed = (Date.now() - activeStageStartedAt) / 1000;
  const remaining = Math.max(0, activeEstimateSeconds - stageElapsed);
  elapsedEl.textContent = formatDuration(elapsedSeconds);

  if (statusBand.dataset.status === "completed" || statusBand.dataset.status === "failed") {
    return;
  }
  etaLabel.textContent = "Remaining";
  etaEl.textContent =
    activeEstimateSeconds && remaining > 5 ? `~ ${formatDuration(remaining)}` : "Almost";
}

function rotateNote() {
  const notes = notesByStage[activeStage] || [
    "Keep this tab open. The page picks the job back up if you reload it.",
  ];
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
    urlInput.focus();
    return;
  }

  resetTimers();
  setSubmitDisabled(true);
  activeStage = "queued";
  activeStageStartedAt = Date.now();
  activeJobStartedAt = Date.now();
  activeEstimateSeconds = 30;
  noteIndex = 0;
  cacheHit = false;
  lastRailSignature = "";

  hide(resultPanel);
  setDecisionMode(true);
  show(statusPanel);
  transcriptOutput.textContent = "Waiting for the transcript.";
  transcriptOutput.classList.add("sheet-empty");
  copyButton.disabled = true;
  jobModeEl.textContent = mode.label;
  jobCacheEl.textContent = "checking";
  jobIdEl.textContent = "-";
  setSource(youtubeUrl);
  stepLabel.textContent = "Submitting the job";
  stepCount.textContent = "-";
  setStageIcon("queue");
  setProgress(0);
  setStatus("queued");
  setMessage("The job has been handed to the server.");
  renderRail("queued", "queued");
  resign();
  rotateNote();
  updateLiveClock();
  statusPanel.scrollIntoView({ block: "start", behavior: "smooth" });

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
    setStageIcon("closed");
    stepLabel.textContent = "The job was not accepted";
    etaLabel.textContent = "Stopped after";
    etaEl.textContent = formatDuration((Date.now() - activeJobStartedAt) / 1000);
    setMessage(`${error.message} Check the link and start again.`, true);
    setSubmitDisabled(false);
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
      setSubmitDisabled(false);
      await loadTranscript(job);
    }

    if (job.status === "failed") {
      resetTimers();
      setSubmitDisabled(false);
      setMessage(job.error || "The job stopped before it produced a transcript.", true);
    }
  } catch (error) {
    resetTimers();
    setSubmitDisabled(false);
    setStatus("failed");
    setStageIcon("closed");
    setMessage(`Lost contact with the server: ${error.message}`, true);
  }
}

function renderJob(job) {
  const stage = job.stage || job.status || "queued";
  const status = job.status || "queued";
  const stageChanged = stage !== activeStage;

  if (stageChanged) {
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

  const usedCache = Boolean(
    job.result?.used_cached_cleaned_transcript || job.result?.used_cached_raw_transcript
  );
  cacheHit = usedCache || stage === "cache_hit";

  setStatus(status);
  jobIdEl.textContent = job.job_id;
  setSource(job.request?.youtube_url);
  jobModeEl.textContent = job.request?.clean === false ? "Fast output" : "Better formatting";
  jobCacheEl.textContent = cacheHit ? "hit" : "miss";
  stepLabel.textContent = job.stage_label || "Working through the lecture";
  stepCount.textContent = `${job.current_step ?? "-"} of ${job.total_steps ?? "-"}`;
  setProgress(job.progress_percent);

  const railStage = stage === "cache_hit" ? "checking_cache" : stage;
  const stageEntry = STAGES[stageIndex(railStage)];
  setStageIcon(
    status === "failed"
      ? "closed"
      : status === "completed"
        ? "arrival"
        : stageEntry
          ? stageEntry.icon
          : "queue"
  );
  renderRail(stage, status);

  if (job.chunk_total) {
    show(chunkRow);
    chunkStatus.textContent = `Cleaning chunk ${job.chunk_index || 0} of ${job.chunk_total}.`;
  } else {
    hide(chunkRow);
  }

  if (status === "queued") {
    setMessage("Waiting for the machine to free up.");
  } else if (status === "running") {
    setMessage("");
  } else if (status === "completed") {
    setMessage(
      cacheHit
        ? "This lecture was already in the archive, so nothing had to be transcribed again."
        : "The transcript is saved. Asking for this lecture again will be near-instant."
    );
    setProgress(100);
    stepLabel.textContent = "Transcript ready";
    etaLabel.textContent = "Took";
    etaEl.textContent = formatDuration((Date.now() - activeJobStartedAt) / 1000);
    hide(chunkRow);
    waitingNote.textContent = "The text sits below. Copy it, or start another lecture.";
  } else if (status === "failed") {
    stepLabel.textContent = "The route is closed";
    etaLabel.textContent = "Stopped after";
    etaEl.textContent = formatDuration((Date.now() - activeJobStartedAt) / 1000);
    waitingNote.textContent =
      "Nothing was saved for this lecture. Fixing the link and starting again is safe.";
  }

  if (stageChanged || status === "completed" || status === "failed") {
    resign();
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
      ? `Transcript, cleaned and formatted by ${provider}`
      : "Transcript, cleaned and formatted";
  } else {
    resultSummary.textContent = "Transcript, straight from Whisper and uncleaned";
  }

  try {
    const text = await requestText(`/jobs/${job.job_id}/transcript?kind=${kind}`);
    transcriptOutput.textContent = text || "The transcript came back empty.";
    transcriptOutput.classList.toggle("sheet-empty", !text);
    copyButton.disabled = !text;
  } catch (error) {
    transcriptOutput.textContent = `The transcript could not be read: ${error.message}`;
    transcriptOutput.classList.add("sheet-empty");
    copyButton.disabled = true;
  }
}

async function loadJobFromQuery(jobId) {
  show(statusPanel);
  setDecisionMode(true);
  hide(resultPanel);
  setStatus("queued");
  setMessage("Reading the job from the server.");

  try {
    const job = await requestJson(`/jobs/${jobId}`);
    activeJobId = job.job_id;
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
      setMessage(job.error || "The job stopped before it produced a transcript.", true);
    }
  } catch (error) {
    setStatus("failed");
    setStageIcon("closed");
    stepLabel.textContent = "That job could not be opened";
    setMessage(`${error.message} It may have been cleared from the server.`, true);
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
  try {
    await navigator.clipboard.writeText(transcriptOutput.textContent);
    const label = copyButton.querySelector(".action-label");
    label.textContent = "Copied";
    setTimeout(() => {
      label.textContent = "Copy text";
    }, 2000);
  } catch {
    setMessage("The browser refused clipboard access. Select the text and copy it manually.", true);
  }
});

form.addEventListener("submit", submitJob);
applyQueryParams();
