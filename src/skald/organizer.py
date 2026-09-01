import os
import shutil
from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}


def movie_target_path(movies_root: str, title: str, year: int, source_ext: str) -> Path:
    folder_name = f"{title} ({year})"
    file_name = f"{title} ({year}){source_ext}"
    return Path(movies_root) / folder_name / file_name


def tv_target_path(tv_root: str, series: str, season: int, episode: int, source_ext: str) -> Path:
    season_folder = f"Season {season:02d}"
    file_name = f"{series} - S{season:02d}E{episode:02d}{source_ext}"
    return Path(tv_root) / series / season_folder / file_name


def find_video_files(content_path: Path) -> list[Path]:
    if content_path.is_file():
        return [content_path] if content_path.suffix.lower() in VIDEO_EXTENSIONS else []
    return sorted(
        p for p in content_path.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def link_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Target already exists: {target}")
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
