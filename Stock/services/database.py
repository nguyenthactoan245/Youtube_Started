"""Persistent, global Pixabay download history. Connections are never shared by threads."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread

from models import Video

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "stock.db"
LEASE_SECONDS = 120


class VideoDatabase:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, heartbeat REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    video_id INTEGER,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    keyword TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL UNIQUE,
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','downloading','completed','failed','cancelled','review')),
                    owner TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    hidden_at REAL,
                    UNIQUE(source, video_id)
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(project_id, position)
                );
                CREATE INDEX IF NOT EXISTS chapters_project_position
                    ON chapters(project_id, position);
                CREATE TABLE IF NOT EXISTS video_placements (
                    id INTEGER PRIMARY KEY,
                    video_record_id INTEGER NOT NULL REFERENCES videos(id),
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    chapter_id INTEGER REFERENCES chapters(id),
                    assigned_at REAL NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
            if "hidden_at" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN hidden_at REAL")
            placement_columns = {row[1] for row in db.execute("PRAGMA table_info(video_placements)")}
            if "id" not in placement_columns:
                db.execute("BEGIN IMMEDIATE")
                placement_columns = {row[1] for row in db.execute("PRAGMA table_info(video_placements)")}
                if "id" not in placement_columns:
                    db.execute("""CREATE TABLE video_placements_migrated (
                        id INTEGER PRIMARY KEY,
                        video_record_id INTEGER NOT NULL REFERENCES videos(id),
                        project_id INTEGER NOT NULL REFERENCES projects(id),
                        chapter_id INTEGER REFERENCES chapters(id),
                        assigned_at REAL NOT NULL
                    )""")
                    db.execute("""INSERT INTO video_placements_migrated
                        (video_record_id, project_id, chapter_id, assigned_at)
                        SELECT video_record_id, project_id, chapter_id, assigned_at
                        FROM video_placements""")
                    db.execute("DROP TABLE video_placements")
                    db.execute("ALTER TABLE video_placements_migrated RENAME TO video_placements")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS video_placements_unique_target
                ON video_placements(video_record_id, project_id, COALESCE(chapter_id, 0))""")
            db.execute("""CREATE INDEX IF NOT EXISTS video_placements_project_chapter
                ON video_placements(project_id, chapter_id)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def start_batch(self) -> str:
        owner = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE videos SET status='failed', owner=NULL,
                error='Lượt tải trước bị gián đoạn', updated_at=?
                WHERE status IN ('queued','downloading') AND owner IN
                (SELECT id FROM batches WHERE heartbeat < ?)""",
                (time.time(), time.time() - LEASE_SECONDS))
            db.execute("DELETE FROM batches WHERE heartbeat < ?", (time.time() - LEASE_SECONDS,))
            db.execute("INSERT INTO batches VALUES (?, ?)", (owner, time.time()))
        return owner

    def heartbeat(self, owner: str):
        with self.connect() as db:
            db.execute("UPDATE batches SET heartbeat=? WHERE id=?", (time.time(), owner))

    @contextmanager
    def batch(self):
        owner = self.start_batch()
        stop = Event()

        def keep_alive():
            while not stop.wait(10):
                try:
                    self.heartbeat(owner)
                except sqlite3.Error:
                    # A worker must still verify ownership before finalizing its file.
                    pass

        thread = Thread(target=keep_alive, daemon=True)
        thread.start()
        try:
            yield owner
        finally:
            stop.set()
            thread.join(timeout=35)
            self.finish_batch(owner)

    def finish_batch(self, owner: str):
        with self.connect() as db:
            db.execute("""UPDATE videos SET status='cancelled', owner=NULL, updated_at=?
                WHERE owner=? AND status IN ('queued','downloading')""", (time.time(), owner))
            db.execute("DELETE FROM batches WHERE id=?", (owner,))

    def reserve(self, video: Video, keyword: str, target: Path, owner: str) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM batches WHERE id=?", (owner,)).fetchone():
                raise RuntimeError("Lượt tải đã hết hiệu lực.")
            row = db.execute("SELECT status, hidden_at FROM videos WHERE source='pixabay' AND video_id=?",
                             (video.id,)).fetchone()
            if row and row['hidden_at'] is None and row['status'] not in ('failed', 'cancelled'):
                return False
            db.execute("""INSERT INTO videos
                (source, video_id, metadata, keyword, file_path, status, owner, created_at, updated_at)
                VALUES ('pixabay', ?, ?, ?, ?, 'queued', ?, ?, ?)
                ON CONFLICT(source, video_id) DO UPDATE SET
                metadata=excluded.metadata, keyword=excluded.keyword, file_path=excluded.file_path,
                status='queued', owner=excluded.owner, error='', hidden_at=NULL,
                updated_at=excluded.updated_at""",
                (video.id, json.dumps(asdict(video), ensure_ascii=False), keyword, str(target.resolve()), owner, now, now))
            return True

    def owns(self, video_id: int, owner: str) -> bool:
        with self.connect() as db:
            return db.execute("""SELECT 1 FROM videos WHERE source='pixabay'
                AND video_id=? AND owner=? AND status IN ('queued','downloading')""", (video_id, owner)).fetchone() is not None

    def record(self, video_id: int, owner: str, status: str, error: str = ''):
        with self.connect() as db:
            db.execute("""UPDATE videos SET status=?, error=?, updated_at=?,
                completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END,
                thumbnail_path=CASE WHEN ?='completed' THEN substr(file_path,1,length(file_path)-4)||'.jpg' ELSE thumbnail_path END,
                owner=CASE WHEN ? IN ('completed','failed','cancelled') THEN NULL ELSE owner END
                WHERE source='pixabay' AND video_id=? AND owner=?""",
                (status, error, time.time(), status, time.time(), status, status, video_id, owner))

    def library(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM videos WHERE status IN ('completed','review')
                AND hidden_at IS NULL ORDER BY COALESCE(completed_at,created_at) DESC""").fetchall()
        return [dict(row, details=json.loads(row['metadata'])) for row in rows]

    def video_records(self, ids: list[int], pixabay_ids: bool = False) -> list[dict]:
        if not ids:
            return []
        column = "video_id" if pixabay_ids else "id"
        source = "AND source='pixabay'" if pixabay_ids else ""
        rows = []
        with self.connect() as db:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(db.execute(f"""SELECT * FROM videos WHERE {column} IN ({placeholders}) {source}
                    AND status IN ('completed','review') AND hidden_at IS NULL""", chunk).fetchall())
        return [dict(row) for row in rows]

    def delete_video(self, record_id: int) -> bool:
        """Remove a completed video and its placement so its Pixabay ID can be reused."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM videos WHERE id=?", (record_id,)).fetchone()
            if row is None or row["status"] not in ("completed", "review"):
                return False
            db.execute("DELETE FROM video_placements WHERE video_record_id=?", (record_id,))
            db.execute("DELETE FROM videos WHERE id=?", (record_id,))
        return True

    def assign_videos(self, record_ids: list[int], project_id: int,
                      chapter_id: int | None = None) -> int:
        return self.assign_videos_to_destinations(record_ids, [(project_id, chapter_id)])

    def assign_videos_to_destinations(
        self, record_ids: list[int], destinations: list[tuple[int, int | None]]
    ) -> int:
        """Add missing video placements; preserve every existing project/chapter link."""
        if not record_ids or not destinations:
            return 0
        destinations = list(dict.fromkeys(destinations))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for project_id, chapter_id in destinations:
                if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise ValueError("Project không còn tồn tại.")
                if chapter_id is not None and not db.execute(
                    "SELECT 1 FROM chapters WHERE id=? AND project_id=?", (chapter_id, project_id)
                ).fetchone():
                    raise ValueError("Chapter không thuộc project đã chọn.")
            valid: set[int] = set()
            for offset in range(0, len(record_ids), 500):
                chunk = record_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                valid.update(row[0] for row in db.execute(
                    f"""SELECT id FROM videos WHERE id IN ({placeholders})
                        AND status IN ('completed','review') AND hidden_at IS NULL""", chunk))
            if len(valid) != len(set(record_ids)):
                raise ValueError("Có video không còn trong Library.")
            now = time.time()
            inserted = db.executemany("""INSERT OR IGNORE INTO video_placements
                (video_record_id, project_id, chapter_id, assigned_at) VALUES (?, ?, ?, ?)""",
                [(record_id, project_id, chapter_id, now)
                 for record_id in sorted(valid) for project_id, chapter_id in destinations]).rowcount
        return inserted

    def list_project_videos(self, project_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT v.id, v.file_path, v.metadata, p.chapter_id
                FROM video_placements p JOIN videos v ON v.id=p.video_record_id
                WHERE p.project_id=? AND v.hidden_at IS NULL
                ORDER BY p.assigned_at, v.id""", (project_id,)).fetchall()
        return [dict(row, details=json.loads(row["metadata"])) for row in rows]

    def create_project(self, name: str) -> int:
        name = name.strip()
        if not 1 <= len(name) <= 120:
            raise ValueError("Tên project phải có từ 1 đến 120 ký tự.")
        with self.connect() as db:
            cursor = db.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)",
                                (name, time.time()))
            return cursor.lastrowid

    def list_projects(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT p.id, p.name, p.created_at,
                COUNT(c.id) AS chapter_count FROM projects p
                LEFT JOIN chapters c ON c.project_id=p.id
                GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC""").fetchall()
        return [dict(row) for row in rows]

    def create_chapter(self, project_id: int, title: str) -> int:
        title = title.strip()
        if not 1 <= len(title) <= 120:
            raise ValueError("Tên chapter phải có từ 1 đến 120 ký tự.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("Project không còn tồn tại.")
            position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM chapters WHERE project_id=?",
                                  (project_id,)).fetchone()[0]
            cursor = db.execute("""INSERT INTO chapters (project_id, title, position, created_at)
                VALUES (?, ?, ?, ?)""", (project_id, title, position, time.time()))
            return cursor.lastrowid

    def list_chapters(self, project_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT id, project_id, title, position, created_at
                FROM chapters WHERE project_id=? ORDER BY position, id""", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_all_chapters(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT id, project_id, title, position, created_at
                FROM chapters ORDER BY project_id, position, id""").fetchall()
        return [dict(row) for row in rows]

    def delete_chapter(self, project_id: int, chapter_id: int) -> int:
        """Delete a chapter, keeping its videos assigned to the parent project."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM chapters WHERE id=? AND project_id=?",
                              (chapter_id, project_id)).fetchone():
                raise ValueError("Chapter không còn thuộc project này.")
            affected = db.execute("""SELECT COUNT(*) FROM video_placements
                WHERE project_id=? AND chapter_id=?""", (project_id, chapter_id)).fetchone()[0]
            db.execute("""DELETE FROM video_placements WHERE id IN (
                SELECT child.id FROM video_placements child
                JOIN video_placements root
                  ON root.video_record_id=child.video_record_id
                 AND root.project_id=child.project_id AND root.chapter_id IS NULL
                WHERE child.project_id=? AND child.chapter_id=?
            )""", (project_id, chapter_id))
            db.execute("""UPDATE video_placements SET chapter_id=NULL
                WHERE project_id=? AND chapter_id=?""", (project_id, chapter_id))
            db.execute("DELETE FROM chapters WHERE id=?", (chapter_id,))
        return affected

    def delete_project(self, project_id: int) -> int:
        """Delete a project and its placements without deleting Library videos."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("Project không còn tồn tại.")
            unassigned = db.execute("DELETE FROM video_placements WHERE project_id=?",
                                    (project_id,)).rowcount
            db.execute("DELETE FROM chapters WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return unassigned

    def import_existing(self, *roots: Path) -> int:
        """Register old files without renaming, moving, or deleting anything."""
        added = 0
        with self.connect() as db:
            for root in roots:
                if not root.exists():
                    continue
                for file in root.rglob('*.mp4'):
                    try:
                        stat = file.stat()
                    except OSError:
                        continue
                    if not stat.st_size:
                        continue
                    match = re.fullmatch(r'(\d+)_(\d+)x(\d+)', file.stem)
                    video_id = int(match[1]) if match else None
                    details = {'tags': file.stem, 'size': stat.st_size}
                    if match:
                        details.update(width=int(match[2]), height=int(match[3]))
                    cursor = db.execute("""INSERT OR IGNORE INTO videos
                        (source,video_id,metadata,keyword,file_path,thumbnail_path,status,created_at,updated_at,completed_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        ('pixabay' if match else 'local', video_id, json.dumps(details), file.parent.name,
                         str(file.resolve()), str(file.with_suffix('.jpg').resolve()),
                         'completed' if match else 'review', stat.st_mtime, time.time(), stat.st_mtime))
                    added += cursor.rowcount
        return added
