# Skald Design System

This documents what's actually implemented in `src/skald/static/style.css` and
the templates under `src/skald/templates/`. It's a reference for staying
consistent, not an aspirational spec — if you add a new pattern, add it here
too.

Aesthetic: dark, information-dense, Sonarr/Radarr/Jellyfin-adjacent. Amber
accent on near-black, monospace for anything structural/labeled, sans-serif
for prose.

## Tokens (`:root` in style.css)

### Color

| Variable | Value | Use |
|---|---|---|
| `--bg` | `#0a0c12` | Page background (near-black) |
| `--bg-mesh-1` / `--bg-mesh-2` | `#14192b` / `#1a1210` | Radial gradient "mesh" blobs behind the page (see `body` background-image) — subtle atmosphere, not a solid fill |
| `--surface` | `#12151e` | Panel background |
| `--surface-2` | `#171b27` | Raised/hover surface (inputs, row hover, active nav) |
| `--border` | `#232838` | Standard border |
| `--border-soft` | `#1b1f2c` | Subtler border (topbar bottom edge, grid gutters) |
| `--text` | `#e7e9f2` | Primary text |
| `--text-muted` | `#8b93a7` | Secondary text (sub-headings, muted table cells) |
| `--text-faint` | `#565d72` | Tertiary text (labels, IDs, empty-state glyphs) |
| `--accent` / `--accent-strong` | `#e8a33d` / `#f3b661` | Brand amber. `-strong` is the lighter variant used for hover/active text; base is used for borders/gradients/focus rings |
| `--accent-soft` | `#e8a33d22` | Amber wash (active nav background, focus ring shadow) |
| `--blue`, `--teal`, `--green`, `--amber`, `--red`, `--gray` (+ `-soft` pairs) | — | Status/semantic colors. Each has a `-soft` (≈12% alpha) variant for badge backgrounds |

Status color mapping (see Badges below): gray = queued, blue = downloading,
teal = completed, amber = organizing/needs_attention, green = organized,
red = failed.

### Typography

Two font stacks, used deliberately for hierarchy — not interchangeably:

- **`--font-display`** — `ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Menlo, Consolas, monospace`.
  Used for anything that reads as *structural/system chrome*: the brand
  mark, nav links, tab links, `h1`, table header cells, badges, job IDs,
  release titles, form field labels (`.login-label`, `.retry-form label`),
  progress percentage, empty-state glyph, error-page glyph. If it's a label,
  a status, an ID, or a heading, it's monospace.
- **`--font-body`** — system sans-serif stack (`-apple-system, "Segoe UI", "Helvetica Neue", ui-sans-serif, system-ui, sans-serif`).
  Used for `body` and all prose/data values: paragraph copy, table body
  cells, form input text, page subtitles (`.page-sub`).

Rule of thumb: **labels and chrome are mono; content and prose are sans.**

Sizes in use (no formal type scale, but consistent by role):
- `h1`: 22px / 700 weight
- Nav / tab links: 13px, uppercase, `0.03em` tracking
- Table headers: 11px, uppercase, `0.08em` tracking
- Table body: 14px
- Badges: 11px, uppercase, `0.05em` tracking
- Body default: 15px, line-height 1.5

### Spacing

No formal spacing-scale variable exists (all literal px in the stylesheet).
The values in practical use cluster around: `4, 6, 8, 10, 12, 14, 16, 18, 20,
22, 24, 28, 32`. Conventions:
- Component internal padding: `14px 18px` (detail fields), `12px 16px` /
  `14px 16px` (table cells), `16px` (search form panel)
- Section spacing: `20px`–`24px` between major blocks (`.page-head`,
  `.detail-grid`, `.tab-bar` margins)
- Tight control spacing: `4px`–`10px` (form control gaps, badge dot gap)

When adding new components, reuse these values rather than inventing new
ones.

### Radius

- `--radius`: `10px` — panels, detail-grid container
- `--radius-sm`: `6px` — buttons, inputs, nav links, badges use `999px`
  (pill) instead

### Shadows

Only one shadow pattern in use, on `.panel`:
```css
box-shadow: 0 1px 0 0 #ffffff05 inset, 0 12px 28px -18px #000000aa;
```
A faint inset top highlight (glass-edge effect) plus a soft downward drop
shadow. Reuse this exact combination for any new elevated surface — don't
invent a second shadow language.

## Layout shell

- `.shell` — max-width 1180px, centered, `0 28px 64px` padding. Everything
  lives inside this.
- `header.topbar` — sticky, blurred/translucent background
  (`backdrop-filter: blur(10px)`), bottom border. Contains `.topbar-inner`
  (same 1180px max-width) with brand, `nav.main-nav`, a `.topbar-spacer` to
  push the logout link right.
- `main` — plays a one-shot `rise` keyframe animation (fade + translateY(8px)
  → 0) on every page load. This is the one intentional page-level animation;
  don't add competing entrance animations elsewhere.

## Core components

### `.panel`
The base "card" surface: `--surface` background, `--border` border,
`--radius`, the standard shadow above. Used for the search form, tables
(`.panel.table-wrap`), empty states, the login card, retry panel, and error
pages. Default surface for anything that needs to visually separate from the
page background.

### Buttons
Base `button` / `.btn` styles apply to all buttons and button-styled links
(e.g. `.error-page a.btn`):
- Mono font, uppercase, `0.03em` tracking, bold
- Default: amber gradient (`--accent-strong` → `--accent`) with dark text
  (`#1a1206`) — this is the primary/default action style
- Hover: `filter: brightness(1.08)`; active: `translateY(1px)` press effect

Variants (add the modifier class alongside the base element):
- **`.btn-quiet`** — secondary/neutral action. `--surface-2` background,
  `--border` border, `--text` colored text. Use for anything that isn't the
  primary action but shouldn't look alarming.
- **`.btn-danger`** — destructive action (used on every "Delete" button).
  Red gradient (`#ff8b96` → `--red`), dark red text (`#2a0b10`).

Only introduce a new button variant if it represents a genuinely different
action class (primary / quiet / danger) — don't create one-off button
colors.

### Badges (status pills)
`.badge` is the base pill (mono, 11px, uppercase, pill radius, includes a
`.badge-dot` — a small circle using `currentColor`). Status color is applied
via a `badge-{status}` modifier matching `MediaJob.status.value`:

| Class | Color | Status |
|---|---|---|
| `.badge-queued` | gray | queued |
| `.badge-downloading` | blue | downloading |
| `.badge-completed` | teal | completed (download finished, not yet organized) |
| `.badge-organizing` | amber | organizing (dot pulses via `@keyframes pulse`) |
| `.badge-organized` | green | organized (terminal success) |
| `.badge-needs_attention` | amber | needs_attention (terminal, actionable) |
| `.badge-failed` | red | failed (terminal) |

Adding a new job status requires adding both a `.badge-{status}` rule here
and confirming it belongs in `ACTIVE_TAB_STATUSES` or
`COMPLETED_TAB_STATUSES` in `routes/jobs.py`.

### Tables
Plain `table` inside a `.panel.table-wrap` (the wrapper adds
`overflow-x: auto` so wide tables scroll horizontally on narrow screens
instead of breaking layout). Header cells are mono/uppercase/faint; body
cells are sans, 14px, row-hover highlights via `--surface-2`. Use
`.cell-muted` for secondary column content (indexer, type, size) and
`.release-title` for filename/release-style strings that should read as
mono.

### Progress bar
`.progress-cell` (flex row) → `.progress-track` (the pill-shaped groove) →
`.progress-fill` (blue→teal gradient, animated `width` transition) +
`.progress-pct` (mono percentage label).

**Progress only means anything for `status == "downloading"`.** Every place
that renders a progress bar for a job row must gate on that status and
render a plain `—` (`.cell-muted`) otherwise — queued (0% is meaningless),
completed/organizing/organized/needs_attention/failed (already resolved) all
render a dash, not a stale/misleading bar. See `job_detail.html` and the
completed-tab table in `jobs.html` for the reference pattern. The active-tab
table's live-updating row (driven by `active_jobs.js` /
`data-job-progress-fill`) always shows a bar because every status in that
tab is part of the in-flight pipeline.

The one exception to "no inline styles" is `.progress-fill`'s
`style="width: X%"` — that's a runtime-computed value, not a design
inconsistency, and is fine to keep inline.

### Detail grid (`job_detail.html`)
`.detail-grid` is a **flexbox** (not CSS grid) row-wrapping layout:
`display: flex; flex-wrap: wrap;` with each `.detail-field` set to
`flex: 1 1 160px`. This is deliberate — a `grid-template-columns:
repeat(auto-fit, minmax(...))` approach computes a fixed column-track count
for the *whole* grid, and full-width fields (see below) that span every
track prevent auto-fit from collapsing unused trailing tracks, leaving
visibly empty cells at the end of partial rows. Flexbox distributes leftover
space per-line instead, so a short last row just stretches evenly with no
dead cells, at any field count or viewport width.

Fields that should span the full row (Release, Content Path) use the
**`.detail-field--wide`** modifier (`flex: 1 1 100%`) — never an inline
`style="grid-column: ..."` or similar. Keep all layout rules in CSS classes;
inline styles are reserved for genuinely runtime-computed values (see
progress bar above).

### Empty state
`.panel.empty-state` — centered text block with a mono `.glyph` (currently an
em-dash) above a muted sentence. Used for "no active jobs" / "no completed
jobs" / "no search results". Keep the glyph + one line of copy pattern for
any new empty state; don't add extra decoration.

### Alerts
`.alert` is the base (padding, radius, 13.5px). `.alert-error` is the only
variant currently implemented (red wash, `--red-soft` background). Used for
login errors, search errors, and job `error_message`.

### Error pages (`templates/error.html`)
A dedicated template (extends `base.html`, so it gets the full topbar/nav
chrome like every other page) for the two exception paths in the app: the
`/grab` qBittorrent failure and the `_error_page()` helper used by
`/jobs/{id}/delete` failure branches. Wrapped in `.panel.error-page`
(centered, max-width 560px, generous padding). Context vars: `title`,
`detail` (rendered in a mono `<code>` chip), optional `hint`, `back_url`,
`back_label`. Never hand-author a raw HTML string with its own
`<head>`/stylesheet link for an error response — route through this
template so error states stay inside the app's chrome.

### Forms
Text/number/password inputs and `select` share one rule block: sans font,
`--surface-2` background, `--border` border, `--radius-sm`. Focus state is
an amber border + `0 0 0 3px var(--accent-soft)` glow ring — reuse this
exact focus treatment for any new input type. The grab mini-form
(`.grab-form`, in the search results table) has its own tighter padding and
fixed per-field widths (title/year/season/episode) since it lives inside a
table cell; the retry form (`.retry-form`) uses labeled stacked
label+input pairs (mono uppercase label above a full input).

### Tab bar
`.tab-bar` is a bottom-bordered flex row of `.tab-link`s (mono, uppercase,
2px bottom border on active, amber when active) each optionally followed by
a `.tab-count` pill (faint by default, amber-tinted when its parent tab is
active). Used for the Jobs Active/Completed switch — reuse this pattern for
any future top-level filter/segmented-control UI rather than inventing a
new tab style.

## Focus & interaction states

Every focusable element gets a consistent focus-visible ring:
```css
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
```
Inputs additionally get the amber glow ring described above on plain
`:focus` (not just `:focus-visible`) since they're expected to show state
on any focus method. Buttons/links rely on the global `:focus-visible` rule
plus their existing hover treatments (brightness bump for primary buttons,
background/color shift for nav/tab/quiet links).

## Responsive behavior

One breakpoint: `@media (max-width: 640px)`. At that width:
- `.shell` and `.topbar-inner` drop side padding from 28px to 16px
- `.search-form` wraps its controls instead of forcing a single row

Tables rely on `.table-wrap`'s horizontal scroll rather than reflowing
columns. `.detail-grid`'s flexbox wrapping and `.grab-form`'s flex-wrap
handle narrow widths without needing their own breakpoint — verify this
still holds before adding new fixed-width elements to either.
