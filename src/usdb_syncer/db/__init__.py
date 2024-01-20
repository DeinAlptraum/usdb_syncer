"""Database utilities."""

import contextlib
import enum
import sqlite3
import threading
import time
from pathlib import Path
from typing import Generator, Iterable, Iterator

import attrs

from usdb_syncer import SongId, SyncMetaId, errors, logger
from usdb_syncer.utils import AppPaths

SCHEMA_VERSION = 1


_logger = logger.get_logger(__file__)


class _SqlCache:
    _cache: dict[str, str] = {}

    @classmethod
    def get(cls, name: str) -> str:
        if (stmt := cls._cache.get(name)) is None:
            cls._cache[name] = stmt = AppPaths.sql.joinpath(name).read_text("utf8")
        return stmt


class _DbState:
    """Singleton for managing the global database connection."""

    lock = threading.Lock()
    _connection: sqlite3.Connection | None = None

    @classmethod
    def connect(cls, db_path: Path | str, trace: bool = False) -> None:
        with cls.lock:
            cls._connection = sqlite3.connect(
                db_path, check_same_thread=False, isolation_level=None
            )
            if trace:
                cls._connection.set_trace_callback(_logger.debug)
            _validate_schema(cls._connection)

    @classmethod
    def connection(cls) -> sqlite3.Connection:
        if cls._connection is None:
            raise errors.DatabaseError("Not connected to database!")
        return cls._connection

    @classmethod
    def close(cls) -> None:
        if _DbState._connection is not None:
            _DbState._connection.close()
            _DbState._connection = None


@contextlib.contextmanager
def transaction() -> Generator[None, None, None]:
    with _DbState.lock:
        try:
            _DbState.connection().execute("BEGIN")
            yield None
        except Exception:  # pylint: disable=broad-except
            _DbState.connection().rollback()
            raise
        _DbState.connection().commit()


def _validate_schema(connection: sqlite3.Connection) -> None:
    meta_table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if meta_table is None:
        connection.executescript(_SqlCache.get("setup_script.sql"))
        connection.execute(
            "INSERT INTO meta (id, version, ctime) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, int(time.time() * 1_000_000)),
        )
    else:
        row = connection.execute("SELECT version FROM meta").fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            raise errors.UnknownSchemaError


def connect(db_path: Path | str, trace: bool = False) -> None:
    _DbState.connect(db_path, trace=trace)


def close() -> None:
    _DbState.close()


class SongOrder(enum.Enum):
    """Attributes songs can be sorted by."""

    NONE = None
    SONG_ID = "usdb_song.song_id"
    ARTIST = "usdb_song.artist"
    TITLE = "usdb_song.title"
    EDITION = "usdb_song.edition"
    LANGUAGE = "usdb_song.language"
    GOLDEN_NOTES = "usdb_song.golden_notes"
    RATING = "usdb_song.rating"
    VIEWS = "usdb_song.views"
    PINNED = "sync_meta.pinned"
    TXT = "txt.sync_meta_id IS NULL"
    AUDIO = "audio.sync_meta_id IS NULL"
    VIDEO = "video.sync_meta_id IS NULL"
    COVER = "cover.sync_meta_id IS NULL"
    BACKGROUND = "background.sync_meta_id IS NULL"
    SYNC_TIME = "sync_meta.mtime"


@attrs.define
class SearchBuilder:
    """Helper for building a where clause to find songs."""

    order: SongOrder = SongOrder.NONE
    descending: bool = False
    text: str = ""
    artists: list[str] = attrs.field(factory=list)
    titles: list[str] = attrs.field(factory=list)
    editions: list[str] = attrs.field(factory=list)
    languages: list[str] = attrs.field(factory=list)
    golden_notes: bool | None = None
    ratings: list[int] = attrs.field(factory=list)
    views: list[tuple[int, int | None]] = attrs.field(factory=list)
    downloaded: bool | None = None

    def _filters(self) -> Iterator[str]:
        if _fts5_phrases(self.text):
            yield "fts_usdb_song MATCH ?"
        if self.artists:
            yield _in_values_clause("usdb_song.artist", self.artists)
        if self.titles:
            yield _in_values_clause("usdb_song.title", self.titles)
        if self.editions:
            yield _in_values_clause("usdb_song.edition", self.editions)
        if self.languages:
            yield _in_values_clause("usdb_song.language", self.languages)
        if self.golden_notes is not None:
            yield "usdb_song.golden_notes = ?"
        if self.ratings:
            yield _in_values_clause("usdb_song.rating", self.ratings)
        if self.views:
            yield _in_ranges_clause("usdb_song.views", self.views)
        if self.downloaded is not None:
            yield f"sync_meta.sync_meta_id IS {'NOT ' if self.downloaded else ''}NULL"

    def _where_clause(self) -> str:
        where = " AND ".join(self._filters())
        return f" WHERE {where}" if where else ""

    def _order_by_clause(self) -> str:
        if not self.order.value:
            return ""
        return f" ORDER BY {self.order.value} {'DESC' if self.descending else 'ASC'}"

    def parameters(self) -> Iterator[str | int | bool]:
        if text := _fts5_phrases(self.text):
            yield text
        yield from self.artists
        yield from self.titles
        yield from self.editions
        yield from self.languages
        if self.golden_notes is not None:
            yield self.golden_notes
        yield from self.ratings
        for min_views, max_views in self.views:
            yield min_views
            if max_views is not None:
                yield max_views

    def statement(self) -> str:
        select_from = _SqlCache.get("select_song_id.sql")
        where = self._where_clause()
        order_by = self._order_by_clause()
        return f"{select_from}{where}{order_by}"


def _in_values_clause(attribute: str, values: list) -> str:
    return f"{attribute} IN ({', '.join('?'*len(values))})"


def _in_ranges_clause(attribute: str, values: list[tuple[int, int | None]]) -> str:
    return " OR ".join(
        f"{attribute} >= ?{'' if val[1] is None else f' AND {attribute} < ?'}"
        for val in values
    )


def _fts5_phrases(text: str) -> str:
    """Turns each whitespace-separated word into an FTS5 phrase."""
    return " ".join(f'"{s}"' for s in text.replace('"', "").split(" ") if s)


def _fts5_start_phrase(text: str) -> str:
    """Turns the entire string into an FTS5 initial phrase."""
    return f'''^ "{text.replace('"', "")}"'''


### UsdbSong


def get_usdb_song(song_id: SongId) -> tuple | None:
    stmt = f"{_SqlCache.get('select_usdb_song.sql')} WHERE usdb_song.song_id = ?"
    return _DbState.connection().execute(stmt, (song_id,)).fetchone()


def delete_usdb_song(song_id: SongId) -> None:
    _DbState.connection().execute("DELETE FROM usdb_song WHERE song_id = ?", (song_id,))


@attrs.define(frozen=True, slots=False)
class UsdbSongParams:
    """Parameters for inserting or updating a USDB song."""

    song_id: SongId
    artist: str
    title: str
    language: str
    edition: str
    golden_notes: bool
    rating: int
    views: int


def upsert_usdb_song(params: UsdbSongParams) -> None:
    stmt = _SqlCache.get("upsert_usdb_song.sql")
    _DbState.connection().execute(stmt, params.__dict__)


def upsert_usdb_songs(params: Iterable[UsdbSongParams]) -> None:
    stmt = _SqlCache.get("upsert_usdb_song.sql")
    _DbState.connection().executemany(stmt, (p.__dict__ for p in params))


def usdb_song_count() -> int:
    return _DbState.connection().execute("SELECT count(*) FROM usdb_song").fetchone()[0]


def max_usdb_song_id() -> SongId:
    row = _DbState.connection().execute("SELECT max(song_id) FROM usdb_song").fetchone()
    return SongId(row[0] or 0)


def delete_all_usdb_songs() -> None:
    _DbState.connection().execute("DELETE FROM usdb_song")


def all_local_usdb_songs() -> Iterable[SongId]:
    stmt = "SELECT DISTINCT song_id FROM sync_meta"
    return (SongId(r[0]) for r in _DbState.connection().execute(stmt))


def search_usdb_songs(search: SearchBuilder) -> Iterable[SongId]:
    rows = _DbState.connection().execute(search.statement(), tuple(search.parameters()))
    return (SongId(r[0]) for r in rows)


def find_similar_usdb_songs(artist: str, title: str) -> Iterable[SongId]:
    stmt = "SELECT rowid FROM fts_usdb_song WHERE artist MATCH ? AND title MATCH ?"
    params = (_fts5_start_phrase(artist), _fts5_start_phrase(title))
    return (SongId(r[0]) for r in _DbState.connection().execute(stmt, params))


### song filters


def usdb_song_artists() -> list[tuple[str, int]]:
    stmt = "SELECT artist, COUNT(*) FROM usdb_song GROUP BY artist ORDER BY artist"
    return _DbState.connection().execute(stmt).fetchall()


def usdb_song_titles() -> list[tuple[str, int]]:
    stmt = "SELECT title, COUNT(*) FROM usdb_song GROUP BY title ORDER BY title"
    return _DbState.connection().execute(stmt).fetchall()


def usdb_song_editions() -> list[tuple[str, int]]:
    stmt = "SELECT edition, COUNT(*) FROM usdb_song GROUP BY edition ORDER BY edition"
    return _DbState.connection().execute(stmt).fetchall()


def usdb_song_languages() -> list[tuple[str, int]]:
    stmt = (
        "SELECT language, COUNT(*) FROM usdb_song GROUP BY language ORDER BY language"
    )
    return _DbState.connection().execute(stmt).fetchall()


def search_usdb_song_artists(search: str) -> set[str]:
    stmt = "SELECT artist FROM fts_usdb_song WHERE artist MATCH ?"
    rows = _DbState.connection().execute(stmt, (_fts5_phrases(search),)).fetchall()
    return set(row[0] for row in rows)


def search_usdb_song_titles(search: str) -> set[str]:
    stmt = "SELECT title FROM fts_usdb_song WHERE title MATCH ?"
    rows = _DbState.connection().execute(stmt, (_fts5_phrases(search),)).fetchall()
    return set(row[0] for row in rows)


def search_usdb_song_editions(search: str) -> set[str]:
    stmt = "SELECT edition FROM fts_usdb_song WHERE edition MATCH ?"
    rows = _DbState.connection().execute(stmt, (_fts5_phrases(search),)).fetchall()
    return set(row[0] for row in rows)


def search_usdb_song_languages(search: str) -> set[str]:
    stmt = "SELECT language FROM fts_usdb_song WHERE language MATCH ?"
    rows = _DbState.connection().execute(stmt, (_fts5_phrases(search),)).fetchall()
    return set(row[0] for row in rows)


### SyncMeta


def get_in_folder(folder: Path) -> list[tuple]:
    stmt = f"{_SqlCache.get('select_sync_meta.sql')} WHERE path GLOB ? || '/*'"
    return _DbState.connection().execute(stmt, (folder.as_posix(),)).fetchall()


def reset_active_sync_metas(folder: Path) -> None:
    _DbState.connection().execute("DELETE FROM active_sync_meta")
    params = {"folder": folder.as_posix()}
    _DbState.connection().execute(_SqlCache.get("insert_active_sync_metas.sql"), params)


def update_active_sync_metas(folder: Path, song_id: SongId) -> None:
    _DbState.connection().execute(
        "DELETE FROM active_sync_meta WHERE song_id = ?", (song_id,)
    )
    params = {"folder": folder.as_posix(), "song_id": song_id}
    _DbState.connection().execute(_SqlCache.get("insert_active_sync_meta.sql"), params)


@attrs.define(frozen=True, slots=False)
class SyncMetaParams:
    """Parameters for inserting or updating a sync meta."""

    sync_meta_id: SyncMetaId
    song_id: SongId
    path: str
    mtime: int
    meta_tags: str
    pinned: bool


def upsert_sync_meta(params: SyncMetaParams) -> None:
    stmt = _SqlCache.get("upsert_sync_meta.sql")
    _DbState.connection().execute(stmt, params.__dict__)


def upsert_sync_metas(params: Iterable[SyncMetaParams]) -> None:
    stmt = _SqlCache.get("upsert_sync_meta.sql")
    _DbState.connection().executemany(stmt, (p.__dict__ for p in params))


def delete_sync_meta(sync_meta_id: SyncMetaId) -> None:
    _DbState.connection().execute(
        "DELETE FROM sync_meta WHERE sync_meta_id = ?", (sync_meta_id,)
    )


def delete_sync_metas(ids: tuple[SyncMetaId, ...]) -> None:
    id_str = ", ".join("?" for _ in range(len(ids)))
    _DbState.connection().execute(
        f"DELETE FROM sync_meta WHERE sync_meta_id IN ({id_str})", ids
    )


### ResourceFile


class ResourceFileKind(str, enum.Enum):
    """Kinds of resource files."""

    TXT = "txt"
    AUDIO = "audio"
    VIDEO = "video"
    COVER = "cover"
    BACKGROUND = "background"


@attrs.define(frozen=True, slots=False)
class ResourceFileParams:
    """Parameters for inserting or updating a resource file."""

    sync_meta_id: SyncMetaId
    kind: ResourceFileKind
    fname: str
    mtime: int
    resource: str


def delete_resource_files(ids: Iterable[tuple[SyncMetaId, ResourceFileKind]]) -> None:
    params = tuple(param for i, k in ids for param in (int(i), k.value))
    tuples = ", ".join("(?, ?)" for _ in range(len(params) // 2))
    _DbState.connection().execute(
        f"DELETE FROM resource_file WHERE (sync_meta_id, kind) IN ({tuples})", params
    )


def upsert_resource_files(params: Iterable[ResourceFileParams]) -> None:
    stmt = _SqlCache.get("upsert_resource_file.sql")
    _DbState.connection().executemany(stmt, (p.__dict__ for p in params))