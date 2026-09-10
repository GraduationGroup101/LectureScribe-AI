---
name: LectureScribe AI
description: A dark airport-terminal concourse where signage yellow is spent only on telling you where you are.
colors:
  concourse: "#101215"
  panel: "#16191d"
  panel-2: "#1c2026"
  inset: "#0b0b0b"
  rule: "#262b31"
  rule-strong: "#3a4149"
  sign: "#ffcc00"
  sign-dim: "#4a3f0f"
  sign-ink: "#0b0b0b"
  ink: "#f2f3f5"
  ink-2: "#a8b0b8"
  ink-3: "#838d96"
  arrival: "#0f7a43"
  arrival-lit: "#35c07a"
  closed: "#c0261f"
  closed-lit: "#ff6a5e"
  sheet: "#ffffff"
  sheet-ink: "#14161a"
typography:
  display:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "clamp(32px, 4.6vw, 52px)"
    fontWeight: 700
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  figure:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "clamp(30px, 4.4vw, 52px)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.03em"
    fontFeature: "tabular-nums"
  headline:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "clamp(21px, 2.4vw, 27px)"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "26px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  lede:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  body:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
    fontFeature: "tabular-nums"
  label:
    fontFamily: "Fira Sans, ui-sans-serif, system-ui, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.5
    letterSpacing: "0.1em"
  mono:
    fontFamily: "Fira Mono, ui-monospace, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "0.02em"
    fontFeature: "tabular-nums"
  sheet:
    fontFamily: "Fira Sans, Noto Naskh Arabic, ui-sans-serif, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.75
    letterSpacing: "normal"
rounded:
  none: "0"
  sm: "2px"
spacing:
  hairline: "1px"
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "18px"
  2xl: "28px"
  3xl: "56px"
components:
  go:
    backgroundColor: "{colors.sign}"
    textColor: "{colors.sign-ink}"
    typography: "{typography.headline}"
    rounded: "{rounded.sm}"
    padding: "0 24px 0 8px"
    height: "56px"
  go-hover:
    backgroundColor: "#ffd633"
  go-active:
    backgroundColor: "#e6b800"
  go-disabled:
    backgroundColor: "#232a31"
    textColor: "{colors.ink-2}"
  action:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 15px"
    height: "42px"
  action-hover:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.ink}"
  input-field:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.ink}"
    typography: "{typography.lede}"
    rounded: "{rounded.sm}"
    padding: "14px 16px"
    height: "56px"
  mode-panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "16px"
  mode-panel-selected:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.ink}"
  band-wayfinding:
    backgroundColor: "{colors.sign}"
    textColor: "{colors.sign-ink}"
    padding: "22px 28px"
  band-arrival:
    backgroundColor: "{colors.arrival}"
    textColor: "#ffffff"
    padding: "22px 28px"
  band-closed:
    backgroundColor: "{colors.closed}"
    textColor: "#ffffff"
    padding: "22px 28px"
  band-quiet:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    padding: "22px 28px"
  tile:
    backgroundColor: "{colors.inset}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "6px"
    size: "34px"
  stop:
    backgroundColor: "transparent"
    textColor: "{colors.ink-3}"
    padding: "14px 16px"
  stop-current:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.ink}"
  sheet:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.sheet-ink}"
    typography: "{typography.sheet}"
    rounded: "{rounded.sm}"
    padding: "clamp(24px, 3.4vw, 44px)"
    width: "min(100%, 86ch)"
  board-row:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "18px 0"
  board-row-hover:
    backgroundColor: "{colors.panel}"
---

# Design System: LectureScribe AI

## Overview

**Creative North Star: "The Concourse"**

LectureScribe is a long wait with a machine at the end of it, and the world it lives in is an international airport terminal after dark. The ground is matte and unlit; the only thing that glows is the sign telling you where you are. A student pastes a link and leaves the tab, and when they glance back from across the desk, one lit band across the full width of the screen answers all three questions at once — which stage, how far along, how much longer — before they have read a sentence.

Everything here is architecture rather than ornament. Structural regions are full-bleed horizontal bands that butt against each other and are separated by a single 1px rule; there are no cards, no shadows, no floating containers, no rounded panels. Depth comes from four ground values (near-black inset, concourse, panel, panel-lift) and hairlines, never from lift. Signage yellow is spent on exactly one job — wayfinding — and appears at most once per view: the current stage, the primary action, the progress fill, the current destination in the sign band, the focus edge. When yellow appears somewhere it is not directing you, the world has been broken.

The world is deliberately not the category default it replaced: a centred stack of white rounded cards on light gray, each with an eyebrow label, a pill badge, and a gradient progress bar. Pill badges, gradient bars, drop shadows, and eyebrow labels are the confirmed anti-reference. There is exactly one exception to the dark ground, and it is a reward rather than a surface type: when a job arrives, the concourse hands you one white sheet with the transcript on it.

**Key Characteristics:**
- Dark matte ground (#101215) with a single saturated yellow (#ffcc00) reserved absolutely for wayfinding
- Full-bleed horizontal bands separated by 1px rules; no cards, no elevation, effectively no radius (2px maximum)
- Authored solid-silhouette pictograms on black inset tiles, one construction across the whole set
- Fira Sans in sentence case, Fira Mono for every machine value, Noto Naskh Arabic for Arabic transcript text, tabular numerals everywhere
- Every state readable from shape and label as well as hue; every figure carries its unit
- One white sheet, only as the arrival of a finished transcript

## Colors

A near-black terminal ground carrying one saturated signage yellow, with a deep/lit pair for each of the two outcome states so the same state can be read as a band ground or as a mark on the dark.

### Primary
- **Signage Yellow** (`{colors.sign}`): Wayfinding only. The running band, the current stop's tile, the progress fill, the Start action, the underline and label of the current destination, the focus ring, the caret, the text-selection ground, and the state mark on running or queued rows. Nothing else, ever.
- **Sign Ink** (`{colors.sign-ink}`): The near-black that type and pictogram tiles take when they sit on yellow. Also the ground of the hung sign band itself.
- **Sign Shade** (`{colors.sign-dim}`): The darkened yellow used for small labels printed on a yellow band, where full sign ink would be too loud and white would fail contrast.

### Secondary
- **Arrival Green, deep** (`{colors.arrival}`): The ground of the arrival band when a job lands. Carries white type.
- **Arrival Green, lit** (`{colors.arrival-lit}`): The same state read against the dark concourse — the completed progress fill, passed stops, the ARRIVED mark on a history row.
- **Closed Red, deep** (`{colors.closed}`): The ground of the closed-route band when a job fails. Carries white type.
- **Closed Red, lit** (`{colors.closed-lit}`): The same state on the dark — the failed progress fill, the failed stop's tile, the cancelled stamp, error copy.

### Neutral
- **Concourse** (`{colors.concourse}`): The page ground. Everything is hung on it.
- **Panel** (`{colors.panel}`): The quiet band ground and the resting ground of mode panels and hovered history rows.
- **Panel Lift** (`{colors.panel-2}`): One step up from panel; marks the current stop on the rail, the selected mode, and hovered secondary actions. This is as "raised" as the system gets.
- **Inset Black** (`{colors.inset}`): Cut into the ground rather than laid on it — the hung sign band, pictogram tiles, the URL field, the progress trough.
- **Rule** (`{colors.rule}`): The 1px hairline that separates every region, row, stop, and field. The most-used value in the system.
- **Rule Strong** (`{colors.rule-strong}`): The visible edge of an interactive control (input border, secondary action border) and the scrollbar thumb.
- **Ink** (`{colors.ink}`): Primary type on the dark.
- **Ink Two** (`{colors.ink-2}`): Secondary type — labels, supporting copy, passed-stop names.
- **Ink Three** (`{colors.ink-3}`): Tertiary type — placeholders, unlit stop names and their durations, counts.
- **Sheet White** (`{colors.sheet}`) and **Sheet Ink** (`{colors.sheet-ink}`): The transcript sheet and its type. The only white surface in the terminal.

### Named Rules

**The Wayfinding-Only Rule.** Yellow directs; it never decorates. It marks where you are (current stop, current destination), where to go next (the one primary action), how far you have got (the progress fill), and where your keyboard is (focus, caret, selection). If a yellow element is not answering one of those, remove the yellow.

**The One Sign Rule.** At most one yellow band is lit per view. The question band and the running-stage band are the same slot: when a job starts, the ask band demotes to a quiet panel strip and hands its yellow to the stage band.

**The Deep-and-Lit Rule.** Every state colour exists twice: a deep value that is a band ground under white type, and a lit value that is a mark on the dark ground. Never use the deep value as a mark, or the lit value as a large ground.

**The Never-Hue-Alone Rule.** State is carried by shape and word as well as colour. Passed stops read "done", the current stop reads "now", dropped stops are struck through, running and queued rows take a triangular mark while failed rows take a rotated square, and a failed job takes a bordered CANCELLED stamp.

## Typography

**Display / Body Font:** Fira Sans (400, 500, 600, 700), with ui-sans-serif, system-ui, Segoe UI fallback
**Mono Font:** Fira Mono (400, 500, 700), with ui-monospace, Consolas fallback
**Arabic Font:** Noto Naskh Arabic (400, 500, 600), stacked after Fira Sans on the transcript sheet only

**Character:** Fira Sans is a signage grotesque — even colour, open apertures, legible at a glance from across a desk — set in sentence case at heavy weights with tight negative tracking so a headline reads as a destination name rather than a paragraph. Fira Mono carries anything the machine produced: job IDs, counts, durations, timestamps, stage notes. Numerals are tabular across the whole page so a live countdown never shifts its neighbours.

### Hierarchy
- **Display** (700, `clamp(32px, 4.6vw, 52px)`, 1.02, -0.035em): The question hung on the ask band — "Which lecture?". One per page. Demotes to 26px when a job takes over the page.
- **Figure** (700, `clamp(30px, 4.4vw, 52px)`, 1, -0.03em, tabular): The number on the right of a band — minutes remaining, stops on the route, lectures on the board. Never wraps.
- **Headline** (600, `clamp(21px, 2.4vw, 27px)`, 1.2, -0.015em): The one message a band carries — the stage sentence, the result summary. One line, one message.
- **Title** (700, 26px, -0.02em): Empty-state and error headings inside the board.
- **Lede** (400, 17px, 1.5, max 52ch): The single explanatory paragraph under the question, and input text.
- **Body** (400, 16px, 1.5): Default copy; supporting notes run 14–15px at max 68ch.
- **Label** (700, 12px, 0.1em, uppercase): The name of a figure or a field value. See the rule below.
- **Mono** (400, 11–15px, 0.02em, tabular): Job IDs, stop counts, elapsed and estimated durations, per-stop typical times, history row metadata, board counts.
- **Sheet** (400, 17px, 1.75, `white-space: pre-wrap`, `dir="auto"`): Transcript text on the white sheet, 86ch measure, right-aligned when the direction resolves to RTL.

### Named Rules

**The Field-Label Rule.** The 12px uppercase label exists only to name a figure or a field value directly beneath or above it: REMAINING over the countdown, TOOK and STOPPED AFTER over a final duration, THE ROUTE over "6 stops", ON THE BOARD over the job count, STOP / ELAPSED / ARCHIVE / MODE over their monospace readouts, LECTURE and JOB over the identifiers, LECTURE URL and OUTPUT over their controls, EVERY STOP over the list of stops it heads. It is a board field name, never an introduction. A small uppercase label above a heading is an eyebrow, and this world has none — the build removed all five it once carried. If a label is not naming the value it is bound to, delete it.

**The One Message Rule.** A band carries one sentence and at most one figure. Nothing is stacked inside a band; if a second message is needed, it needs its own band or it belongs in the supporting copy below.

**The Machine Voice Rule.** Anything the server produced or measured is set in Fira Mono: IDs, counts, durations, timestamps. Anything written for a person is Fira Sans in sentence case. The switch of face is how you tell a fact from a sentence.

## Layout

The page is a stack of full-bleed horizontal bands, each separated from the next by a single 1px rule, with no vertical gaps between them. Content inside a band is held to a corridor of `min(1240px, 100%)` with 28px inline padding (18px below 760px); bands themselves pad with `max(28px, calc((100% - 1240px) / 2 + 28px))` so their grounds run edge to edge while their type stays on the corridor.

A 56px module runs through the whole surface: the hung sign band is 56px tall, every primary control (URL field, select, Start, the filter row's actions) is 56px tall, and the two-column corridor gap is 56px. The spacing rhythm is optically set rather than strictly stepped, but the recurring steps are 6, 8, 12, 16, 18, 28 and 56px, over a 1px structural hairline and a 2px corner.

Two-column regions — the decision body and the job status foot — run `minmax(0, 1.15fr) / minmax(300px, 0.85fr)`, with the action column on the left and the rail or identity fields on the right. The stage rail is a `grid-auto-flow: column` chain of equal `minmax(0, 1fr)` tracks that scales its stops down instead of scrolling or collapsing.

**Responsive behaviour.** At ≤1000px both two-column regions become one column and the facts grid drops from four columns to two. At ≤760px the corridor tightens to 18px, bands stack their message above their figure (the figure indenting 50px to stay aligned under the pictogram), the rail becomes two rows of three stops rather than shedding any, history rows drop the job ID column and move their actions to a full-width row, and the sheet loses its 64vh cap and scrolls with the page. At ≤620px the sign band splits into two stacked rows, the mark above a full-width pair of destinations. Below 760px, and under `prefers-reduced-motion`, continuous motion is replaced by discrete state change. Under `prefers-contrast: more` the two secondary inks and the hairline all step lighter.

**Named Rules**

**The Butt-Joint Rule.** Regions meet on a 1px rule with no gutter, no margin, and no corner. If two regions need separating, they need a rule, not space.

**The Chain-Scales Rule.** The route never sheds a stop to fit. It compresses its tracks, and only below 760px does it fold from one row of six into two rows of three. A stop that does not apply is struck through in place; it is never removed.

## Elevation & Depth

The system is flat by construction. There are no elevation shadows anywhere, no blur, no glow, and no scrim. Depth is entirely tonal and linear: four ground values stacked from cut-in to raised (`inset` #0b0b0b for things cut into the page, `concourse` #101215 for the page itself, `panel` #16191d for a quiet band, `panel-2` #1c2026 for the one element that is currently active), separated by 1px `rule` hairlines and, at the edge of an interactive control, a `rule-strong` hairline. A pictogram tile reads as recessed because it is darker than its ground, not because anything floats above it.

### Shadow Vocabulary

The single `box-shadow` in the build is not elevation. A hovered history row uses `box-shadow: 28px 0 0 var(--panel), -28px 0 0 var(--panel)` (18px at ≤760px) purely to extend its hover fill past the corridor padding so the row highlight runs to the corridor edge. It is a horizontal fill extension with zero blur and zero vertical offset, and it is the only permitted use of the property.

### Named Rules

**The No-Lift Rule.** Nothing in this world casts a shadow. Depth is a change of ground value plus a hairline. If a surface needs to read as separate, darken or lighten its ground one step and rule it off; do not lift it.

## Shapes

Corners are effectively absent. The only radius in the system is 2px, and it is used identically everywhere it appears: pictogram tiles, stop tiles, the URL field, the select, buttons, the mode list, and the transcript sheet. Every other edge is a true 90° corner — bands, rows, rules, stamps, the progress trough, the fill. There is no pill, no capsule, and no circle anywhere except the native radio dot.

The recurring geometry is the horizontal band and the hairline: a full-width ground, one rule beneath it, the next ground. Inside a band the constant unit is the tile — a square black chip (34px in bands and history rows, 30px on the rail, 28px in the mark, 40px inside the Start action) holding a white solid-silhouette pictogram with 3–9px of padding.

Pictograms are authored as a single inline SVG sprite defined at the top of each HTML file: 16 symbols on `index.html`, 11 on `jobs.html`, all on a 24×24 viewBox, all solid silhouettes filled with `currentColor`, all built from the same rectangular, chamfer-free vocabulary that airport panels use. No icon library, no stroke icons, no glyph or emoji icons.

State marks are geometric rather than pictorial: a running or queued history row takes an 8px triangle (`clip-path: polygon(50% 0, 100% 100%, 0 100%)`), a failed row takes a 7px square rotated 45°, and a completed row takes the plain 8px square. A cancelled job takes a rectangular hairline-bordered stamp in closed red.

## Components

### Buttons
- **Shape:** Square with a 2px corner (`{rounded.sm}`); no border on the primary, a 1px `rule-strong` edge on the secondary.
- **Primary — "the go":** Signage yellow ground with sign-ink type at 18px/700, 56px tall, padded `0 24px 0 8px` because a 40px black pictogram tile sits inside its left edge with a 14px gap to the word. The tile-plus-word construction is what makes it read as a departure sign rather than a button. Hover lightens to #ffd633; active deepens to #e6b800; disabled drops to a #232a31 ground with ink-2 type and an inset-black tile, at full opacity so it stays legible while it waits.
- **Secondary — "the action":** Transparent ground, 1px `rule-strong` border, ink type at 15px/500, 42px tall, 15px inline padding, with a 16px pictogram before the label. Hover lightens the border to ink-2 and fills with panel-lift. On a yellow or coloured band it inverts to `action-on-sign`: a 35%-black border and sign-ink type, hovering to a 9%-black fill.
- **Focus:** Every control takes the global 3px signage-yellow outline at 2px offset. Text fields instead move their border to signage yellow with zero outline offset.

### Cards / Containers
There are none. The unit of composition is the band: a full-bleed ground, 22px block padding, a 1px bottom rule, holding one `band-message` (tile + one headline) on the left and one `band-figure` (12px label + large tabular number) or one action on the right. Four grounds are defined — `band-wayfinding` (yellow, running), `band-arrival` (deep green, landed), `band-closed` (deep red, failed), `band-quiet` (panel, queued or neutral) — plus `ask-band`, the yellow question band, which demotes to a quiet panel strip with its tile and figure hidden once a job is on the machine.

### Inputs / Fields
- **Style:** Inset-black ground, 1px `rule-strong` border, 2px corner, 56px minimum height, 17px type, full width of its track. Placeholders are ink-3 at full opacity. The select drops its native appearance and draws its caret from two 6px linear-gradient triangles.
- **Focus:** Border shifts to signage yellow; the caret is signage yellow.
- **Labels:** 12px uppercase label above the control, 9px clear.
- **Mode panels:** A radio group rendered as stacked full-width panels inside a single 1px `rule-strong` box with 2px corners, each panel 16px padded on a panel ground with a 19px native radio (`accent-color` signage yellow), a 600-weight name, and a 14px ink-2 note. The checked panel lifts to panel-lift and takes a 2px inset signage-yellow outline. In the demoted entry strip the panels sit side by side, drop their notes, and shrink to 56px.

### Navigation
The hung sign band: inset-black, sticky at the top, 56px tall, 1px bottom rule. The mark on the left is a 28px white tile carrying the drawn pictogram plus the wordmark at 17px/700. Destinations on the right are 15px/500 ink-2 links with a 17px pictogram, 16px inline padding, and a 3px transparent bottom border. Hover lifts them to ink on a #141414 ground. The current destination takes signage yellow type and a signage-yellow 3px underline via `aria-current="page"` — the followed destination stays lit band to band. Below 620px the band splits into two rows and the destinations become equal-width, centred, 46px-tall targets.

### The Rail (signature component)
The route rendered as a chain of decision points: an `<ol>` of equal-width stops, each a 30px black tile plus a stop name (14px) and a monospace note (11px), separated by 1px left rules, with the first and last stops flush to the corridor. Four states are drawn: `done` (green-tinted #1b2d23 tile with arrival-lit pictogram, ink-2 name, note reads "done"), `current` (panel-lift ground, signage-yellow tile with sign-ink pictogram, 600-weight ink name, signage-yellow note reading "now"), `ahead` (inset tile with a #6b747c pictogram, ink-3 name, note carrying the stop's typical duration), `dropped` (40%-opacity tile, struck-through ink-3 name, note reads "not needed") and `failed` (red-tinted #331714 tile with closed-lit pictogram, ink name, note reads "stopped here"). Before anything runs, the same markup appears as `rail-invitation` — the stops stacked as rows under an EVERY STOP head and over a foot line explaining the cache — so the empty state is an invitation rather than an absence.

### The Progress Rule
A 6px inset-black trough directly under the band, filled by a `transform: scaleX()` element at 320ms on `cubic-bezier(0.16, 1, 0.3, 1)`. Signage yellow while running, arrival-lit when completed, closed-lit when failed. No radius, no gradient, no stripe, no label of its own — the figure on the band is the label.

### The Sheet
The one white surface: `min(100%, 86ch)` wide, centred, `clamp(24px, 3.4vw, 44px)` padded, 17px/1.75 type on white, 2px corner, `white-space: pre-wrap`, `dir="auto"` so Arabic transcript text takes its own direction and right alignment. Selection inside the sheet still burns signage yellow. Its scrollbar is themed to the sheet in CSS (a #b9bec4 thumb with a 3px white border) rather than inheriting the dark scrollbar — this override is specified but has not yet been seen render, because no captured transcript was long enough to overflow the sheet. Treat it as unverified until a long transcript confirms it. Below 760px it drops its 64vh cap and scrolls with the page.

### The Board Row
A history row: a 34px tile, a truncating 16px/600 source line, a monospace metadata line (state word, mode, archive hit or miss, date, job ID), and a two-button action pair. The row's tile and its uppercase state word take the state colour, and the state word carries a geometric mark before it. Failed rows add a bordered CANCELLED stamp and, when present, the error sentence at 14px ink-2. Hover fills panel and bleeds to the corridor edge. Rows are separated by 1px rules with no radius and no gap.

### Browser Surfaces
The parts the build did not draw still carry the world: selection is signage yellow on sign-ink, the focus ring is a 3px signage-yellow outline at 2px offset, the caret is signage yellow, placeholders are ink-3 at full opacity, and scrollbars are a `rule-strong` thumb with a 3px concourse-coloured border on a transparent track (hovering to #4d565f), thin under `scrollbar-width`. `color-scheme: dark` is declared on the root and in a meta tag so native form and scroll chrome match. Tabular numerals are set on `body` so every figure on the page is tabular by default.

### Motion
One authored moment, and one transition. The `resign` keyframe (260ms, `cubic-bezier(0.16, 1, 0.3, 1)`, opacity 0 → 1 with a -0.42em rise) replays on the band's status word, headline, and figure whenever the sign actually changes what it says — a stage change, an arrival, a failure — with the figure delayed 40ms so the number lands after the name, the way a split-flap panel re-signs itself. Everything else is a 140–320ms `cubic-bezier(0.16, 1, 0.3, 1)` on colour, border, and the progress fill's scale. Below 760px and under `prefers-reduced-motion: reduce`, the resign animation, the progress transition, and the stop-tile transition are all switched off and the changes land discretely.

## Do's and Don'ts

### Do:
- **Do** spend signage yellow (#ffcc00) only on wayfinding — the current stage, the one primary action, the progress fill, the current destination, the focus edge — and at most one lit yellow band per view.
- **Do** compose in full-bleed horizontal bands separated by 1px `--rule` hairlines, with one message and at most one figure per band.
- **Do** put every server-produced value — IDs, counts, durations, timestamps — in Fira Mono, and keep numerals tabular so live figures never shift their neighbours.
- **Do** pair every state colour with a word and a shape: "done" / "now" / "not needed" / "stopped here", a triangle for running, a rotated square for failed, a strike-through for dropped.
- **Do** give every figure its unit (`4 of 6`, `~ 11m 30s`, `6 stops`, `7m 10s`).
- **Do** draw new pictograms as solid silhouettes on a 24×24 viewBox in the existing inline sprite, filled with `currentColor`, and set them on a black inset tile with a 2px corner.
- **Do** keep the transcript sheet the only white surface, with `dir="auto"` and Noto Naskh Arabic in its stack so Arabic renders and aligns correctly inside the LTR page.
- **Do** state a stop's typical duration before anything runs, so the empty rail reads as an invitation rather than an absence.

### Don't:
- **Don't** use yellow decoratively — not for a heading, a hairline, an icon that is not marking the current position, a hover fill, or a second simultaneous band.
- **Don't** introduce a card, a panel with a shadow, or any `box-shadow` used for elevation. The only permitted shadow is the history row's zero-blur horizontal fill bleed.
- **Don't** exceed a 2px corner radius anywhere, and don't introduce a pill, capsule, or circular badge.
- **Don't** put a small uppercase label above a heading. Labels name a figure or a field value; a label above a heading is an eyebrow and this world has none.
- **Don't** use a gradient, a blur, a glow, a scrim, or a stripe. The progress fill is one flat colour on a flat trough.
- **Don't** use an icon library, a stroke icon set, an icon font, or emoji. Every pictogram is authored into the sprite in the one construction.
- **Don't** drop a stop from the route to make it fit, and don't remove a failed job from the board — it keeps its line and takes a CANCELLED stamp.
- **Don't** add continuous motion. `resign` is the only authored animation, it plays only when the sign's message actually changes, and it is off below 760px and under reduced motion.
- **Don't** carry state in hue alone, and don't put white or full sign-ink small labels on a yellow band — that label colour is `--sign-dim` (#4a3f0f).
