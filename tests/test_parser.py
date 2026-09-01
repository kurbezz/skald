from skald.parser import parse_release


def test_parse_movie_release():
    guess = parse_release("The.Matrix.1999.1080p.BluRay.x264-GROUP")
    assert guess["title"] == "The Matrix"
    assert guess["year"] == 1999
    assert guess["media_type"] == "movie"
    assert guess["season"] is None
    assert guess["episode"] is None


def test_parse_tv_release():
    guess = parse_release("Breaking.Bad.S01E05.720p.HDTV.x264-GROUP")
    assert guess["title"] == "Breaking Bad"
    assert guess["season"] == 1
    assert guess["episode"] == 5
    assert guess["media_type"] == "tv"
