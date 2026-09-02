import guessit

from skald.episodes import normalize_episode_set


def parse_release(release_title: str) -> dict:
    guess = guessit.guessit(release_title)
    episode_set = normalize_episode_set(guess.get("episode"))
    return {
        "title": guess.get("title"),
        "year": guess.get("year"),
        "season": guess.get("season"),
        "episode": episode_set[0] if episode_set else None,
        "episode_set": episode_set,
        "media_type": "tv" if guess.get("type") == "episode" else "movie",
    }
