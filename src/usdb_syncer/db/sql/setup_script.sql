BEGIN;

CREATE TABLE meta (
    id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    ctime INTEGER NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE usdb_song (
    song_id INTEGER NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    language TEXT NOT NULL,
    edition TEXT NOT NULL,
    golden_notes BOOLEAN NOT NULL,
    rating INTEGER NOT NULL,
    views INTEGER NOT NULL,
    PRIMARY KEY (song_id)
);

CREATE TABLE sync_meta (
    sync_meta_id INTEGER NOT NULL,
    song_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    meta_tags TEXT NOT NULL,
    pinned BOOLEAN NOT NULL,
    PRIMARY KEY (sync_meta_id),
    UNIQUE (path),
    FOREIGN KEY (song_id) REFERENCES usdb_song (song_id) ON DELETE CASCADE
);

CREATE TABLE resource_file (
    sync_meta_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    fname TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    resource TEXT NOT NULL,
    PRIMARY KEY (sync_meta_id, kind),
    FOREIGN KEY (sync_meta_id) REFERENCES sync_meta (sync_meta_id) ON DELETE CASCADE
);

CREATE TABLE active_sync_meta (
    song_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    sync_meta_id INTEGER NOT NULL,
    PRIMARY KEY (song_id, rank),
    FOREIGN KEY (song_id, sync_meta_id) REFERENCES sync_meta (song_id, sync_meta_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE fts_usdb_song USING fts5 (
    song_id,
    artist,
    title,
    language,
    edition,
    content = usdb_song,
    content_rowid = song_id
);

CREATE TRIGGER fts_usdb_song_insert
AFTER
INSERT
    ON usdb_song BEGIN
INSERT INTO
    fts_usdb_song (
        rowid,
        song_id,
        artist,
        title,
        language,
        edition
    )
VALUES
    (
        new.song_id,
        new.song_id,
        new.artist,
        new.title,
        new.language,
        new.edition
    );

END;

CREATE TRIGGER fts_usdb_song_update
AFTER
UPDATE
    ON usdb_song BEGIN
UPDATE
    fts_usdb_song
SET
    rowid = new.song_id,
    song_id = new.song_id,
    artist = new.artist,
    title = new.title,
    language = new.language,
    edition = new.edition
WHERE
    rowid = old.song_id;

END;

CREATE TRIGGER fts_usdb_song_delete
AFTER
    DELETE ON usdb_song BEGIN
DELETE FROM
    fts_usdb_song
WHERE
    rowid = old.song_id;

END;

COMMIT;