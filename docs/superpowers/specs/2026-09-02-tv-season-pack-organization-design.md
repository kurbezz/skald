# TV season-pack organization

Date: 2026-09-02
Status: Approved

## Goal

Organize completed TV season-release packs by examining every video file in the
download, rather than treating the pack as one episode. Each recognized episode
is linked into the normal Jellyfin-compatible series and season layout. A pack
is all-or-nothing: an unrecognized-only pack or any condition that makes the
full set unsafe to organize leaves the job in `NEEDS_ATTENTION` with a clear
error.

## Scope

This behavior applies only to TV jobs whose completed download contains more
than one video file. Existing movie organization and existing single-video-file
TV organization continue to use their current metadata-driven behavior.

Video-file discovery continues to use the Organizer's existing whitelisted
video extensions. Non-video files, including subtitles, NFO files, artwork, and
samples that are not video files, are not organization candidates.

## Season-pack organization

For an eligible TV pack, the worker recursively scans the completed content
path and considers every whitelisted video file. For each candidate, it reads a
case-insensitive `SxxEyy` marker from that file's basename, where `xx` is a
two-digit season number and `yy` is a two-digit episode number. It accepts the
marker either without a separator (`S01E01`) or with one `.`, `_`, or `-`
separator between the season and episode portions (`S01.E01`, `S01_E01`, or
`S01-E01`). The marker is the source of season and episode placement for that
file; the job's confirmed season and episode values do not override it.

- A candidate with exactly one supported `SxxEyy` marker is recognized.
- A candidate with no such token is skipped and recorded for the final result;
  it does not prevent a pack containing at least one recognized episode from
  being organized.
- A candidate with more than one such token is ambiguous and prevents safe
  completion.
- The series name continues to come from the confirmed TV job title.
- Each recognized source maps to
  `{tv_root}/{Series}/Season {SS}/{Series} - S{SS}E{EE}.{ext}`, with the parsed
  numbers rendered as two digits and the source extension retained.

The organizer uses the existing hardlink-first behavior, falling back to copy
only for the existing cross-device case. Source files remain in the download
directory so the torrent can continue seeding.

## Safe completion and errors

Before creating any library files, the organizer builds the complete set of
recognized source-to-target mappings and validates it. A season pack is safe to
execute only when it has at least one recognized video and every target is
unique and absent from the library.

The organizer must set the job to `NEEDS_ATTENTION` and save a clear,
actionable `error_message` when:

- no video file in the pack has a valid `SxxEyy` token;
- a video filename has multiple `SxxEyy` tokens;
- two recognized source files map to the same target path;
- a target path already exists; or
- directory creation, linking, copying, or another filesystem error prevents
  the complete mapping from being created.

Error messages identify the failed condition and the relevant source filename
or target path. For example: `No video files with SxxEyy episode markers were
found`, `Ambiguous episode markers in Show.S01E01.S01E02.mkv`, or `Target
already exists: /library/tv/Show/Season 01/Show - S01E01.mkv`.

Validation happens before writes so known conflicts never create a partial
season in the library. If an unexpected write error occurs after linking or
copying has begun, the organizer removes only the library files it created for
that attempt, then marks the job `NEEDS_ATTENTION`. It never deletes or alters
pre-existing library files or download content. A cleanup failure is included
in the job's error message.

Only after every recognized mapping has been created successfully does the
worker transition the job to `ORGANIZED`. The existing retry flow for
`NEEDS_ATTENTION` re-runs the full validation and organization attempt.

## Organized-file tracking and deletion

Every successfully created library path for a season pack is recorded against
the job. Deleting that job removes exactly those recorded library files and no
other episodes in the series or season. Single-file movie and TV jobs retain
their existing `library_path` behavior.

## Test strategy

- Add organizer tests for a nested multi-file pack whose `SxxEyy` filenames
  link to the expected series and per-file season directories, retaining their
  extensions.
- Cover case-insensitive markers, multiple seasons in one pack, and ignored
  non-video files.
- Cover a pack with unmarked video files plus recognized episodes, and a pack
  with no recognized video files; the latter must become `NEEDS_ATTENTION` with
  the no-recognized-video error.
- Cover ambiguous multiple-marker filenames, duplicate derived targets, and
  existing targets. Assert each fails before creating any library files.
- Simulate a link/copy or directory-creation failure after one successful
  output; assert attempt-created outputs are cleaned up, download sources are
  unchanged, and the job becomes `NEEDS_ATTENTION` with the filesystem error.
- Verify deleting an organized season-pack job removes every path recorded for
  that job while preserving unrecorded files in the same season directory.
- Retain existing movie and single-video-file TV organization tests unchanged
  to confirm their metadata-driven paths and behavior remain intact.
