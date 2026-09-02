# Search peer sorting design

## Goal

Let users sort search results by seeders or leechers. New result pages default to the highest number of seeders first.

## Approach

Sorting is server-side and controlled by URL query parameters. This keeps the selected sort shareable and works without client-side JavaScript.

`/search` accepts these parameters alongside `q`:

- `sort`: `seeders` or `leechers`; defaults to `seeders`.
- `direction`: `asc` or `desc`; defaults to `desc`.

Invalid or absent values use the defaults. The route sorts the `ReleaseResult` list before adding parsed title metadata and rendering the template.

## Interface

The Seeders and Leechers column headers become links. Each link retains the search query and selects its respective sort key. Selecting the active header toggles between ascending and descending order; selecting the other header starts in descending order. The active header displays its direction with an accessible text label.

The result table is initially sorted by seeders descending, including when a user submits a new search without sort parameters.

## Error handling

Unknown `sort` or `direction` values are ignored in favour of the default `seeders desc` order. Missing numeric peer data continues to use the existing integer model values.

## Testing

Extend the route tests with multiple fake releases that differ in seeder and leecher counts. Assert:

- default results are ordered by seeders descending;
- sorting by either field respects ascending and descending directions;
- invalid sort parameters fall back to the default order;
- rendered header links retain the query and expose the current sort state.
