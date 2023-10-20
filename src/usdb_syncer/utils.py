"""General-purpose utilities."""

import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from appdirs import AppDirs

from usdb_syncer.logger import get_logger

_logger = get_logger(__file__)

CACHE_LIFETIME = 60 * 60
_app_dirs = AppDirs("usdb_syncer", "bohning")


def _root() -> Path:
    """Returns source root folder or temprory bundle folder if running as such.

    https://pyinstaller.org/en/stable/runtime-information.html#run-time-information
    """
    if getattr(sys, "frozen", False) and (bundle := getattr(sys, "_MEIPASS", None)):
        return Path(bundle)
    return Path(__file__).parent.parent.parent.absolute()


class AppPaths:
    """App data paths."""

    log = Path(_app_dirs.user_data_dir, "usdb_syncer.log")
    song_list = Path(_app_dirs.user_cache_dir, "available_songs.json")
    root = _root()
    fallback_song_list = Path(root, "data", "song_list.json")
    profile = Path(root, "usdb_syncer.prof")

    @classmethod
    def make_dirs(cls) -> None:
        cls.log.parent.mkdir(parents=True, exist_ok=True)
        cls.song_list.parent.mkdir(parents=True, exist_ok=True)


class DirectoryCache:
    """Helper to keep track of directories.

    This is to avoid a race condition when two songs requiring the same folder name
    are downloaded concurrently.
    """

    _cache: dict[Path, float] = {}

    @classmethod
    def insert(cls, path: Path) -> bool:
        """True if path was not in the cache (or the entry had expired)."""
        now = time.time()
        if cls._cache.get(path, 0) + CACHE_LIFETIME < now:
            cls._cache[path] = now
            return True
        return False


def extract_youtube_id(url: str) -> str | None:
    """Extracts the YouTube id from a variety of URLs.

    Partially taken from `https://regexr.com/531i0`.
    """

    pattern = r"""
        (?:https?://)?
        (?:www\.)?
        (?:m\.)?
        (?:
            youtube\.com/
            |
            youtube-nocookie\.com/
            |
            youtu\.be               # no '/' because id may follow immediately
        )
        \S*
        (?:/|%3D|v=|vi=)
        ([0-9a-z_-]{11})            # the actual id
        (?:[%#?&]|$)                # URL may contain additonal parameters
        .*
        """
    if match := re.search(pattern, url, re.VERBOSE | re.IGNORECASE):
        return match.group(1)
    return None


def extract_vimeo_id(url: str) -> str | None:
    """Extracts the Vimeo id from a variety of URLs."""

    pattern = r"""
        (?:https?://)?
        (?:
            www\.
            |
            player\.
        )?
        (?:vimeo\.com/)
        (?:video/)?
        (\d{2,9})                   # the actual id
        (?:[%#?&]|$)                # URL may contain additonal parameters
        .*
        """
    if match := re.search(pattern, url, re.VERBOSE | re.IGNORECASE):
        return match.group(1)
    return None


def try_read_unknown_encoding(path: Path) -> str | None:
    for codec in ["utf-8-sig", "cp1252"]:
        try:
            with open(path, encoding=codec) as file:
                return file.read()
        except UnicodeDecodeError:
            pass
    return None


FILENAME_REPLACEMENTS = (('?:"', ""), ("<", "("), (">", ")"), ("/\\|*", "-"))


def sanitize_filename(fname: str) -> str:
    for old, new in FILENAME_REPLACEMENTS:
        for char in old:
            fname = fname.replace(char, new)
    if fname.endswith("."):
        fname = fname.rstrip(" .")  # Windows does not like trailing periods
    return fname


def next_unique_directory(path: Path) -> Path:
    """Ensures directory name is unique by adding a suffix if necessary."""
    out_path = path
    suffix = 0
    while not DirectoryCache.insert(out_path) or out_path.exists():
        suffix += 1
        out_path = path.with_name(f"{path.name} ({suffix})")
    return out_path


def is_name_maybe_with_suffix(text: str, name: str) -> bool:
    """True if `text` is 'name' or 'name (n)' for the provided `name` and some number n."""
    if not text.startswith(name):
        return False
    tail = text.removeprefix(name)
    return not tail or re.fullmatch(r" \(\d+\)", tail) is not None


def open_file_explorer(path: Path) -> None:
    _logger.debug(f"Opening '{path}' with file explorer.")
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", str(path)], check=True)
    else:
        subprocess.run(["open", str(path)], check=True)


def add_to_system_path(path: str) -> None:
    os.environ["PATH"] = path + os.pathsep + os.environ["PATH"]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def resource_file_ending(name: str) -> str:
    """Return the suffix or name, including " [BG]" and " [CO]"."""
    regex = re.compile(r".+?((?: \[(?:CO|BG)\])?\.[^.]+)")
    if match := regex.fullmatch(name):
        return match.group(1)
    return ""
