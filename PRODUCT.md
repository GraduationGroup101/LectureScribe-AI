# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: university students who recorded or found a lecture on YouTube and need the spoken content as text they can search, review, and quote. They arrive with a URL, paste it, and wait — often at night, on a laptop, sometimes on a phone, usually while doing something else in another tab.

Secondary: the graduation-project committee and supervising faculty, who evaluate the interface as the visible face of the pipeline during a live demo. They judge whether the stages, state, and engineering are legible, not whether they can transcribe their own lecture.

Both audiences share one screen. Nothing may be added for the demo that makes the everyday student path slower.

## Product Purpose

Turn a YouTube lecture into a readable transcript, and keep that result available so the same lecture is never processed twice. Success is a student who pastes a link, leaves the tab, comes back, and copies clean text.

## Positioning

Not a one-shot transcription script. It is a real processing pipeline with observable state: URL validation, a single-slot queue, audio download, Faster-Whisper `large-v3` transcription on CUDA, optional LLM cleanup, per-stage progress and estimates, a persistent video-ID cache, and a durable job history. The mechanism a neighboring tool could not truthfully copy is the visible stage machine plus the cache that makes a repeat request near-instant.

## Operating Context

- One job runs at a time so the GPU and Ollama stay stable; other requests queue.
- Runs are long. Transcription is the dominant stage; a full lecture can take many minutes, so the waiting state is a primary screen, not an edge case.
- The wait is unattended. Users switch tabs and return, so the page must be readable at a glance from across a desk and must survive a reload (`/app?job_id=...` rehydrates a running job).
- Cached lectures return in seconds, which makes the fast path and the slow path visually different experiences of the same screen.
- Transcript output is frequently Arabic (jobs are submitted with `language: "ar"`), rendered inside an English-language interface.

## Capabilities and Constraints

- Stack: plain HTML/CSS/JS under `front/`, served by FastAPI. No build step, no framework, no npm. Self-hosted or CDN webfonts are acceptable.
- Routes that must keep working: `GET /` and `GET /app` (index), `GET /app/jobs` (history), static assets under `/front`, and the API — `POST /jobs`, `GET /jobs`, `GET /jobs/{job_id}`, `GET /jobs/{job_id}/transcript?kind=cleaned|raw`.
- Job stages surfaced to the user: `queued`, `checking_cache`, `cache_hit`, `downloading`, `transcribing`, `formatting`, `saving`. Job statuses: `queued`, `running`, `completed`, `failed`.
- Two request modes: **Fast output** (`clean: false`) and **Better formatting** (`clean: true`, OpenRouter with Ollama fallback).
- Job payload fields the UI reads: `status`, `stage`, `stage_label`, `current_step`, `total_steps`, `progress_percent`, `started_at`, `stage_started_at`, `estimated_stage_seconds`, `chunk_index`, `chunk_total`, `result.cleaned_transcript_path`, `result.cleaner_provider`, `result.used_cached_cleaned_transcript`, `result.used_cached_raw_transcript`, `error`.
- Query params the index honors: `url`, `mode`, `job_id`.
- Interface language is English only. Transcript content may be Arabic and must render correctly (direction, shaping, and a face with real Arabic coverage) inside an otherwise LTR page.
- Failure is ordinary: unavailable videos, download errors, and cleaner outages all surface as a failed job with a message.

## Brand Commitments

Name: **LectureScribe AI**. Public site: lecturescribe.app. No logo, wordmark, or palette has been fixed; nothing in the current CSS is binding.

## Evidence on Hand

Real job history in `jobs.json`, real cached transcripts in `OutputForWhisper/` and `OutputForOllama/`, and a real running API. Sample transcripts are Arabic-language computer-engineering lectures. There are no users, testimonials, benchmarks, pricing, or institutional endorsements — none may be invented.

## Product Principles

1. **State is the product.** During a long run the screen's job is to answer "what is happening, how far along, how much longer" without the user reading a sentence.
2. **The wait is a first-class screen, not a spinner.** It is where users spend most of their time.
3. **Never make them redo work.** The cache, the job history, and URL rehydration all exist so a lecture is processed once.
4. **Failure states get the same care as success states.** A failed job must name the problem and the next move.
5. **Familiar affordances.** This is a tool in a task; standard controls, standard shapes, no invented widgets.

## Accessibility & Inclusion

Status must never be carried by color alone — badges and stage markers need text or shape. Progress and stage changes are announced through an existing `aria-live` region. Contrast meets WCAG AA. The page is used at arm's length while the user does something else, so glanceable type sizes matter more than density.
