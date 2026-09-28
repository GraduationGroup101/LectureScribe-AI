// Run with: playwright-cli run-code --filename tests/frontend_ui.js
async (page) => {
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const origin = "http://127.0.0.1:8000";
  const completedId = "frontend-completed";
  const now = Date.now() / 1000;
  const completedJob = {
    job_id: completedId, status: "completed", stage: "completed",
    started_at: now - 360, finished_at: now - 240,
    request: { clean: false, youtube_url: "https://www.youtube.com/watch?v=LiXm1wvK7Tk" },
    result: { raw_transcript_path: "OutputForWhisper/LiXm1wvK7Tk_Ch3_Part3_transcript.txt",
      cleaned_transcript_path: "OutputForOllama/LiXm1wvK7Tk_Ch3_Part3_transcript_cleanedv5.txt",
      cleaner_provider: "openrouter" },
  };
  const fixtureText = ("Lecture introduction.\n\nEvery sentence is retained in the transcript.\n\n").repeat(15);
  await page.route(`**/jobs/${completedId}`, route => route.fulfill({ json: completedJob }));
  await page.route(`**/jobs/${completedId}/transcript?*`, route => route.fulfill({
    contentType: "text/plain; charset=utf-8", body: fixtureText,
  }));
  await page.goto(`${origin}/app?job_id=${completedId}`);
  await page.locator("#download-transcript").waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.querySelector("#download-transcript").disabled);
  const realText = await page.locator("#transcript-output").textContent();

  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const layout = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth > window.innerWidth,
      textTop: document.querySelector("#transcript-output").getBoundingClientRect().top,
      detailsOpen: document.querySelector("#processing-details").open,
      resultFirst: [...document.querySelector("#main").children]
        .find(element => getComputedStyle(element).display !== "none")?.id === "result-panel",
      bandHidden: getComputedStyle(document.querySelector("#status-band")).display === "none",
    }));
    assert(!layout.overflow, `Horizontal overflow at ${width}px`);
    assert(layout.textTop < 500, `Transcript below first viewport at ${width}px`);
    assert(layout.resultFirst && !layout.detailsOpen && layout.bandHidden, "Completed layout is not compact");
    await page.screenshot({ path: `output/playwright/result-${width}.png` });
  }

  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("button", { name: "Copy text", exact: true }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  assert(copied.replace(/\r\n/g, "\n") === realText.replace(/\r\n/g, "\n"), "Copy changed the transcript text");
  await page.evaluate(() => {
    const original = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (blob) => {
      window.downloadedText = blob.text();
      return original(blob);
    };
  });
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download", exact: true }).click();
  const download = await downloadEvent;
  assert(download.suggestedFilename() === "Ch3 Part3_transcript.txt", "Unexpected download filename");
  assert(await page.evaluate(() => window.downloadedText) === realText, "Download changed the transcript text");

  await page.getByText("Processing details", { exact: true }).click();
  assert(await page.locator("#processing-details").getAttribute("open") !== null, "Details cannot be expanded");
  assert(await page.locator("#step-count").textContent() === "6 of 6", "Completed step count mismatch");

  let job = {
    job_id: "frontend-check", status: "running", stage: "transcribing",
    started_at: now - 300, stage_started_at: now - 20, estimated_stage_seconds: 600,
    current_step: 3, total_steps: 4, progress_percent: 45, chunk_index: 1, chunk_total: 3,
    request: { clean: false, youtube_url: "https://www.youtube.com/watch?v=LiXm1wvK7Tk&list=example&index=7" },
  };
  await page.route("**/jobs/frontend-check", route => route.fulfill({ json: job }));
  await page.route("**/jobs/frontend-check/transcript?*", route => route.fulfill({
    contentType: "text/plain; charset=utf-8", body: "Fixture transcript. Exact words retained.",
  }));
  await page.goto(`${origin}/app?job_id=frontend-check`);
  await page.waitForFunction(() => document.querySelector("#status-panel").dataset.status === "running");
  assert(await page.locator("#step-count").textContent() === "4 of 6", "Running step count mismatch");
  assert(await page.locator("#stage-rail .stop").count() === 6, "Rail does not match step total");
  assert(await page.locator("#chunk-status").textContent() === "Transcribing part 1 of 3.", "Audio chunks mislabeled");
  assert(await page.locator("#processing-details").getAttribute("open") !== null, "Running details should be visible");
  assert(await page.locator("#result-panel").isHidden(), "Result shown before completion");
  assert(await page.locator("#submit-button").isDisabled(), "Running job resume did not lock submit");
  await page.screenshot({ path: "output/playwright/running-mobile.png" });

  job = { ...job, status: "completed", stage: "completed", finished_at: job.started_at + 120,
    result: { raw_transcript_path: "OutputForWhisper/LiXm1wvK7Tk_Ch3_Part3_transcript.txt",
      cleaned_transcript_path: "OutputForOllama/LiXm1wvK7Tk_Ch3_Part3_transcript_cleanedv5.txt",
      cleaner_provider: "openrouter", used_cached_cleaned_transcript: true } };
  await page.reload();
  await page.waitForFunction(() => !document.querySelector("#copy-transcript").disabled);
  assert(await page.locator("#result-duration").textContent() === "Saved transcript reused", "Cache label is technical");
  assert(await page.locator("#elapsed").textContent() === "2m", "Finished elapsed time uses page-open time");
  assert(await page.locator('#stage-rail [data-state="dropped"]').count() === 3, "Clean cache hit should skip three stages");
  assert(await page.locator("#job-source").getAttribute("href") === "https://www.youtube.com/watch?v=LiXm1wvK7Tk", "Playlist parameters were not removed");
  assert(await page.locator("#result-summary").textContent() === "Formatted transcript", "Provider shown in result title");

  job = { ...job, result: { raw_transcript_path: job.result.raw_transcript_path,
    used_cached_raw_transcript: true, cleaner_error: "HTTP 401" } };
  await page.reload();
  await page.waitForFunction(() => !document.querySelector("#copy-transcript").disabled);
  assert(await page.locator("#result-summary").textContent() === "Original transcript", "Raw output mislabeled");
  assert((await page.locator("#result-notice").textContent()).includes("original transcript"), "Raw fallback explanation missing");
  assert(await page.locator('#stage-rail [data-state="failed"]').count() === 1, "Unavailable formatting was marked as completed");

  await page.route("**/jobs", route => route.request().method() === "POST"
    ? route.fulfill({ json: { job_id: "frontend-check" } }) : route.fallback());
  job = { ...job, status: "running", stage: "transcribing", finished_at: null, result: null };
  await page.getByRole("textbox", { name: "Lecture URL", exact: true }).fill(job.request.youtube_url);
  await page.locator("#submit-button").click();
  await page.waitForFunction(() => document.querySelector("#status-panel").dataset.status === "running");
  assert(await page.locator("#submit-button").isDisabled(), "New job did not lock submit");
  assert(await page.evaluate(() => [...document.querySelector("#main").children]
    .find(element => getComputedStyle(element).display !== "none")?.id === "status-panel"), "Starting again did not restore status-first layout");
  assert(page.url().includes("job_id=frontend-check"), "New job cannot be resumed on reload");

  job = { ...job, status: "failed", stage: "failed", finished_at: now,
    error: "Fixture: transcription could not finish." };
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#status-panel").dataset.status === "failed");
  assert(await page.locator("#result-panel").isHidden(), "Failed job shows a completed result");
  assert((await page.locator("#job-message").textContent()).includes("Fixture:"), "Failure message hidden");
  await page.screenshot({ path: "output/playwright/failed-mobile.png" });
  await page.unroute("**/jobs");
  await page.unroute("**/jobs/frontend-check");
  await page.unroute("**/jobs/frontend-check/transcript?*");
  await page.unroute(`**/jobs/${completedId}`);
  await page.unroute(`**/jobs/${completedId}/transcript?*`);
  await page.goto(`${origin}/app`);
  return "Frontend checks passed: desktop/mobile, copy/download, running, completed, cache, raw fallback, retry, and failure.";
}
