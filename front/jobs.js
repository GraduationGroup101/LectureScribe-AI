const refreshJobsButton = document.querySelector("#refresh-jobs");
const jobsList = document.querySelector("#jobs-list");
const jobsCount = document.querySelector("#jobs-count");
const jobSearch = document.querySelector("#job-search");
const statusFilter = document.querySelector("#status-filter");
const boardTotal = document.querySelector("#board-total");

const STATUS_ICONS = {
  completed: "arrival",
  failed: "closed",
  running: "transcribe",
  queued: "queue",
};

const STATUS_WORDS = {
  completed: "Arrived",
  failed: "Stopped",
  running: "Running",
  queued: "Queued",
};

let allJobs = [];

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

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]
  );
}

function formatDate(timestamp) {
  if (!timestamp) {
    return "no date";
  }
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function sourceLabel(job) {
  const url = job.request?.youtube_url;
  if (!url) {
    return job.job_id;
  }
  return url.replace(/^https?:\/\/(www\.)?/, "");
}

function modeLabel(job) {
  return job.request?.clean === false ? "fast output" : "better formatting";
}

function cacheLabel(job) {
  const result = job.result || {};
  if (result.used_cached_cleaned_transcript || result.used_cached_raw_transcript) {
    return "archive hit";
  }
  return "archive miss";
}

function jobActionLabel(job) {
  if (job.status === "completed") {
    return "Open transcript";
  }
  if (job.status === "running" || job.status === "queued") {
    return "Follow";
  }
  return "Open details";
}

function filteredJobs() {
  const query = jobSearch.value.trim().toLowerCase();
  const status = statusFilter.value;
  return allJobs.filter((job) => {
    const haystack = `${job.job_id} ${job.request?.youtube_url || ""}`.toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    const matchesStatus = status === "all" || job.status === status;
    return matchesQuery && matchesStatus;
  });
}

function renderEmpty() {
  const filtering = jobSearch.value.trim() || statusFilter.value !== "all";

  if (filtering) {
    jobsList.innerHTML = `
      <div class="board-empty">
        <h2>Nothing on the board matches</h2>
        <p>
          ${allJobs.length} ${allJobs.length === 1 ? "lecture has" : "lectures have"}
          come through this server. None of them match the search and filter you have set.
        </p>
      </div>`;
    return;
  }

  jobsList.innerHTML = `
    <div class="board-empty">
      <h2>No lectures yet</h2>
      <p>
        Every lecture you send through LectureScribe stays on this board with its state,
        its mode, and its transcript. Nothing has been through yet.
      </p>
      <a class="go" href="/app">
        <span class="tile"><svg class="pict"><use href="#i-arrow-right"></use></svg></span>
        Start the first one
      </a>
    </div>`;
}

function renderJobs() {
  const jobs = filteredJobs();
  jobsList.innerHTML = "";

  boardTotal.textContent = String(allJobs.length);

  if (!jobs.length) {
    renderEmpty();
    jobsCount.textContent = "";
    return;
  }

  for (const job of jobs) {
    const status = job.status || "queued";
    const row = document.createElement("article");
    row.className = "board-row";
    row.dataset.status = status;

    row.innerHTML = `
      <span class="tile"><svg class="pict"><use href="#i-${STATUS_ICONS[status] || "queue"}"></use></svg></span>
      <div class="row-main">
        <span class="row-source">${escapeHtml(sourceLabel(job))}</span>
        <div class="row-meta">
          <span class="row-state">${STATUS_WORDS[status] || escapeHtml(status)}</span>
          <span>${escapeHtml(modeLabel(job))}</span>
          <span>${escapeHtml(cacheLabel(job))}</span>
          <span>${escapeHtml(formatDate(job.submitted_at))}</span>
          <span class="row-id">${escapeHtml(job.job_id)}</span>
        </div>
        ${status === "failed" ? '<span class="row-stamp">Cancelled</span>' : ""}
        ${job.error ? `<p class="row-error">${escapeHtml(job.error)}</p>` : ""}
      </div>
      <div class="row-actions">
        <button type="button" class="action" data-action="reuse">Reuse link</button>
        <button type="button" class="action" data-action="open">${jobActionLabel(job)}</button>
      </div>
    `;

    row.querySelector('[data-action="reuse"]').addEventListener("click", () => {
      const params = new URLSearchParams({
        url: job.request?.youtube_url || "",
        mode: job.request?.clean === false ? "fast" : "formatted",
      });
      window.location.href = `/app?${params.toString()}`;
    });

    row.querySelector('[data-action="open"]').addEventListener("click", () => {
      const params = new URLSearchParams({ job_id: job.job_id });
      window.location.href = `/app?${params.toString()}`;
    });

    jobsList.appendChild(row);
  }

  jobsCount.textContent =
    jobs.length === allJobs.length
      ? `${jobs.length} ${jobs.length === 1 ? "lecture" : "lectures"} on the board`
      : `${jobs.length} of ${allJobs.length} shown`;
}

async function loadJobs() {
  refreshJobsButton.disabled = true;
  try {
    const data = await requestJson("/jobs");
    allJobs = [...(data.jobs || [])].sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
    renderJobs();
  } catch (error) {
    jobsList.innerHTML = `
      <div class="board-empty">
        <h2>The board could not be read</h2>
        <p>${escapeHtml(error.message)} The server may be restarting &mdash; try refreshing in a moment.</p>
      </div>`;
    boardTotal.textContent = "-";
    jobsCount.textContent = "";
  } finally {
    refreshJobsButton.disabled = false;
  }
}

refreshJobsButton.addEventListener("click", loadJobs);
jobSearch.addEventListener("input", renderJobs);
statusFilter.addEventListener("change", renderJobs);
loadJobs();
