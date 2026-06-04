const form = document.querySelector("#job-form");
const urlInput = document.querySelector("#youtube-url");
const submitButton = document.querySelector("#submit-button");
const statusBadge = document.querySelector("#job-status");
const jobIdEl = document.querySelector("#job-id");
const jobModeEl = document.querySelector("#job-mode");
const jobCacheEl = document.querySelector("#job-cache");
const jobMessage = document.querySelector("#job-message");
const transcriptOutput = document.querySelector("#transcript-output");
const copyButton = document.querySelector("#copy-transcript");
const refreshJobsButton = document.querySelector("#refresh-jobs");
const jobsList = document.querySelector("#jobs-list");

let activeJobId = null;
let activeClean = false;
let pollTimer = null;

function selectedMode() {
  const value = new FormData(form).get("mode");
  return {
    clean: value === "formatted",
    label: value === "formatted" ? "Better formatting" : "Fast output",
    transcriptKind: value === "formatted" ? "cleaned" : "raw",
  };
}

function setStatus(status) {
  statusBadge.textContent = status;
  statusBadge.className = `status-badge ${status.toLowerCase()}`;
}

function setMessage(message, isError = false) {
  jobMessage.textContent = message;
  jobMessage.style.color = isError ? "var(--danger)" : "var(--muted)";
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

  submitButton.disabled = true;
  transcriptOutput.textContent = "Waiting for transcript...";
  copyButton.disabled = true;
  activeClean = mode.clean;
  jobModeEl.textContent = mode.label;
  jobCacheEl.textContent = "-";
  setStatus("Queued");
  setMessage("Job submitted.");

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
    await loadJobs();
  } catch (error) {
    setStatus("Failed");
    setMessage(error.message, true);
    transcriptOutput.textContent = "No transcript loaded.";
    submitButton.disabled = false;
  }
}

function startPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  pollTimer = setInterval(() => pollJob(activeJobId), 2500);
  pollJob(activeJobId);
}

async function pollJob(jobId) {
  if (!jobId) {
    return;
  }

  try {
    const job = await requestJson(`/jobs/${jobId}`);
    renderJob(job, activeClean);

    if (job.status === "completed") {
      clearInterval(pollTimer);
      pollTimer = null;
      submitButton.disabled = false;
      await loadTranscript(job, activeClean);
      await loadJobs();
    }

    if (job.status === "failed") {
      clearInterval(pollTimer);
      pollTimer = null;
      submitButton.disabled = false;
      setMessage(job.error || "Job failed.", true);
      await loadJobs();
    }
  } catch (error) {
    clearInterval(pollTimer);
    pollTimer = null;
    submitButton.disabled = false;
    setStatus("Failed");
    setMessage(error.message, true);
  }
}

function renderJob(job, clean) {
  setStatus(job.status);
  jobIdEl.textContent = job.job_id;
  jobModeEl.textContent = clean ? "Better formatting" : "Fast output";
  const usedCache = Boolean(job.result?.used_cached_cleaned_transcript || job.result?.used_cached_raw_transcript);
  jobCacheEl.textContent = usedCache ? "Used" : "Not used";

  if (job.status === "queued") {
    setMessage("Waiting in queue.");
  } else if (job.status === "running") {
    setMessage("Processing lecture.");
  } else if (job.status === "completed") {
    setMessage("Transcript ready.");
  }
}

async function loadTranscript(job, clean) {
  const kind = clean ? "cleaned" : "raw";
  try {
    const text = await requestText(`/jobs/${job.job_id}/transcript?kind=${kind}`);
    transcriptOutput.textContent = text || "Transcript is empty.";
    copyButton.disabled = !text;
  } catch (error) {
    transcriptOutput.textContent = error.message;
    copyButton.disabled = true;
  }
}

async function loadJobs() {
  try {
    const data = await requestJson("/jobs");
    const jobs = [...(data.jobs || [])].sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
    jobsList.innerHTML = "";

    if (!jobs.length) {
      jobsList.innerHTML = '<p class="message">No jobs yet.</p>';
      return;
    }

    for (const job of jobs.slice(0, 8)) {
      const clean = job.request?.clean !== false;
      const item = document.createElement("div");
      item.className = "job-item";
      item.innerHTML = `
        <div>
          <strong>${job.request?.youtube_url || job.job_id}</strong>
          <small>${job.status} - ${clean ? "Better formatting" : "Fast output"}</small>
        </div>
        <button type="button">Open</button>
      `;
      item.querySelector("button").addEventListener("click", async () => {
        activeJobId = job.job_id;
        activeClean = clean;
        renderJob(job, clean);
        if (job.status === "completed") {
          await loadTranscript(job, clean);
        }
      });
      jobsList.appendChild(item);
    }
  } catch (error) {
    jobsList.innerHTML = `<p class="message">${error.message}</p>`;
  }
}

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(transcriptOutput.textContent);
  setMessage("Transcript copied.");
});

refreshJobsButton.addEventListener("click", loadJobs);
form.addEventListener("submit", submitJob);
loadJobs();
