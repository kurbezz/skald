# Grab metadata review design

## Goal

Keep completed metadata out of the search result table while clearly marking incomplete parsing and letting users reveal the editable metadata fields on demand.

## Parsing completeness

The requested search type determines completeness:

- Movies require a parsed title and year.
- TV releases require a parsed title, season, and episode.

Any missing required field marks the result as needing review. Optional fields remain optional in the grab request.

## Interface

Every result retains its own POST `/grab` form. For a complete parse, the form contains hidden metadata values and only a Grab button is visible.

For an incomplete parse, the row displays a clear review status and a `Review metadata` button. Pressing the button expands the existing editable title/year/season/episode fields in that same row. The trigger updates `aria-expanded`, controls the fields container with `aria-controls`, and moves keyboard focus to the title input. The user then corrects the values and submits Grab.

## Architecture

The search route calculates a `needs_review` boolean for each rendered result, avoiding duplicated completeness rules in the template. The template uses that state to render hidden values for complete results or a collapsed review section for incomplete ones.

A small search-page JavaScript module attaches click handlers to review buttons. It only controls disclosure state; metadata validation and `/grab` submission remain server-rendered form behavior.

## Failure and accessibility behavior

Incomplete metadata is never silently submitted: its editable controls remain in the form and only become visible through the review trigger. The trigger has an accessible name, initial `aria-expanded="false"`, and a matching `aria-controls` target. Each review region can be opened independently.

## Testing

Route/template integration tests cover complete movie and TV results rendering without visible editable fields, and incomplete results rendering a marked, initially collapsed review region with all required editable controls. A JavaScript unit test covers opening a review region, setting `aria-expanded`, unhiding the fields, and focusing the title input.
