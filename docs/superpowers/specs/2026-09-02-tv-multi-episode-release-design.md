# TV multi-episode release support

Date: 2026-09-02
Status: Approved

## Goal

Accept TV releases whose parsed metadata identifies more than one episode, such
as `S07E01-06`, without serializing a Python list into an integer form field.
Treat multi-episode releases as normal TV content, preserving their episode set
for display and for safe organization.

## Data model

`MediaJob.season` remains the scalar starting season. `MediaJob.episode`
remains the scalar starting episode for compatibility. Add an additive,
nullable serialized episode-set field that preserves every parsed episode. A
single episode keeps the existing scalar representation and has no stored set;
a contiguous set can be rendered as `E01-E06`, while a non-contiguous set is
rendered explicitly.

The migration must leave existing rows valid and nullable. A release is a
multi-episode release when its normalized episode set has more than one value.

## Search and grab flow

The parser output is normalized at the search boundary. GuessIt collection
values become a sorted, deduplicated episode set rather than being placed
directly in a numeric input. Search results display the full episode range.

For complete multi-episode TV metadata, the compact grab form posts scalar
starting `season` and `episode` values plus the serialized episode set. For
review forms, users may edit the start season and the episode range/set in a
format validated by the server; malformed or empty required values receive a
422 response before qBittorrent is called. `/grab` must never receive a list in
an integer parameter.

## Organization

Existing multi-file season-pack behavior remains authoritative: each source
file's `SxxEyy` marker determines its own library target. Release-level episode
metadata is not used to override per-file filenames.

For a single combined video with a multi-episode set, create one deterministic
TV target using the range form, for example
`Show - S07E01-E06.mkv`. Non-contiguous episode sets use an unambiguous joined
form, such as `Show - S07E01-E03-E05.mkv`. The recorded library path/deletion
ledger continues to track the one created file.

If the completed release has multiple video files, preserve the existing
all-or-nothing pack validation and organization behavior. A combined filename
is used only for a single video file whose job has a multi-episode set.

## Display and retry

Search and job-detail views label multi-episode jobs with their formatted range
instead of implying a single episode. The retry form preserves the saved set
and offers the same validated editing semantics as the initial grab form.

## Error handling

Reject malformed serialized episode metadata, an empty set, non-positive
episodes, or missing scalar TV title/season/start episode with HTTP 422 before
any qBittorrent call. Deduplicate repeated episode values deterministically.
The server does not silently discard all but the first parsed episode.

## Test strategy

- Parse a real `S07E01-06` title and assert normalized scalar start plus its
  preserved episode set.
- Assert search form data contains integer `episode=1` and serialized range
  metadata, never `episode=[...]`.
- Cover `/grab` persistence and validation of a contiguous and non-contiguous
  multi-episode set, including zero qBittorrent calls on invalid input.
- Cover database migration/default compatibility for existing single-episode
  jobs.
- Cover compact/review form rendering and job-detail range display.
- Cover single-file range naming and preserve the existing multi-file
  per-source organization tests.
