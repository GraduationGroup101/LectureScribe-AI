---
version: 1
slug: "front-index-html"
primary_target: "front/index.html"
related_targets: ["front/jobs.html","front/styles.css","front/app.js","front/jobs.js"]
---

Scope: the whole LectureScribe web surface — the new-transcript page (`front/index.html`, routes `/` and `/app`) and the job history page (`front/jobs.html`, route `/app/jobs`), plus `front/styles.css`, `front/app.js`, `front/jobs.js`.

Visitor mode: Operate. The visitor is in a task and mostly in a wait.

Audience and job: university students who paste a YouTube lecture URL and wait, often at night, usually in a background tab; and the graduation committee, who read the same screen as evidence that a real pipeline runs underneath. The task is: submit a link, understand what is happening and how much longer, then take the text.

Action: one field, one mode choice, one Start. Everything else on the surface reports state.

Constraints: no build step, no framework, no npm; plain HTML/CSS/JS served by FastAPI from `/front`. Routes `/`, `/app`, `/app/jobs`, `POST /jobs`, `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/transcript` must keep working, as must the `url`, `mode` and `job_id` query params. Interface copy is English; transcript content is frequently Arabic and must render with correct direction and a face that has real Arabic coverage.

Memorable moment: a cache hit visibly removes the download and transcribe stages from the rail, the way a closed route drops off an airport sign panel.

Unresolved: none blocking. No logo asset exists; the mark is drawn type plus an authored pictogram.

## Direction contract

THESIS: A long-running job is a walk through a terminal — at any moment one lit sign tells you where you are, how far is left, and nothing else. It refuses the category default this surface currently ships: a centred stack of white rounded cards on light gray, each with its own eyebrow label, a pill badge, and a gradient progress bar. Status here is architecture, not a badge sitting inside a card.

OWN-WORLD: A dark matte concourse ground (#101215, panels #16191d, hairline rules #262b31) with saturated signage yellow (#ffcc00) reserved absolutely for wayfinding — the current stage, the primary action, the progress fill, the current destination in the nav band, and nothing else, ever. Pictograms are authored solid-silhouette SVG, white on black inset tiles (#0b0b0b), one construction across the whole set. Arrivals are signage green (#35c07a), closed routes signage red (#ff5a4d). Structural regions are separated by 1px rules and butt against each other edge to edge — no card, no shadow, and no border radius above 2px anywhere except the pictogram tiles. Type is Fira Sans in sentence case, one message per band line, with Fira Mono for job IDs, timecodes, counts, and durations, and Noto Naskh Arabic carrying Arabic transcript text. Numerals are tabular everywhere. The single exception to the dark world is the transcript itself: when a job arrives, the concourse hands you one white sheet.

STORY: The visitor understands within one glance that this is a machine with named stages and a real estimate, not a spinner. They believe the wait is bounded because the sign says how many minutes and which stage. They paste a link, leave the tab, glance back from across the desk, and take the text.

FIRST VIEWPORT: A black sign band hung across the full width, 56px, carrying the drawn mark at left and two destinations at right, the current one underlined in yellow. Below it, on the dark ground, a two-column corridor: left, "Which lecture?" at 64px sentence case, the URL field as a wide black inset panel with a yellow focus edge, two mode panels stacked beneath it, and the primary action as a black-tile arrow beside the word Start on yellow; right, the six-stage rail rendered unlit as a vertical chain with each stage's typical duration, present before anything runs. Once a job starts, the yellow stage band replaces the top of the corridor full-bleed: the stage pictogram and stage name at left, the minutes remaining at 64px hard right, the rail directly beneath it as a horizontal chain that scales rather than wraps. The primary action always sits at the end of the left column, above the fold.

FORM: International airport terminal sign system — the dealt challenger `wayfinding-cartography-signage-terminal-yellow-wayfinding`, which beat my own roll assignment (grounded candidate 4, a film continuity spotting sheet) on both audience identification and product clarity, and which the user locked on the decision page. Seed key 0c970a2c. Raises carried in from declined challengers: nothing disappears, it cancels (failed jobs keep their line and take a stamp); the empty rail is an invitation, not an absence; back retraces the exact trail, so every job state is addressable and the way back is always on screen; every figure carries its unit and every state reads from shape and label, never hue alone; on small screens continuous motion is replaced by discrete state changes.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.
