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
| `--text-muted` | `#9aa2b5` | Secondary text (sub-headings, muted table cells) |
| `--text-faint` | `#9199ad` | Readable tertiary text (labels, IDs); not decorative-only |
| `--text-decorative` | `#565d72` | Decorative glyphs and separators only |
| `--border-control` | `#707a91` | Input, checkbox and secondary-button boundaries |
| `--accent` / `--accent-strong` | `#e8a33d` / `#f3b661` | Brand amber. `-strong` is the lighter variant used for hover/active text; base is used for borders/gradients/focus rings |
| `--accent-soft` | `#e8a33d22` | Amber wash (active nav background, focus ring shadow) |
| `--accent-border` / `--accent-border-strong` | `#e8a33d44` / `#e8a33d66` | Amber tinted borders |
| `--error-text` | `#ffb4bb` | Readable error text on dark/red-wash surfaces |
| `--blue`, `--teal`, `--green`, `--amber`, `--red`, `--gray` (+ `-soft` pairs) | — | Status/semantic colors. Each has a `-soft` (≈12% alpha) variant for badge backgrounds |

Status color mapping (see Badges below): gray = queued, blue = downloading,
teal = completed, amber = organizing/needs_attention, green = organized,
red = failed. Deleting is neutral with a dashed outline; unread is neutral,
not a job error. `color-scheme: dark` enables matching native controls.

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
- `h1`, `.page-title` (including section h2): 22px / 700 weight
- `.login-title`: 20px; profile header h2/h3: 16px
- `.quality-section-head > :is(h2,h3)`: 13px uppercase amber;
  `.quality-details-title`: 13px inline heading in the advanced summary
- Nav / tab links: 13px, uppercase, `0.03em` tracking
- Table headers and primary field labels: 12px, uppercase, ≈`0.05em` tracking
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
  push the logout form right. Height is automatic (minimum 62px). Logout is
  a POST form shown only when `auth_enabled`; `.logout-link` resets the button
  to the same appearance as a text link. The brand links to `/`.
- On phones (≤640px), the same nav becomes a fixed five-section bottom bar:
  Search, Jobs, Subscriptions, Quality, Events. The Subscriptions tab has a
  slightly wider column so the same 12px label fits even at 360px.
  Small inline SVG icons and counters keep every section visible. Brand and
  POST logout stay in the top header; login shows neither nav nor logout.
  Desktop navigation is unchanged. `viewport-fit=cover`, safe-area padding,
  and the dark `theme-color` support mobile browser chrome.
- `.skip-link` targets focusable `main#main`; an amber inline SVG favicon uses
  the same SK brand mark. Sticky-header clearance uses scroll padding/margins.
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
  `--border-control` border, `--text` colored text. Use for anything that isn't the
  primary action but shouldn't look alarming.
- **`.btn-danger`** — destructive action (used on every "Delete" button).
  Detail pages use a red gradient (`#ff8b96` → `--red`) and dark text.
  Inside tables this becomes a quiet outlined action with `--error-text`.
- **`.btn-link`** — small underlined action, such as live-status Retry.
- Disabled buttons and `[aria-disabled="true"]` use reduced opacity and a
  not-allowed cursor; ARIA alone does not prevent activation.
- Search, catalog search, and quality save buttons opt into shared pending UI
  with `data-pending-label="Searching…"` or `"Saving…"`. `confirm.js` updates
  their labels, disables them after submission data is collected, and sets
  form `aria-busy`. Back/forward restoration resets this state. Grab has its
  own submit guard and does not use this attribute.
- Latest releases has a direct Grab form with `data-pending-label="Adding…"`,
  followed by an underlined Search link. `.subscription-actions` aligns these
  at center and wraps with an 8px × 10px gap on narrow screens.

### Job lists and action hierarchy

Jobs uses three link-based tabs: **Queue**, **Needs attention**, and **History**.
Each tab has a count and the current link has `aria-current="page"`. Queue
updates live; Needs attention includes an error excerpt and a Details link;
History shows Finished (`updated_at`) and `.pagination` controls.

`.job-actions` groups wrapping actions without form margins. Retry stays the
primary amber recovery action wherever available. **Remove from list (keep
files)** is quiet and removes the job from the list/qBittorrent without deleting
downloaded or library files. **Delete** is outlined danger in tables and filled
danger on details. Both confirmations name the job and explain which files
are kept or deleted. Keep server/client Delete confirmation wording aligned.
The detail action group moves onto a separate row at 640px; long action labels
wrap. History uses `.table--sticky-title` for its first title column.

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
| `.badge-completed` | teal | displayed as “downloaded” (not yet organized) |
| `.badge-organizing` | amber | organizing (dot pulses via `@keyframes pulse`) |
| `.badge-organized` | green | organized (terminal success) |
| `.badge-needs_attention` | amber, diamond dot | needs_attention (Needs attention tab) |
| `.badge-failed` | light red | failed (Needs attention tab) |
| `.badge-deleting` | neutral, dashed border | deleting (Queue tab) |
| `.badge-unread` | neutral | unread event (not a job status) |

Aliases `.badge--needs-attention`, `.badge--failed`, and `.badge--deleting`
are supported. Badge links are visibly underlined and may wrap. Dot spans
are decorative (`aria-hidden="true"`); always include the written status.

Adding a new job status requires adding both a `.badge-{status}` rule here
and confirming its Queue / Needs attention / History membership in
`routes/jobs.py`.

### Tables
Plain `table` inside a `.panel.table-wrap` (the wrapper adds
`overflow-x: auto` so wide tables scroll horizontally on narrow screens
instead of breaking layout). Header cells use `scope="col"` and readable
mono/uppercase secondary text; body
cells are sans, 14px, row-hover highlights via `--surface-2`. Use
`.cell-muted` for secondary column content (indexer, type, size) and
`.release-title` for filename/release-style strings that should read as
mono. Long release names wrap with `overflow-wrap: anywhere`. Sizes divided
by 1073741824 are labeled GiB; unknown/zero sizes display an em dash.
At 641–780px subscription titles (first column) and job titles (third
column) are sticky with solid backgrounds. `.table--sticky-title` provides
first-column opt-in for other tables. At ≤640px, `.mobile-cards` tables use
one bordered grid card per row, without horizontal scrolling. Add `.card-title`
to the title cell (shown first, spanning the card), `data-label` to metadata
cells, `.card-wide` to long content/progress, and `.card-actions` to the full-width
action cell. Keep these attributes in live-update templates too. The original
header remains screen-reader-accessible; search replaces its interactive sort
header with a visible `.mobile-sort` navigation. Event rows have `event-{id}` anchors
and an amber target highlight after a mark-read redirect.

### Progress bar
`.progress-cell` (flex row) → `.progress-track` (the pill-shaped groove) →
`.progress-fill` (blue→teal gradient, animated `width` transition) +
`.progress-pct` (mono percentage label).

The detail page shows download progress only while downloading. Queue rows
keep a live progress bar. Needs attention uses a Problem column instead;
History uses Finished rather than meaningless progress placeholders.

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
`.alert` is the base (padding, radius, 13.5px); `.alert-error` uses red wash and
`--error-text`, `.alert-success` uses green. `.alert-prominent` adds a strong
left border for failed jobs. Error summaries can contain linked lists.
Errors use `role="alert"`; routine confirmation uses `role="status"`.

`.notice` is the shared inline-banner alternative with `--success`,
`--warning`, and `--error` modifiers (for example `.notice--warning` for
missing TMDB configuration). Don't render setup instructions as failures.

`.alert-info` is blue and `.alert-warning` is amber. The base template displays
signed flash feedback at the top of main with `.flash`: 14px × 18px padding,
3px left border, 24px space below. Errors use `role="alert"`; other flash
messages use `role="status"`. There is no automatic dismissal or timer.

`.nav-count` is a compact, nonshrinking 11px numeric pill inside main-nav links:
Jobs counts attention items, Events counts unread events. Zero counts are
omitted. Active-link pills turn amber. The link and pill stay together when
the tablet nav scrolls; on phones the pill sits over its section icon. Hidden
text supplies the count's meaning. Mobile counters and other secondary text
use at least 12px.

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
Text/number/password inputs, `select`, and `textarea` share one rule block:
sans font, `--surface-2` background, `--border-control` border, `--radius-sm`.
Focus has an amber border and soft glow; keyboard focus has a solid amber
outline plus a 2px ring. Invalid inputs keep that ring as well as their red
error boundary. The grab mini-form
(`.grab-form`, in the search results table) has its own tighter padding and
fixed per-field widths (title/year/season/episode) since it lives inside a
table cell; the retry form (`.retry-form`) uses labeled stacked
label+input pairs (mono uppercase label above a full input).

At ≤780px, text/number/password fields, selects and textareas use 16px to avoid
iOS focus zoom. Interactive targets are at least 44×44px; checkbox labels are
at least 48px tall (the visual checkbox stays compact). Search uses a full-width
query field and `inputmode="search"` / `enterkeyhint="search"`; integer metadata
uses numeric input mode, GiB inputs use decimal mode. At ≤640px Grab and retry
actions span the available width; Grab metadata uses a two-column grid with a
full-width title and episode-set field.

Quality and custom TV scope forms use sticky save footers above the bottom nav
on phones. Their containers must not clip overflow, or sticky positioning stops
working. Keep the footer in normal document flow so the final fields can scroll
clear of it. Hide the duplicate scope-intro save button on phones when the footer
exists. Body height is automatic, with bottom padding for the bar, home indicator
and 32px breathing room: the last content and flashes must scroll above the bar.
No timed flashes, new animation, or mobile-only JS is needed.

Job tables additionally use `.job-cards`: compact title/status header, a
`Movie · #7` metadata line, and inline progress/percentage beside a quiet Delete
button. The amber title is the primary details target (44px tall; long titles
truncate on phones, with the full title available on detail). Attention excerpts
clamp to two lines, and keep-files/Delete actions share a row. These rules also
apply to live-inserted rows; desktop tables retain their existing presentation.
TV search guesses missing season or episode show a warning such as
`Arrival (2016) — season/episode needed`, not a misleading `S?E?` destination.

### Tab bar
`.tab-bar` is a bottom-bordered flex row of `.tab-link`s (mono, uppercase,
2px bottom border on active, amber when active) each optionally followed by
a `.tab-count` pill (faint by default, amber-tinted when its parent tab is
active). Used for the Jobs Queue/Needs attention/History switch — reuse this pattern for
any future top-level filter/segmented-control UI rather than inventing a
new tab style.

`.tabs-nav` and `.quality-tabs` provide the same treatment for direct anchor
children; use `aria-current="page"` on the selected link. Tabs wrap as needed.

### Quality profiles and disclosures

Profile headers use 22px × 24px padding and a raised surface. Sections are
separated by subtle borders and use the same padding; `.quality-section--split`
uses two equal columns with a full-width section heading. Legends are muted
12px mono. Focusable fieldsets have a 2px amber outline (red when invalid).
GiB/seed-count inputs are compact (160px); text preference inputs span the
field width and list supported values in an associated hint.

`details.disclosure > summary` is the general advanced-section pattern.
`.grab-review` / `.grab-review-toggle` and `.quality-advanced` are native
details variants. `.grab-field` stacks a label above its input;
`.organize-as` / `.grab-organize-as` show compact destination metadata.

### Live status and smaller utilities

- `.live-status[data-live-state]`: `connecting` and `reconnecting` are amber;
  `live` is teal; `disconnected` is red with Retry; `closed` is muted and reads
  “Up to date”. State text is announced through `role="status"`.
- `.visually-hidden`: screen-reader-only text. Never use an `aria-label` on
  a plain span in place of an actual hidden description.
- `.field-error` plus `[aria-invalid="true"]`: field feedback; connect it
  with `aria-describedby`.
- `.setup-required`: amber setup pill. Linked subscription setup badges use
  `.badge.badge-needs_attention` with visible link styling.
- `.organized-files` / `.episode-chip`: destination paths and episode labels.
- `.events-filters`, `.pagination`: wrapping secondary navigation;
  `.quality-changed`: amber emphasis for changed candidate quality.
- `.tv-included-note`: explains season-covered episodes; shared form JS keeps
  the description and selected count current. Sidebar buttons may wrap.
- `.episode-load-status`: native season disclosures fetch episodes on first
  opening. Loading shows a small spinner and live text; errors use an inline
  red state with a **Try again** button that preserves unsaved selections.
  The episode list has `aria-busy` while loading. Empty results get explicit
  text. A Load episodes link remains as the no-JS navigation fallback. The
  spinner stops under reduced motion. Server-rendered selection counts remain
  visible until the lazy list has loaded.
- `[hidden]` always wins over component display styles.

## Focus & interaction states

Every focusable element gets a consistent focus-visible ring:
```css
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
```
Inputs additionally get a solid `0 0 0 2px var(--accent)` ring on
`:focus-visible`, including invalid fields; they never remove the outline.
Plain `:focus` keeps the soft glow. Buttons/links rely on the global rule
plus their existing hover treatments (brightness bump for primary buttons,
background/color shift for nav/tab/quiet links).

Forced-colors mode restores native quality checkboxes and gives TV checkboxes
system-color boundaries and an explicit checkmark. Reduced-motion mode
disables entry animation, pulse, transitions, and smooth scrolling.

## Responsive behavior

Breakpoints:
- **900px:** nav moves to its own horizontally scrolling row with proximity
  scroll-snap. Brand/logout remain visible; the header itself never scrolls.
- **780px:** touch targets ≥44px, inputs ≥16px, secondary text ≥12px. TV detail
  layout becomes one column. Tablet tables retain sticky titles and compact
  12px padding (641–780px only).
- **640px:** safe-area-aware 16px shell/header padding, fixed bottom nav,
  table cards, full-width search field, stacked actions, quality split groups
  become one column, sticky quality/scope save footers. Bottom shell padding
  is moved to body to reserve space for navigation and the home indicator. TV hero keeps a compact
  poster beside the title; episodes wrap instead of truncating.
- **440px:** TV sidebar becomes one column; hero actions span full width.

Use `.mobile-cards` for every new data table and supply its cell roles/labels.
Do not hide page overflow to mask wide content. Long unbroken titles, errors,
paths, and delivery badges must wrap. Check both `scrollWidth > innerWidth`
and `scrollWidth > device viewport width`: mobile browsers may expand the
layout viewport to accommodate accidental overflow.
