"""SQLAlchemy models mirroring the CoverAI schema.

This file is the schema written down once, as Python classes, instead of
being scattered across CREATE TABLE strings in storage.py. It is step 1 of
the SQLAlchemy migration: models only, no runtime behavior change. Nothing
imports this module yet; storage.py still runs on raw sqlite3.

Deliberate mirroring choices, to stay byte-compatible with existing data:

- Timestamps stay TEXT (ISO-8601 strings from storage.utc_now()), not
  DateTime, so values written by the old code read back identically.
- Boolean-ish flags (required, enabled) stay Integer 0/1, exactly as the
  current tables store them.
- server_default (a DEFAULT baked into the table definition, applied by
  SQLite itself) is used instead of Python-side defaults, matching the
  existing CREATE TABLE blocks.

Not represented here: the offers_clean VIEW (views are queries, not tables;
it moves into the Alembic baseline in step 2) and sqlite_sequence (SQLite
internal bookkeeping for AUTOINCREMENT).
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_USER_ID = "julien"


class Base(DeclarativeBase):
    """Collects every model's table definition into one metadata registry."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    display_name: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    role: Mapped[str] = mapped_column(String, nullable=False, server_default="user")
    phone: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class UserProfile(Base):
    """Reusable identity fields that every application form asks for.

    Separate from `users` (which is auth-ish: role, display_name) because this is
    the application-facing profile -- the values Helene's autofill types into a
    real form: name, email, phone, city, country, and public profile links. One
    row per user (user_id is both PK and FK). This is the future home of Louise
    (memory.profile); for now it is a plain table the packet producer reads.

    Column names match the frozen LOGICAL_FIELDS vocabulary in browser_apply.py
    (first_name, location_city, ...) so the producer is a direct copy, no rename.
    """

    __tablename__ = "user_profile"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    first_name: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    last_name: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    email: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    phone: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    location_city: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    location_country: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    linkedin_url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    portfolio_url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Platform(Base):
    __tablename__ = "platforms"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    login_url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    kind: Mapped[str] = mapped_column(String, nullable=False, server_default="job_board")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class UserPlatformAccount(Base):
    __tablename__ = "user_platform_accounts"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    platform_id: Mapped[str] = mapped_column(ForeignKey("platforms.id"), primary_key=True)
    login_url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    profile_dir: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="not_connected")
    last_login_check_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        Index("idx_offers_user_score", "user_id", "score", "updated_at"),
        Index("idx_offers_user_dedupe", "user_id", "dedupe_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    dedupe_hash: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    title: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    company: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    location: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    snippet: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="new")
    # Scout offer-quality flags: ok | noise | duplicate | thin_body.
    cleanup_status: Mapped[str] = mapped_column(String, nullable=False, server_default="ok")
    # For duplicates, the offer id kept as canonical.
    canonical_ref: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String, nullable=False)


class ExplorerRun(Base):
    __tablename__ = "explorer_runs"
    __table_args__ = (Index("idx_runs_user_started", "user_id", "started_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    config_path: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    completed_at: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    offers_found: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    offers_new: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    offers_reported: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class QueueItem(Base):
    __tablename__ = "queue_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    subject_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class SmsReport(Base):
    __tablename__ = "sms_reports"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), nullable=False)
    number: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    response_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class SmsMessage(Base):
    __tablename__ = "sms_messages"
    __table_args__ = (Index("idx_sms_messages_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    direction: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    command: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ApplicationTask(Base):
    __tablename__ = "application_tasks"
    __table_args__ = (
        Index("idx_application_tasks_user_status", "user_id", "status", "updated_at"),
        Index("idx_application_tasks_offer", "offer_id"),
        Index("idx_application_tasks_user_offer", "user_id", "offer_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), nullable=False)
    queue_item_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    company: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    role_title: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="preparing")
    readiness_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    questions_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    questions_answered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    questions_needs_user: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    questions_low_confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    strategy_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    last_action: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ApplicationQuestion(Base):
    __tablename__ = "application_questions"
    __table_args__ = (Index("idx_application_questions_app", "application_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    application_id: Mapped[str] = mapped_column(ForeignKey("application_tasks.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    field_type: Mapped[str] = mapped_column(String, nullable=False, server_default="text")
    required: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    answer: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    answer_source: Mapped[str] = mapped_column(String, nullable=False, server_default="unknown")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="detected")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class InterviewQuestion(Base):
    """A likely interview question for a specific offer, in the Helene->Camille seam.

    Helene (browser) COLLECTS the question from a job board and stores it (status
    'collected', source names where it came from). Camille (coach) then drafts a
    job-specific suggested_answer with AI (status 'coached'). The user refines it
    into `answer` (status 'answered'). One row per (offer, question).

    Deliberately separate from application_questions: those are form fields to
    submit; these are interview prep. Different lifecycle, different owner.
    """

    __tablename__ = "interview_questions"
    __table_args__ = (Index("idx_interview_questions_offer", "offer_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, server_default="general")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="unknown")
    suggested_answer: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    answer: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    answer_source: Mapped[str] = mapped_column(String, nullable=False, server_default="unknown")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="collected")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Event(Base):
    __tablename__ = "events"
    # sqlite_autoincrement reproduces the AUTOINCREMENT keyword the current
    # table uses (ids are never reused, even after deletes).
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, server_default=DEFAULT_USER_ID
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    subject_type: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    subject_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
