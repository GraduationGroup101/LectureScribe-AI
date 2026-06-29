const refreshJobsButton = document.querySelector("#refresh-jobs");
const jobsList = document.querySelector("#jobs-list");
const jobSearch = document.querySelector("#job-search");
const statusFilter = document.querySelector("#status-filter");

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

function formatDate(timestamp) {
  if (!timestamp) {
    return "-";
  }
  return new Date(timestamp * 1000).toLocaleString();
}

function modeLabel(job) {
  return job.request?.clean === false ? "Fast output" : "Better formatting";
}

function cacheLabel(job) {
  const result = job.result || {};
  if (result.used_cached_cleaned_transcript || result.used_cached_raw_transcript) {
    return "cache used";
  }
  return "cache not used";
}

function jobActionLabel(job) {
  if (job.status === "completed") {
    return "Open Result";
  }
  if (job.status === "running" || job.status === "queued") {
    return "View Status";
  }
  return "View Details";
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

function renderJobs() {
  const jobs = filteredJobs();
  jobsList.innerHTML = "";

  if (!jobs.length) {
    jobsList.innerHTML = '<p class="message">No jobs match this filter.</p>';
    return;
  }

  for (const job of jobs) {
    const item = document.createElement("article");
    item.className = `job-card ${job.status}`;
    const actionLabel = jobActionLabel(job);
    item.innerHTML = `
      <div class="job-card-main">
        <strong>${job.request?.youtube_url || job.job_id}</strong>
        <small>${job.job_id}</small>
        <div class="job-meta">
          <span>${job.status}</span>
          <span>${modeLabel(job)}</span>
          <span>${cacheLabel(job)}</span>
          <span>${formatDate(job.submitted_at)}</span>
        </div>
        ${job.error ? `<p class="job-error">${job.error}</p>` : ""}
      </div>
      <div class="job-actions">
        <button type="button" data-action="reuse">Reuse URL</button>
        <button type="button" data-action="open">${actionLabel}</button>
      </div>
    `;

    item.querySelector('[data-action="reuse"]').addEventListener("click", () => {
      const params = new URLSearchParams({
        url: job.request?.youtube_url || "",
        mode: job.request?.clean === false ? "fast" : "formatted",
      });
      window.location.href = `/app?${params.toString()}`;
    });

    item.querySelector('[data-action="open"]').addEventListener("click", () => {
      const params = new URLSearchParams({
        job_id: job.job_id,
      });
      window.location.href = `/app?${params.toString()}`;
    });

    jobsList.appendChild(item);
  }
}

async function loadJobs() {
  try {
    const data = await requestJson("/jobs");
    allJobs = [...(data.jobs || [])].sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
    renderJobs();
  } catch (error) {
    jobsList.innerHTML = `<p class="message">${error.message}</p>`;
  }
}

refreshJobsButton.addEventListener("click", loadJobs);
jobSearch.addEventListener("input", renderJobs);
statusFilter.addEventListener("change", renderJobs);
loadJobs();
