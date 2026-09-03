# TV Season and Episode Subscription Design

**Date:** 2026-09-03  
**Status:** Ready for review

## Goal

Let users target an entire TV series, selected seasons, or exact episodes; notify
or auto-download only matching content, including only the required file from a
multi-file torrent.

## Scope

- A TV detail page lists TMDB seasons, episodes, and specials (season 0).
- Series mode automatically includes current and future seasons, episodes, and
  specials. Manual mode selects seasons and/or episodes.
- A season matches season packs and individual episodes. An episode matches a
  single episode, multi-episode pack, or season pack that contains it.
- The existing per-subscription auto-download setting applies after scope
  matching and uses the global quality profile.

## Data and matching

Persist TV scope separately from the existing series subscription: stable TMDB
episode IDs plus series/season/episode coordinates, and selected seasons. A
series subscription has future-content enabled; manual rows are immutable until
edited. Store the target scope alongside each found release so duplicate scans
and duplicate jobs are impossible.

Use existing `parse_release()` season, episode, and normalized episode-set
fields. A pack matches a requested episode only when its parsed season matches
and the episode set contains that episode.

## Selective torrent download

For an eligible auto-download, add the torrent paused, poll qBittorrent until
file metadata is available, disable every file, enable only files that parse to
the requested episode, then resume. Use qBittorrent file `index`, not list
position. If no requested file is present, keep the torrent paused, persist a
bounded error, and import nothing. Shared torrent pieces may still download.

## Interface

The subscriptions page links TV rows to a detail page. It offers whole-series
mode, season checkboxes, and expandable episode checkboxes. The detail page
shows auto-download state and explains that it follows the global quality
profile.

## Safety and tests

No TV torrent starts before priorities are successfully applied. Tests cover
TMDB season data, specials, future seasons, exact episode and pack matching,
missing target files, paused metadata polling, priority requests, duplicate
prevention, and existing manual/movie behavior.
