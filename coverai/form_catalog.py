"""A growing catalog of real application-form fields.

Purpose: freeze the submission vocabulary from EVIDENCE, not one form. Every successful
scan (scan_form / resolve_apply_target / Easy Apply modal) is recorded here; aggregating
across many forms shows which fields are universal (name, email) vs. rare (salary, EEO
questions), so Marie can set the vocabulary from the real distribution.

Owned by Helene (browser.apply). Stdlib only (sqlite3) -- its own DB file, separate from
coverai.db, so it never collides with the app's schema.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = "form_catalog.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS form_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_ref TEXT NOT NULL DEFAULT '',
    ats TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    captcha_blocked INTEGER NOT NULL DEFAULT 0,
    field_count INTEGER NOT NULL DEFAULT 0,
    scanned_at TEXT NOT NULL,
    UNIQUE(url, offer_ref)
);
CREATE TABLE IF NOT EXISTS form_fields (
    scan_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    norm_label TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    tag TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    dom_id TEXT NOT NULL DEFAULT '',
    selector TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (scan_id) REFERENCES form_scans(id)
);
CREATE INDEX IF NOT EXISTS idx_fields_scan ON form_fields(scan_id);
CREATE INDEX IF NOT EXISTS idx_fields_norm ON form_fields(norm_label, type);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_label(label: str) -> str:
    """Collapse a field label to a comparable key: lowercase, strip '*', squeeze spaces."""
    s = (label or "").lower().replace("*", " ").strip()
    return re.sub(r"\s+", " ", s)


class FormCatalog:
    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def record(self, scan: dict[str, Any], offer_ref: str = "", source: str = "") -> int:
        """Store one scanned form and its controls. Idempotent on (url, offer_ref)."""
        url = scan.get("final_url") or scan.get("requested_url") or ""
        controls = scan.get("controls", [])
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO form_scans (offer_ref, ats, url, source, captcha_blocked, field_count, scanned_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(url, offer_ref) DO UPDATE SET
                     ats=excluded.ats, captcha_blocked=excluded.captcha_blocked,
                     field_count=excluded.field_count, scanned_at=excluded.scanned_at
                   RETURNING id""",
                (offer_ref, scan.get("ats", ""), url, source,
                 int(bool(scan.get("captcha_detected"))), len(controls), _utc_now()),
            )
            scan_id = cur.fetchone()[0]
            conn.execute("DELETE FROM form_fields WHERE scan_id = ?", (scan_id,))
            conn.executemany(
                """INSERT INTO form_fields
                   (scan_id, label, norm_label, type, tag, name, dom_id, selector, required, options_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(scan_id, c.get("label", ""), normalize_label(c.get("label", "")),
                  c.get("type", ""), c.get("tag", ""), c.get("name", ""), c.get("id", ""),
                  c.get("selector", ""), int(bool(c.get("required"))),
                  json.dumps(c.get("options", []), ensure_ascii=False))
                 for c in controls],
            )
            conn.commit()
            return scan_id

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            scans = conn.execute("SELECT COUNT(*) n, SUM(captcha_blocked) b FROM form_scans").fetchone()
            by_ats = conn.execute(
                "SELECT ats, COUNT(*) n FROM form_scans GROUP BY ats ORDER BY n DESC").fetchall()
            return {"scans": scans["n"], "captcha_blocked": scans["b"] or 0,
                    "by_ats": {r["ats"] or "?": r["n"] for r in by_ats}}

    def field_frequency(self, min_forms: int = 1) -> list[dict[str, Any]]:
        """Distinct (normalized label, type) across forms: how many forms, how often required."""
        total = self.stats()["scans"] or 1
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT norm_label, type,
                          COUNT(DISTINCT scan_id) forms,
                          SUM(required) req
                   FROM form_fields
                   WHERE norm_label != ''
                   GROUP BY norm_label, type
                   HAVING forms >= ?
                   ORDER BY forms DESC, req DESC""",
                (min_forms,),
            ).fetchall()
        return [{"label": r["norm_label"], "type": r["type"], "forms": r["forms"],
                 "pct_of_forms": round(100 * r["forms"] / total),
                 "required_in": r["req"]} for r in rows]


def ingest_scan_file(path: str | Path, catalog: FormCatalog, offer_ref: str = "", source: str = "") -> int:
    scan = json.loads(Path(path).read_text())
    return catalog.record(scan, offer_ref=offer_ref, source=source or Path(path).stem)
