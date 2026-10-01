const form = document.querySelector("#job-form");
const urlInput = document.querySelector("#youtube-url");
const languageInput = document.querySelector("#language");
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
const jobLanguageEl = document.querySelector("#job-language");
const processingDetails = document.querySelector("#processing-details");
const resultTitle = document.querySelector("#result-title");
const resultDuration = document.querySelector("#result-duration");
const resultSource = document.querySelector("#result-source");
const resultNotice = document.querySelector("#result-notice");
const downloadButton = document.querySelector("#download-transcript");

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

const LANGUAGE_NAMES = {
  ar: "Arabic",
  en: "English",
};

const POLL_INTERVAL_MS = 2500;
const MAX_POLL_BACKOFF_MS = 30000;
// One cold-start 502 must not end the job on screen; give up after this many in a row.
const MAX_POLL_FAILURES = 5;

const STATUS_WORDS = {
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
};

const notesByStage = {
  queued: [
    "Another lecture is on the machine. Yours starts the moment it finishes.",
    "You can return to this job from History while you wait.",
  ],
  checking_cache: [
    "If this lecture has been through here before, the text comes straight back.",
    "A saved transcript can be returned without processing the lecture again.",
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
    "The lecture is being punctuated and split into paragraphs, in the language it was spoken.",
    "Long lectures are formatted in parts, which is why this stop has its own count.",
    "Your transcript is being organized for easier reading.",
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
let activeJobFinishedAt = null;
let currentJob = null;
let transcriptText = "";
let pollFailures = 0;

function selectedMode() {
  const value = new FormData(form).get("mode");
  return {
    clean: value === "formatted",
    label: value === "formatted" ? "Better formatting" : "Fast output",
  };
}

function selectedLanguage() {
  return languageInput.value || "auto";
}

function languageName(code) {
  return LANGUAGE_NAMES[code] || String(code).toUpperCase();
}

function describeLanguage(job) {
  const requested = job?.language || job?.request?.language;
  const detected = job?.detected_language || job?.result?.detected_language;
  if (!requested || requested === "auto") {
    if (detected) return `${languageName(detected)} (detected)`;
    return requested === "auto" ? "Detecting" : "\u2014";
  }
  return languageName(requested);
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
  statusPanel.dataset.status = value;
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

function lectureTitle(job) {
  const title = job?.title || job?.result?.title;
  if (title) return String(title);
  // Older jobs only carry the title inside their transcript file name.
  const path = job?.result?.raw_transcript_path || job?.result?.cleaned_transcript_path;
  const filename = path ? path.split(/[\\/]/).pop() : "";
  if (/_transcript(?:_cleanedv\d+)?\.txt$/i.test(filename)) {
    const legacy = filename
      .replace(/\.txt$/i, "")
      .replace(/(?:_english)?_transcript(?:_cleanedv\d+)?$/i, "")
      .replace(/^[A-Za-z0-9_-]{11}_/, "")
      .replace(/_/g, " ").trim();
    if (legacy) return legacy;
  }
  return "Lecture transcript";
}

function lectureUrl(url) {
  try {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) return null;
    const id = parsed.searchParams.get("v") || (
      parsed.hostname === "youtu.be" ? parsed.pathname.slice(1) :
        parsed.pathname.match(/^\/(?:shorts|embed|live)\/([^/]+)/)?.[1]
    );
    if (id && /^[A-Za-z0-9_-]{11}$/.test(id)) {
      return `https://www.youtube.com/watch?v=${id}`;
    }
    return parsed.origin + parsed.pathname;
  } catch {
    return null;
  }
}

function setSource(url, job = null) {
  const link = lectureUrl(url);
  jobSourceEl.textContent = job?.title || job?.result ? lectureTitle(job) : "Watch lecture on YouTube";
  for (const element of [jobSourceEl, resultSource]) {
    if (link) element.href = link;
    else element.removeAttribute("href");
  }
  resultSource.classList.toggle("is-hidden", !link);
}

function setSubmitDisabled(disabled) {
  submitButton.disabled = disabled;
  queueNote.classList.toggle("is-hidden", !disabled);
}

/* A cleaner that never ran is a fact the reader is owed, with the reason
   named rather than buried in the job payload. `outcome` says what is shown instead. */
function describeCleanerFailure(raw, outcome = "This is the original transcript.") {
  if (!raw) {
    return "";
  }
  const text = String(raw).toLowerCase();
  let reason = "Formatting was unavailable.";
  if (text.includes("401") || text.includes("unauthorized") || text.includes("expired")) {
    reason = "Formatting is temporarily unavailable.";
  } else if (text.includes("429") || text.includes("rate limit") || text.includes("quota")) {
    reason = "Formatting is currently busy.";
  } else if (text.includes("timeout") || text.includes("timed out")) {
    reason = "Formatting took too long.";
  } else if (text.includes("connection") || text.includes("refused") || text.includes("ollama")) {
    reason = "Formatting could not be reached.";
  }
  return `${reason} ${outcome}`;
}

/* "Better formatting" was asked for, but the service returned its automatic paragraph
   layout (result mode "fast"): no model was usable, or the model stopped part-way. The
   transcript file still exists, so its presence says nothing about formatting. */
function formattingFellBack(job) {
  return job?.request?.clean !== false
    && (job?.result?.mode === "fast" || job?.result?.cleaner_provider === "formatter");
}

function formattingStoppedPartWay(job) {
  return Boolean(job?.result?.cleaner_partial) && job?.result?.cleaner_provider !== "formatter";
}

function describeFormattingFallback(job) {
  if (formattingStoppedPartWay(job)) {
    return "AI formatting stopped part-way through, so the rest of the lecture was arranged into paragraphs automatically.";
  }
  return describeCleanerFailure(job?.result?.cleaner_error, "The transcript was arranged into paragraphs automatically.")
    || "AI formatting was unavailable, so the transcript was arranged into paragraphs automatically.";
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
    if (entry.key === "formatting" && status === "completed") {
      if (currentJob?.result?.used_cached_cleaned_transcript) {
        return { entry, state: "dropped", note: "not needed" };
      }
      if (formattingFellBack(currentJob)) {
        return { entry, state: "failed", note: formattingStoppedPartWay(currentJob) ? "partly done" : "unavailable" };
      }
      // Jobs from before every result carried a cleaned file.
      if (!currentJob?.result?.cleaned_transcript_path) {
        return currentJob?.result?.cleaner_error
          ? { entry, state: "failed", note: "unavailable" }
          : { entry, state: "dropped", note: "not applied" };
      }
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
  const elapsedSeconds = ((activeJobFinishedAt || Date.now()) - activeJobStartedAt) / 1000;
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
    clearTimeout(pollTimer);
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
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    // Validation errors (422) carry a list of {msg} objects rather than a sentence.
    const detail = Array.isArray(data?.detail)
      ? data.detail.map((item) => item?.msg).filter(Boolean).join(" ")
      : data?.detail;
    const error = new Error(detail || response.statusText || `HTTP ${response.status}`);
    error.status = response.status;
    error.retryAfter = Number(response.headers.get("Retry-After"));
    throw error;
  }
  return data;
}

async function requestText(url) {
  const response = await fetch(url, { cache: "no-store" });
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
  activeJobFinishedAt = null;
  currentJob = null;
  transcriptText = "";
  activeEstimateSeconds = 30;
  noteIndex = 0;
  cacheHit = false;
  lastRailSignature = "";

  hide(resultPanel);
  resultPanel.before(statusPanel);
  processingDetails.open = true;
  setDecisionMode(true);
  show(statusPanel);
  setSheetMessage("Waiting for the transcript.");
  copyButton.disabled = true;
  downloadButton.disabled = true;
  resultNotice.textContent = "";
  jobModeEl.textContent = mode.label;
  jobLanguageEl.textContent = selectedLanguage() === "auto" ? "Detecting" : languageName(selectedLanguage());
  jobCacheEl.textContent = "Checking";
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
        language: selectedLanguage(),
      }),
    });

    activeJobId = created.job_id;
    const location = new URL(window.location.href);
    location.search = new URLSearchParams({ job_id: activeJobId }).toString();
    window.history.replaceState(null, "", location);
    jobIdEl.textContent = activeJobId;
    startPolling();
  } catch (error) {
    setStatus("failed");
    setStageIcon("closed");
    stepLabel.textContent = "The job was not accepted";
    etaLabel.textContent = "Stopped after";
    etaEl.textContent = formatDuration((Date.now() - activeJobStartedAt) / 1000);
    const retry = error.status === 429 && error.retryAfter > 0
      ? ` Try again in about ${formatDuration(error.retryAfter)}.`
      : "";
    const message = error.status === 429
      ? `${error.message}${retry}`
      : `${error.message} Check the link and start again.`;
    setMessage(message, true);
    setSubmitDisabled(false);
  }
}

function startPolling() {
  pollFailures = 0;
  clockTimer = setInterval(updateLiveClock, 1000);
  noteTimer = setInterval(rotateNote, 6000);
  pollJob(activeJobId);
}

/* One poll at a time: the next one is scheduled only after this one settles,
   and transient errors back off instead of ending the job on screen. */
function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => pollJob(activeJobId), delay);
}

async function pollJob(jobId) {
  if (!jobId || jobId !== activeJobId) {
    return;
  }

  try {
    const job = await requestJson(`/jobs/${jobId}`);
    if (jobId !== activeJobId) return;
    pollFailures = 0;
    renderJob(job);

    if (job.status === "completed") {
      resetTimers();
      setSubmitDisabled(false);
      await loadTranscript(job);
      return;
    }

    if (job.status === "failed") {
      resetTimers();
      setSubmitDisabled(false);
      setMessage(job.error || "The job stopped before it produced a transcript.", true);
      return;
    }
    schedulePoll(POLL_INTERVAL_MS);
  } catch (error) {
    if (jobId !== activeJobId) return;
    pollFailures += 1;
    if (error.status === 404 || pollFailures >= MAX_POLL_FAILURES) {
      resetTimers();
      setSubmitDisabled(false);
      setStatus("failed");
      setStageIcon("closed");
      setMessage(
        error.status === 404
          ? "The server no longer has this job, usually because it restarted. Start the lecture again; a saved transcript is reused when there is one."
          : `Lost contact with the server: ${error.message}`,
        true
      );
      return;
    }
    setMessage(`Reconnecting to the server (${error.message || "no response"}).`);
    schedulePoll(Math.min(POLL_INTERVAL_MS * 2 ** pollFailures, MAX_POLL_BACKOFF_MS));
  }
}

function renderJob(job) {
  currentJob = job;
  const stage = job.stage || job.status || "queued";
  const status = job.status || "queued";
  const stageChanged = stage !== activeStage;

  if (stageChanged) {
    activeStage = stage;
    activeStageStartedAt = Date.now();
    noteIndex = 0;
    rotateNote();
  }
  activeJobStartedAt = (job.started_at || job.submitted_at || Date.now() / 1000) * 1000;
  activeJobFinishedAt = ["completed", "failed"].includes(status)
    ? (job.finished_at || job.updated_at || Date.now() / 1000) * 1000 : null;
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
  setSource(job.request?.youtube_url, job);
  jobModeEl.textContent = job.request?.clean === false ? "Fast output" : "Better formatting";
  jobLanguageEl.textContent = describeLanguage(job);
  jobCacheEl.textContent = cacheHit ? "Reused" : status === "queued" || stage === "checking_cache"
    ? "Checking" : "New transcript";
  stepLabel.textContent = job.stage_label || "Working through the lecture";
  const step = status === "completed" ? STAGES.length : stageIndex(stage === "cache_hit" ? "checking_cache" : stage) + 1;
  stepCount.textContent = step > 0 ? `${step} of ${STAGES.length}` : "-";
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
    const action = stage === "transcribing" ? "Transcribing" : "Formatting";
    chunkStatus.textContent = `${action} part ${job.chunk_index || 0} of ${job.chunk_total}.`;
  } else {
    hide(chunkRow);
  }

  if (status === "queued") {
    const ahead = Number(job.jobs_ahead || 0);
    setMessage(ahead > 0
      ? `${ahead} ${ahead === 1 ? "lecture is" : "lectures are"} ahead of yours.`
      : "Waiting for the machine to free up.");
  } else if (status === "running") {
    setMessage("");
  } else if (status === "completed") {
    const cleanerNote = formattingFellBack(job)
      ? describeFormattingFallback(job)
      : job.result?.cleaned_transcript_path ? "" : describeCleanerFailure(job.result?.cleaner_error);
    const savedNote = cacheHit ? "A saved transcript was reused." : "Transcript saved.";
    setMessage(cleanerNote ? `${cleanerNote} ${savedNote}` : savedNote);
    resultNotice.textContent = cleanerNote;
    setProgress(100);
    stepLabel.textContent = "Transcript ready";
    etaLabel.textContent = "Took";
    etaEl.textContent = formatDuration((activeJobFinishedAt - activeJobStartedAt) / 1000);
    hide(chunkRow);
    waitingNote.textContent = "";
    if (statusPanel.previousElementSibling !== resultPanel) {
      resultPanel.after(statusPanel);
      processingDetails.open = false;
    }
  } else if (status === "failed") {
    stepLabel.textContent = "Could not complete the transcript";
    etaLabel.textContent = "Stopped after";
    etaEl.textContent = formatDuration((activeJobFinishedAt - activeJobStartedAt) / 1000);
    waitingNote.textContent =
      "You can retry this lecture. Any available saved transcript will be reused.";
  }

  if (stageChanged || status === "completed" || status === "failed") {
    resign();
  }

  updateLiveClock();
}

/* The transcript is a small Markdown subset: # headings, blank-line paragraphs,
   "- " bullets, "1. " items and **bold**. Each block takes its own direction
   (dir="auto"), so Arabic paragraphs read right-to-left and English ones
   left-to-right. Everything is built as DOM nodes; nothing is parsed as HTML. */
const HEADING_LINE = /^(#{1,3})\s+(.+)$/;
const BULLET_LINE = /^\s*[-*\u2022]\s+(.+)$/;
const NUMBERED_LINE = /^\s*(\d+)[.)]\s+(.+)$/;

function appendInline(element, text) {
  text.split(/\*\*(.+?)\*\*/).forEach((part, index) => {
    if (!part) return;
    if (index % 2) {
      const strong = document.createElement("strong");
      strong.textContent = part;
      element.append(strong);
    } else {
      element.append(document.createTextNode(part));
    }
  });
}

function textBlock(tag, text, ownDirection = true) {
  const element = document.createElement(tag);
  if (ownDirection) element.dir = "auto";
  appendInline(element, text);
  return element;
}

function renderBlock(block) {
  const fragment = document.createDocumentFragment();
  let paragraph = [];
  let list = null;
  const flushParagraph = () => {
    if (paragraph.length) fragment.append(textBlock("p", paragraph.join("\n")));
    paragraph = [];
  };

  for (const line of block.split("\n")) {
    const heading = line.match(HEADING_LINE);
    const bullet = line.match(BULLET_LINE);
    const numbered = bullet ? null : line.match(NUMBERED_LINE);
    if (heading) {
      flushParagraph();
      list = null;
      fragment.append(textBlock(`h${heading[1].length + 1}`, heading[2]));
    } else if (bullet || numbered) {
      flushParagraph();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = document.createElement(tag);
        list.dir = "auto";
        if (numbered) list.start = Number(numbered[1]);
        fragment.append(list);
      }
      // Items follow the list's direction; a dir on each item would hide their
      // text from the list's own dir="auto" detection and flip it to LTR.
      list.append(textBlock("li", bullet ? bullet[1] : numbered[2], false));
    } else {
      list = null;
      paragraph.push(line);
    }
  }
  flushParagraph();
  return fragment;
}

function renderTranscript(text, language) {
  transcriptOutput.replaceChildren();
  transcriptOutput.classList.remove("sheet-empty");
  if (language) transcriptOutput.lang = language;
  else transcriptOutput.removeAttribute("lang");
  // Blank-line separators stay as text nodes, so the page text is the transcript itself.
  for (const part of text.replace(/\r\n?/g, "\n").split(/(\n[ \t]*\n\s*)/)) {
    if (!part) continue;
    transcriptOutput.append(part.trim() ? renderBlock(part) : document.createTextNode(part));
  }
}

function setSheetMessage(message) {
  transcriptOutput.textContent = message;
  transcriptOutput.removeAttribute("lang");
  transcriptOutput.classList.add("sheet-empty");
}

async function loadTranscript(job) {
  const hasCleanedTranscript = Boolean(job.result?.cleaned_transcript_path);
  const kind = hasCleanedTranscript ? "cleaned" : "raw";
  show(resultPanel);

  resultTitle.textContent = lectureTitle(job);
  // The cleaned file of a fallback is the automatic paragraph layout, so it is still the one shown.
  resultSummary.textContent = formattingFellBack(job)
    ? formattingStoppedPartWay(job)
      ? "Partly formatted (AI formatting stopped part-way)"
      : "Automatic paragraphs (AI formatting unavailable)"
    : hasCleanedTranscript ? "Formatted transcript" : "Original transcript";
  resultDuration.textContent = cacheHit ? "Saved transcript reused" :
    `Processed in ${formatDuration((activeJobFinishedAt - activeJobStartedAt) / 1000)}`;
  setSheetMessage("Loading transcript...");
  copyButton.disabled = true;
  downloadButton.disabled = true;

  try {
    const text = await requestText(`/jobs/${job.job_id}/transcript?kind=${kind}`);
    transcriptText = text;
    if (text) {
      renderTranscript(text, job.detected_language || job.result?.detected_language);
    } else {
      setSheetMessage("The transcript came back empty.");
    }
    copyButton.disabled = !text;
    downloadButton.disabled = !text;
  } catch (error) {
    setSheetMessage(`The transcript could not be read: ${error.message}`);
    copyButton.disabled = true;
    downloadButton.disabled = true;
    resultNotice.textContent = "Could not load the saved transcript. Reload the page to try again.";
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
      setSubmitDisabled(true);
      startPolling();
    } else if (job.status === "failed") {
      setMessage(job.error || "The job stopped before it produced a transcript.", true);
    }
  } catch (error) {
    if (error.status !== 404) {
      // A waking or redeploying server answers 502/503 for a moment: keep trying.
      activeJobId = jobId;
      setSubmitDisabled(true);
      setMessage(`Reconnecting to the server (${error.message || "no response"}).`);
      clockTimer = setInterval(updateLiveClock, 1000);
      noteTimer = setInterval(rotateNote, 6000);
      pollFailures = 1;
      schedulePoll(POLL_INTERVAL_MS * 2);
      return;
    }
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
  const language = params.get("language");
  const jobId = params.get("job_id");

  if (url) {
    urlInput.value = url;
  }
  if (language && [...languageInput.options].some((option) => option.value === language)) {
    languageInput.value = language;
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
    await navigator.clipboard.writeText(transcriptText);
    const label = copyButton.querySelector(".action-label");
    label.textContent = "Copied";
    setTimeout(() => {
      label.textContent = "Copy text";
    }, 2000);
  } catch {
    resultNotice.textContent = "Clipboard access is unavailable. Select the transcript to copy it.";
  }
});

downloadButton.addEventListener("click", () => {
  if (!transcriptText) return;
  const blobUrl = URL.createObjectURL(new Blob([transcriptText], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = `${lectureTitle(currentJob).replace(/[<>:"/\\|?*\x00-\x1F]/g, "_")}_transcript.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
});

form.addEventListener("submit", submitJob);
applyQueryParams();
