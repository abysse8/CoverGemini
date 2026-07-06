from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, select, update
from sqlalchemy import event as sqla_event
from sqlalchemy.orm import Session

from .models import Event, ExplorerRun, SmsMessage

DEFAULT_USER_ID = "julien"


def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
    # SQLite ships with foreign keys OFF per connection; the raw sqlite3 path
    # turns them on in connect(), so the SQLAlchemy path must match.
    dbapi_connection.execute("PRAGMA foreign_keys = ON")

DEFAULT_PLATFORMS = [
    {
        "id": "welcome_to_the_jungle",
        "name": "Welcome to the Jungle",
        "base_url": "https://www.welcometothejungle.com",
        "login_url": "https://www.welcometothejungle.com/fr/login",
        "kind": "job_board",
    },
    {
        "id": "linkedin",
        "name": "LinkedIn",
        "base_url": "https://www.linkedin.com",
        "login_url": "https://www.linkedin.com/login",
        "kind": "job_board",
    },
    {
        "id": "jobteaser",
        "name": "JobTeaser",
        "base_url": "https://www.jobteaser.com",
        "login_url": "https://www.jobteaser.com/fr/users/sign_in",
        "kind": "job_board",
    },
    {
        "id": "apec",
        "name": "APEC",
        "base_url": "https://www.apec.fr",
        "login_url": "https://www.apec.fr/candidat.html",
        "kind": "job_board",
    },
    {
        "id": "custom_public",
        "name": "Custom public source",
        "base_url": "",
        "login_url": "",
        "kind": "public_source",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Query params that identify a *view* of a posting, not the posting itself.
# LinkedIn/APEC append these per search impression, so the same job scraped twice
# yields two different URLs -- and, before this, two different dedupe hashes.
_TRACKING_PARAMS = frozenset({
    "refid", "trackingid", "position", "pagenum", "trk", "trackingcontext",
    "origin", "originalsubdomain", "savedsearchid", "eboffer", "recommendedflavor",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
})


def normalize_offer_url(url: str) -> str:
    """Canonical form of an offer URL for dedup.

    Lowercases the host, drops the scheme and any trailing slash, and strips
    tracking query params (refId, trackingId, utm_*, ...). Two links to the same
    posting that differ only in tracking noise collapse to one key, so we stop
    storing the same job twice. Uses only urllib (stdlib) -- no new dependency.
    """
    url = url.strip()
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    query = urllib.parse.urlencode(sorted(kept))
    return urllib.parse.urlunsplit(("", host, path, query, "")).lstrip("/")


def offer_dedupe_hash(url: str = "", title: str = "", company: str = "", location: str = "", snippet: str = "") -> str:
    normalized_url = normalize_offer_url(url)
    if normalized_url:
        key = normalized_url
    else:
        key = "\n".join(
            " ".join(part.strip().lower().split())
            for part in (title, company, location, snippet[:500])
        )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class CoverAiStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()
        # SQLAlchemy engine alongside the raw sqlite3 path while methods are
        # converted one table at a time; both read the same file.
        self.engine = create_engine(f"sqlite:///{self.path}")
        sqla_event.listen(self.engine, "connect", _enable_foreign_keys)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    phone TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS platforms (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    login_url TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'job_board',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_platform_accounts (
                    user_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    login_url TEXT NOT NULL DEFAULT '',
                    profile_dir TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'not_connected',
                    last_login_check_at TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, platform_id),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (platform_id) REFERENCES platforms(id)
                );

                CREATE TABLE IF NOT EXISTS offers (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    dedupe_hash TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL DEFAULT '',
                    snippet TEXT NOT NULL DEFAULT '',
                    score INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS explorer_runs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    status TEXT NOT NULL,
                    config_path TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    offers_found INTEGER NOT NULL DEFAULT 0,
                    offers_new INTEGER NOT NULL DEFAULT 0,
                    offers_reported INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS queue_items (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    subject_type TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS sms_reports (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    offer_id TEXT NOT NULL,
                    number TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (offer_id) REFERENCES offers(id)
                );

                CREATE TABLE IF NOT EXISTS sms_messages (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    direction TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    response_text TEXT NOT NULL DEFAULT '',
                    command TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS application_tasks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    offer_id TEXT NOT NULL,
                    queue_item_id TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    role_title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'preparing',
                    readiness_percent INTEGER NOT NULL DEFAULT 0,
                    questions_total INTEGER NOT NULL DEFAULT 0,
                    questions_answered INTEGER NOT NULL DEFAULT 0,
                    questions_needs_user INTEGER NOT NULL DEFAULT 0,
                    questions_low_confidence INTEGER NOT NULL DEFAULT 0,
                    artifacts_json TEXT NOT NULL DEFAULT '{}',
                    strategy_text TEXT NOT NULL DEFAULT '',
                    last_action TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (offer_id) REFERENCES offers(id)
                );

                CREATE TABLE IF NOT EXISTS application_questions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    application_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    field_type TEXT NOT NULL DEFAULT 'text',
                    required INTEGER NOT NULL DEFAULT 1,
                    answer TEXT NOT NULL DEFAULT '',
                    answer_source TEXT NOT NULL DEFAULT 'unknown',
                    confidence INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'detected',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (application_id) REFERENCES application_tasks(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'julien',
                    event_type TEXT NOT NULL,
                    subject_type TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                """
            )
            self._ensure_column(conn, "offers", "user_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'")
            # Scout offer-quality flags (reversible; set by scripts/clean_offers.py).
            #   cleanup_status: ok | noise | duplicate | thin_body
            #   canonical_ref:  for duplicates, the offer id kept as canonical
            self._ensure_column(conn, "offers", "cleanup_status", "TEXT NOT NULL DEFAULT 'ok'")
            self._ensure_column(conn, "offers", "canonical_ref", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "explorer_runs", "user_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'")
            self._ensure_column(conn, "queue_items", "user_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'")
            self._ensure_column(conn, "sms_reports", "user_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'")
            self._ensure_column(conn, "events", "user_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_user_score ON offers(user_id, score, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user_started ON explorer_runs(user_id, started_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_messages_user_created ON sms_messages(user_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_application_tasks_user_status ON application_tasks(user_id, status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_application_tasks_offer ON application_tasks(offer_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_application_questions_app ON application_questions(application_id, status)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_offers_user_dedupe ON offers(user_id, dedupe_hash)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_application_tasks_user_offer ON application_tasks(user_id, offer_id)")
            # Clean surface for downstream agents (Coach/Forms): real, non-duplicate
            # offers only. thin_body rows stay -- they are real offers whose scraped
            # body is a login/consent wall, flagged so callers know not to trust it.
            conn.execute(
                "CREATE VIEW IF NOT EXISTS offers_clean AS "
                "SELECT * FROM offers WHERE cleanup_status NOT IN ('noise', 'duplicate')"
            )
        self.seed_defaults()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def seed_defaults(self) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, display_name, role, phone, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    role = excluded.role,
                    phone = excluded.phone,
                    updated_at = excluded.updated_at
                """,
                (DEFAULT_USER_ID, "", "Julien", "admin", "+33775857082", now, now),
            )
            for platform in DEFAULT_PLATFORMS:
                conn.execute(
                    """
                    INSERT INTO platforms (id, name, base_url, login_url, kind, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        base_url = excluded.base_url,
                        login_url = excluded.login_url,
                        kind = excluded.kind,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        platform["id"],
                        platform["name"],
                        platform["base_url"],
                        platform["login_url"],
                        platform["kind"],
                        now,
                        now,
                    ),
                )
                self._ensure_user_platform_account(conn, DEFAULT_USER_ID, platform["id"], now)

    def new_id(self, prefix: str = "") -> str:
        for _ in range(20):
            value = f"{prefix}{secrets.token_hex(4)}"
            if not self.any_id_exists(value):
                return value
        return f"{prefix}{secrets.token_hex(8)}"

    def any_id_exists(self, value: str) -> bool:
        with self.connect() as conn:
            for table in ("users", "offers", "explorer_runs", "queue_items", "sms_reports", "sms_messages", "application_tasks", "application_questions"):
                row = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (value,)).fetchone()
                if row:
                    return True
        return False

    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    @staticmethod
    def model_to_dict(obj: Any) -> dict[str, Any]:
        # ORM counterpart of row_to_dict: converted methods keep returning
        # plain dicts so callers never see model objects.
        return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}

    def get_user(self, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self.row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def list_platforms(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM platforms WHERE enabled = 1 ORDER BY name").fetchall()]

    def get_platform(self, platform_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self.row_to_dict(conn.execute("SELECT * FROM platforms WHERE id = ?", (platform_id,)).fetchone())

    def user_platform_accounts(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        now = utc_now()
        with self.connect() as conn:
            for platform in DEFAULT_PLATFORMS:
                self._ensure_user_platform_account(conn, user_id, platform["id"], now)
            rows = conn.execute(
                """
                SELECT a.*, p.name, p.base_url, p.kind
                FROM user_platform_accounts a
                JOIN platforms p ON p.id = a.platform_id
                WHERE a.user_id = ?
                ORDER BY p.name
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_user_platform_account(self, user_id: str, platform_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            self._ensure_user_platform_account(conn, user_id, platform_id, now)
            row = conn.execute(
                """
                SELECT a.*, p.name, p.base_url, p.kind
                FROM user_platform_accounts a
                JOIN platforms p ON p.id = a.platform_id
                WHERE a.user_id = ? AND a.platform_id = ?
                """,
                (user_id, platform_id),
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown platform account: {user_id}/{platform_id}")
        return dict(row)

    def update_user_platform_account(self, user_id: str, platform_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"login_url", "profile_dir", "status", "last_login_check_at", "metadata_json"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [user_id, platform_id]
        with self.connect() as conn:
            self._ensure_user_platform_account(conn, user_id, platform_id, utc_now())
            conn.execute(
                f"UPDATE user_platform_accounts SET {assignments} WHERE user_id = ? AND platform_id = ?",
                values,
            )
        return self.get_user_platform_account(user_id, platform_id)

    def _ensure_user_platform_account(self, conn: sqlite3.Connection, user_id: str, platform_id: str, now: str) -> None:
        platform = conn.execute("SELECT login_url FROM platforms WHERE id = ?", (platform_id,)).fetchone()
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not platform:
            raise KeyError(f"Unknown platform: {platform_id}")
        if not user:
            raise KeyError(f"Unknown user: {user_id}")
        profile_dir = str(Path(".coverai-browser") / "users" / user_id / platform_id)
        conn.execute(
            """
            INSERT INTO user_platform_accounts (
                user_id, platform_id, login_url, profile_dir, status, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'not_connected', '{}', ?, ?)
            ON CONFLICT(user_id, platform_id) DO NOTHING
            """,
            (user_id, platform_id, str(platform["login_url"] or ""), profile_dir, now, now),
        )

    def create_explorer_run(self, config_path: str = "", user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        run_id = self.new_id("run_")
        now = utc_now()
        run = ExplorerRun(id=run_id, user_id=user_id, status="running", config_path=config_path, started_at=now)
        with Session(self.engine) as session:
            session.add(run)
            session.commit()
        return self.get_explorer_run(run_id) or {"id": run_id, "status": "running"}

    def update_explorer_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "completed_at", "offers_found", "offers_new", "offers_reported", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_explorer_run(run_id) or {}
        with Session(self.engine) as session:
            run = session.get(ExplorerRun, run_id)
            if run:
                for key, value in updates.items():
                    setattr(run, key, value)
                session.commit()
        return self.get_explorer_run(run_id) or {}

    def get_explorer_run(self, run_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            run = session.get(ExplorerRun, run_id)
            return self.model_to_dict(run) if run else None

    def latest_explorer_run(self, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        stmt = (
            select(ExplorerRun)
            .where(ExplorerRun.user_id == user_id)
            .order_by(ExplorerRun.started_at.desc())
            .limit(1)
        )
        with Session(self.engine) as session:
            run = session.scalars(stmt).first()
            return self.model_to_dict(run) if run else None

    def mark_stale_explorer_runs(self, reason: str = "server restarted before run completed", user_id: str = DEFAULT_USER_ID) -> int:
        now = utc_now()
        stmt = (
            update(ExplorerRun)
            .where(ExplorerRun.user_id == user_id, ExplorerRun.status == "running")
            .values(status="failed", completed_at=now, error=reason)
        )
        with Session(self.engine) as session:
            result = session.execute(stmt)
            session.commit()
            return int(result.rowcount or 0)

    def upsert_offer(self, offer: dict[str, Any], user_id: str = DEFAULT_USER_ID) -> tuple[dict[str, Any], bool]:
        dedupe = offer.get("dedupe_hash") or offer_dedupe_hash(
            str(offer.get("url") or ""),
            str(offer.get("title") or ""),
            str(offer.get("company") or ""),
            str(offer.get("location") or ""),
            str(offer.get("snippet") or ""),
        )
        now = utc_now()
        existing_id = ""
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM offers WHERE user_id = ? AND dedupe_hash = ?", (user_id, dedupe)).fetchone()
            if existing:
                existing_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE offers
                    SET url = ?, title = ?, company = ?, location = ?, source = ?, raw_text = ?,
                        snippet = ?, score = ?, summary = ?, updated_at = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(offer.get("url") or existing["url"] or ""),
                        str(offer.get("title") or existing["title"] or ""),
                        str(offer.get("company") or existing["company"] or ""),
                        str(offer.get("location") or existing["location"] or ""),
                        str(offer.get("source") or existing["source"] or ""),
                        str(offer.get("raw_text") or existing["raw_text"] or ""),
                        str(offer.get("snippet") or existing["snippet"] or ""),
                        int(offer.get("score") or existing["score"] or 0),
                        str(offer.get("summary") or existing["summary"] or ""),
                        now,
                        now,
                        existing_id,
                    ),
                )
            else:
                offer_id = str(offer.get("id") or self.new_id("off_"))
                conn.execute(
                    """
                    INSERT INTO offers (
                        id, user_id, dedupe_hash, url, title, company, location, source, raw_text,
                        snippet, score, summary, status, cleanup_status, created_at, updated_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        offer_id,
                        user_id,
                        dedupe,
                        str(offer.get("url") or ""),
                        str(offer.get("title") or ""),
                        str(offer.get("company") or ""),
                        str(offer.get("location") or ""),
                        str(offer.get("source") or ""),
                        str(offer.get("raw_text") or ""),
                        str(offer.get("snippet") or ""),
                        int(offer.get("score") or 0),
                        str(offer.get("summary") or ""),
                        str(offer.get("status") or "new"),
                        str(offer.get("cleanup_status") or "ok"),
                        now,
                        now,
                        now,
                    ),
                )
                existing_id = offer_id

        return self.get_offer(existing_id) or {"id": existing_id}, not bool(existing)

    def get_offer(self, offer_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self.row_to_dict(conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone())

    def find_offer_by_reference(self, reference: str, user_id: str = DEFAULT_USER_ID, phone: str = "") -> dict[str, Any] | None:
        ref = " ".join(str(reference or "").lower().split())
        if not ref:
            return self.latest_reported_offer(phone, user_id=user_id) or (self.list_offers(limit=1, user_id=user_id) or [None])[0]
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM offers WHERE id = ? AND user_id = ?", (reference.strip(), user_id)).fetchone()
            if row:
                return dict(row)
            for token in str(reference or "").replace(":", " ").replace(",", " ").split():
                if token.startswith("off_"):
                    row = conn.execute("SELECT * FROM offers WHERE id = ? AND user_id = ?", (token.strip(), user_id)).fetchone()
                    if row:
                        return dict(row)

        if ref in {"this", "this one", "that", "that one", "it", "last", "latest", "the one", "the last one"} or any(
            phrase in ref for phrase in ("this one", "that one", "last one", "latest one", "the one")
        ):
            return self.latest_reported_offer(phone, user_id=user_id) or (self.list_offers(limit=1, user_id=user_id) or [None])[0]

        ordinal = {"first": 0, "1": 0, "second": 1, "2": 1, "third": 2, "3": 2, "fourth": 3, "4": 3, "fifth": 4, "5": 4}.get(ref)
        if ordinal is not None:
            recent = self.recent_reported_offers(phone, limit=5, user_id=user_id)
            if ordinal < len(recent):
                return recent[ordinal]

        candidates = self.recent_reported_offers(phone, limit=10, user_id=user_id) + self.list_offers(limit=50, user_id=user_id)
        best: dict[str, Any] | None = None
        best_score = 0
        words = self.search_words(ref, min_length=3)
        for offer in candidates:
            haystack = " ".join(
                str(offer.get(key) or "").lower()
                for key in ("company", "title", "location", "summary", "source")
            )
            score = sum(1 for word in words if word in haystack)
            company = str(offer.get("company") or "").lower()
            if company and company in ref:
                score += 3
            haystack_words = self.search_words(haystack, min_length=4)
            for word in words:
                if any(SequenceMatcher(None, word, candidate).ratio() >= 0.82 for candidate in haystack_words):
                    score += 2
            if score > best_score:
                best = offer
                best_score = score
        return best if best_score > 0 else None

    @staticmethod
    def search_words(value: str, min_length: int = 3) -> set[str]:
        cleaned = "".join(char.lower() if char.isalnum() else " " for char in str(value or ""))
        return {word for word in cleaned.split() if len(word) >= min_length}

    def list_offers(self, status: str = "", limit: int = 50, min_score: int | None = None, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        where: list[str] = ["user_id = ?"]
        args: list[Any] = [user_id]
        if status:
            where.append("status = ?")
            args.append(status)
        if min_score is not None:
            where.append("score >= ?")
            args.append(int(min_score))
        query = "SELECT * FROM offers"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY score DESC, updated_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, args).fetchall()]

    def mark_offer_status(self, offer_id: str, status: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute("UPDATE offers SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?", (status, now, offer_id, user_id))
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown offer: {offer_id}")
        offer = self.get_offer(offer_id)
        if not offer:
            raise KeyError(f"Unknown offer: {offer_id}")
        self.add_event("offer.status", "offer", offer_id, {"status": status}, user_id=user_id)
        return offer

    def create_queue_item(self, item_type: str, subject_type: str = "", subject_id: str = "", payload: dict[str, Any] | None = None, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        item_id = self.new_id("q_")
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO queue_items (id, user_id, type, status, subject_type, subject_id, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (item_id, user_id, item_type, subject_type, subject_id, json.dumps(payload or {}, ensure_ascii=False), now, now),
            )
        return {"id": item_id, "user_id": user_id, "type": item_type, "status": "queued", "subject_type": subject_type, "subject_id": subject_id, "payload": payload or {}}

    def record_sms_report(self, offer_id: str, number: str, text: str, status: str, response: dict[str, Any] | None = None, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        offer = self.get_offer(offer_id)
        if not offer or offer.get("user_id") != user_id:
            raise KeyError(f"Unknown offer: {offer_id}")
        report_id = self.new_id("sms_")
        now = utc_now()
        response_json = json.dumps(response or {}, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sms_reports (id, user_id, offer_id, number, text, status, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (report_id, user_id, offer_id, number, text, status, response_json, now),
            )
        self.add_event("sms.report", "offer", offer_id, {"report_id": report_id, "status": status}, user_id=user_id)
        return {"id": report_id, "user_id": user_id, "offer_id": offer_id, "number": number, "text": text, "status": status, "response": response or {}, "created_at": now}

    def recent_reported_offers(self, phone: str = "", limit: int = 5, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        where = ["r.user_id = ?", "r.status = 'sent'"]
        args: list[Any] = [user_id]
        if phone:
            where.append("r.number = ?")
            args.append(phone)
        query = (
            "SELECT o.* FROM sms_reports r JOIN offers o ON o.id = r.offer_id "
            "WHERE " + " AND ".join(where) + " ORDER BY r.created_at DESC LIMIT ?"
        )
        args.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, args).fetchall()]

    def latest_reported_offer(self, phone: str = "", user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        offers = self.recent_reported_offers(phone, limit=1, user_id=user_id)
        return offers[0] if offers else None

    def record_sms_message(
        self,
        direction: str,
        phone: str,
        text: str,
        response_text: str = "",
        command: str = "",
        user_id: str = DEFAULT_USER_ID,
    ) -> dict[str, Any]:
        message_id = self.new_id("msg_")
        now = utc_now()
        message = SmsMessage(
            id=message_id,
            user_id=user_id,
            direction=direction,
            phone=phone,
            text=text,
            response_text=response_text,
            command=command,
            created_at=now,
        )
        with Session(self.engine) as session:
            session.add(message)
            session.commit()
        self.add_event(
            "sms.message",
            "sms",
            message_id,
            {"direction": direction, "phone": phone, "command": command},
            user_id=user_id,
        )
        return {
            "id": message_id,
            "user_id": user_id,
            "direction": direction,
            "phone": phone,
            "text": text,
            "response_text": response_text,
            "command": command,
            "created_at": now,
        }

    def recent_sms_messages(self, user_id: str = DEFAULT_USER_ID, phone: str = "", limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        stmt = select(SmsMessage).where(SmsMessage.user_id == user_id)
        if phone:
            stmt = stmt.where(SmsMessage.phone == phone)
        stmt = stmt.order_by(SmsMessage.created_at.desc()).limit(limit)
        with Session(self.engine) as session:
            return [self.model_to_dict(message) for message in session.scalars(stmt)]

    def upsert_application_task(self, offer_id: str, user_id: str = DEFAULT_USER_ID) -> tuple[dict[str, Any], bool]:
        offer = self.get_offer(offer_id)
        if not offer or offer.get("user_id") != user_id:
            raise KeyError(f"Unknown offer: {offer_id}")
        now = utc_now()
        created = False
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM application_tasks WHERE user_id = ? AND offer_id = ?",
                (user_id, offer_id),
            ).fetchone()
            if existing:
                app_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE application_tasks
                    SET company = ?, role_title = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(offer.get("company") or ""), str(offer.get("title") or ""), now, app_id),
                )
            else:
                queue = self.create_queue_item("application.prepare", "offer", offer_id, {"offer_id": offer_id}, user_id=user_id)
                app_id = self.new_id("app_")
                conn.execute(
                    """
                    INSERT INTO application_tasks (
                        id, user_id, offer_id, queue_item_id, company, role_title, status,
                        artifacts_json, strategy_text, last_action, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'preparing', '{}', ?, ?, ?, ?)
                    """,
                    (
                        app_id,
                        user_id,
                        offer_id,
                        queue["id"],
                        str(offer.get("company") or ""),
                        str(offer.get("title") or ""),
                        self.default_application_strategy(offer),
                        "Application task created",
                        now,
                        now,
                    ),
                )
                created = True
        if created:
            self.seed_application_questions(app_id, offer, user_id=user_id)
        app = self.recalculate_application_readiness(app_id, user_id=user_id)
        self.add_event("application.created" if created else "application.updated", "application", app_id, {"offer_id": offer_id}, user_id=user_id)
        return app, created

    @staticmethod
    def default_application_strategy(offer: dict[str, Any]) -> str:
        company = str(offer.get("company") or "the company")
        title = str(offer.get("title") or "this role")
        summary = str(offer.get("summary") or offer.get("snippet") or "")
        return f"Position Julien around embedded systems, C/C++, Linux/RTOS, applied AI, and practical engineering curiosity for {title} at {company}. {summary[:220]}"

    def seed_application_questions(self, application_id: str, offer: dict[str, Any], user_id: str = DEFAULT_USER_ID) -> None:
        seeds = [
            ("CV tailored for this role", "file", 1, "Use the stored CoverAI CV context and tune it to the offer.", "generated", 70, "drafted"),
            ("Cover/application motivation angle", "textarea", 1, self.default_application_strategy(offer), "generated", 75, "drafted"),
            ("Start date / availability", "text", 1, "", "unknown", 0, "needs_user"),
            ("Work authorization / location constraints", "text", 1, "", "unknown", 0, "needs_user"),
            ("Relevant embedded systems project example", "textarea", 1, "", "unknown", 0, "needs_user"),
            ("Platform account/login ready", "checkbox", 0, "", "unknown", 0, "detected"),
        ]
        for label, field_type, required, answer, source, confidence, status in seeds:
            self.create_application_question(
                application_id,
                label,
                field_type=field_type,
                required=bool(required),
                answer=answer,
                answer_source=source,
                confidence=confidence,
                status=status,
                user_id=user_id,
            )

    def create_application_question(
        self,
        application_id: str,
        label: str,
        field_type: str = "text",
        required: bool = True,
        answer: str = "",
        answer_source: str = "unknown",
        confidence: int = 0,
        status: str = "detected",
        user_id: str = DEFAULT_USER_ID,
    ) -> dict[str, Any]:
        question_id = self.new_id("aq_")
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO application_questions (
                    id, user_id, application_id, label, field_type, required, answer,
                    answer_source, confidence, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    user_id,
                    application_id,
                    label,
                    field_type,
                    1 if required else 0,
                    answer,
                    answer_source,
                    max(0, min(int(confidence), 100)),
                    status,
                    now,
                    now,
                ),
            )
        return self.get_application_question(question_id) or {"id": question_id}

    def get_application_task(self, application_id: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self.row_to_dict(
                conn.execute("SELECT * FROM application_tasks WHERE id = ? AND user_id = ?", (application_id, user_id)).fetchone()
            )

    def get_application_for_offer(self, offer_id: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self.row_to_dict(
                conn.execute("SELECT * FROM application_tasks WHERE offer_id = ? AND user_id = ?", (offer_id, user_id)).fetchone()
            )

    def list_application_tasks(self, status: str = "", limit: int = 20, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        where = ["user_id = ?"]
        args: list[Any] = [user_id]
        if status:
            where.append("status = ?")
            args.append(status)
        query = "SELECT * FROM application_tasks WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, args).fetchall()]

    def get_application_question(self, question_id: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self.row_to_dict(
                conn.execute("SELECT * FROM application_questions WHERE id = ? AND user_id = ?", (question_id, user_id)).fetchone()
            )

    def list_application_questions(self, application_id: str, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM application_questions WHERE application_id = ? AND user_id = ? ORDER BY created_at",
                    (application_id, user_id),
                ).fetchall()
            ]

    @staticmethod
    def application_field_key(label: str) -> str:
        key = "".join(char.lower() if char.isalnum() else "_" for char in str(label or ""))
        key = "_".join(part for part in key.split("_") if part)
        return key or "field"

    @staticmethod
    def application_answer_is_ready(question: dict[str, Any]) -> bool:
        status = str(question.get("status") or "")
        answer = str(question.get("answer") or "").strip()
        confidence = int(question.get("confidence") or 0)
        return bool(answer) and status in {"drafted", "confirmed", "filled"} and confidence >= 60

    def application_question_payload(self, question: dict[str, Any]) -> dict[str, Any]:
        answer = str(question.get("answer") or "")
        ready = self.application_answer_is_ready(question)
        return {
            "question_id": question.get("id"),
            "field_key": self.application_field_key(str(question.get("label") or "")),
            "label": question.get("label"),
            "field_type": question.get("field_type"),
            "required": bool(question.get("required")),
            "answer": answer,
            "answer_source": question.get("answer_source"),
            "confidence": int(question.get("confidence") or 0),
            "status": question.get("status"),
            "ready_for_injection": ready,
        }

    def application_submission_packet(self, application_id: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        app = self.recalculate_application_readiness(application_id, user_id=user_id)
        offer = self.get_offer(str(app.get("offer_id") or ""))
        questions = self.list_application_questions(application_id, user_id=user_id)
        answers = [self.application_question_payload(question) for question in questions]
        ready_answers = [answer for answer in answers if answer["ready_for_injection"]]
        missing_required = [answer for answer in answers if answer["required"] and not answer["ready_for_injection"]]
        low_confidence = [
            answer for answer in answers
            if answer["required"] and answer["answer"] and int(answer["confidence"] or 0) < 60
        ]
        artifacts: dict[str, Any] = {}
        try:
            artifacts = json.loads(str(app.get("artifacts_json") or "{}"))
        except json.JSONDecodeError:
            artifacts = {}
        ready_for_form_fill = not missing_required
        return {
            "application": app,
            "offer": offer,
            "ready_for_form_fill": ready_for_form_fill,
            "ready_for_submission_review": ready_for_form_fill,
            "ready_to_submit": False,
            "submission_blockers": ["missing_required_fields"] if missing_required else ["human_review_required"],
            "readiness": {
                "percent": app.get("readiness_percent"),
                "required_total": app.get("questions_total"),
                "required_ready": app.get("questions_answered"),
                "required_missing": len(missing_required),
                "low_confidence": len(low_confidence),
            },
            "answers": answers,
            "ready_answers": ready_answers,
            "missing_required": missing_required,
            "low_confidence": low_confidence,
            "artifacts": artifacts,
            "playwright_payload": {
                "application_id": app.get("id"),
                "offer_id": app.get("offer_id"),
                "target_url": offer.get("url") if offer else "",
                "company": app.get("company"),
                "role_title": app.get("role_title"),
                "fields": [
                    {
                        "field_key": answer["field_key"],
                        "label": answer["label"],
                        "field_type": answer["field_type"],
                        "value": answer["answer"],
                        "source": answer["answer_source"],
                        "confidence": answer["confidence"],
                    }
                    for answer in ready_answers
                ],
                "artifacts": artifacts,
            },
        }

    def update_application_question(self, question_id: str, user_id: str = DEFAULT_USER_ID, **fields: Any) -> dict[str, Any]:
        allowed = {"answer", "answer_source", "confidence", "status"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if "confidence" in updates:
            updates["confidence"] = max(0, min(int(updates["confidence"]), 100))
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [question_id, user_id]
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE application_questions SET {assignments} WHERE id = ? AND user_id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown application question: {question_id}")
            row = conn.execute("SELECT application_id FROM application_questions WHERE id = ?", (question_id,)).fetchone()
            application_id = str(row["application_id"]) if row else ""
        if application_id:
            self.recalculate_application_readiness(application_id, user_id=user_id)
        question = self.get_application_question(question_id, user_id=user_id)
        if not question:
            raise KeyError(f"Unknown application question: {question_id}")
        return question

    def answer_next_application_question(self, application_id: str, answer: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM application_questions
                WHERE application_id = ? AND user_id = ? AND status = 'needs_user'
                ORDER BY created_at LIMIT 1
                """,
                (application_id, user_id),
            ).fetchone()
        if not row:
            return None
        return self.update_application_question(
            str(row["id"]),
            user_id=user_id,
            answer=answer,
            answer_source="user_sms",
            confidence=100,
            status="confirmed",
        )

    def recalculate_application_readiness(self, application_id: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        questions = self.list_application_questions(application_id, user_id=user_id)
        required = [question for question in questions if int(question.get("required") or 0)]
        answered_required = [
            question for question in required
            if str(question.get("status") or "") in {"drafted", "confirmed", "filled"} and int(question.get("confidence") or 0) >= 60
        ]
        needs_user = [question for question in required if str(question.get("status") or "") == "needs_user"]
        low_confidence = [
            question for question in required
            if str(question.get("answer") or "") and int(question.get("confidence") or 0) < 60
        ]
        total = len(required)
        answered = len(answered_required)
        percent = int(round((answered / total) * 100)) if total else 0
        status = "ready_to_fill" if total and answered == total else ("needs_user" if needs_user else "preparing")
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE application_tasks
                SET status = ?, readiness_percent = ?, questions_total = ?, questions_answered = ?,
                    questions_needs_user = ?, questions_low_confidence = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (status, percent, total, answered, len(needs_user), len(low_confidence), now, application_id, user_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown application task: {application_id}")
        app = self.get_application_task(application_id, user_id=user_id)
        if not app:
            raise KeyError(f"Unknown application task: {application_id}")
        return app

    def add_event(self, event_type: str, subject_type: str = "", subject_id: str = "", payload: dict[str, Any] | None = None, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        now = utc_now()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        event = Event(
            user_id=user_id,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload_json=payload_json,
            created_at=now,
        )
        with Session(self.engine) as session:
            session.add(event)
            session.commit()
            event_id = event.id
        return {"id": event_id, "user_id": user_id, "event_type": event_type, "subject_type": subject_type, "subject_id": subject_id, "payload": payload or {}, "created_at": now}
