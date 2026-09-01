import guessit


def parse_release(release_title: str) -> dict:
    guess = guessit.guessit(release_title)
    return {
        "title": guess.get("title"),
        "year": guess.get("year"),
        "season": guess.get("season"),
        "episode": guess.get("episode"),
        "media_type": "tv" if guess.get("type") == "episode" else "movie",
    }
