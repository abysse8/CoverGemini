"""CV renderer (Marie / coverai.forms) -- stages 2 and 3 of the CV pipeline.

The CV pipeline has three stages:

    1. offer text  --(model)-->  semantic JSON   (non-deterministic; NOT here)
    2. semantic JSON --> CV.tex  (deterministic LaTeX render)
    3. CV.tex --(pdflatex)--> CV.pdf

Marie owns stages 2 and 3. Stage 1 -- writing the CV prose -- is deliberately
excluded: it is non-deterministic and would violate Marie's rule ("never invent
field values"). This module is handed `sections` (the OUTPUT_SCHEMA shape from
server.py: company, role_title, objective, apl_items, skills, letter, notes) and
only *renders* them, then wraps the result as a contract artifact-ref
(contracts/artifact-ref.schema.json) that the submission packet attaches as its
`cv_upload` field.

Stage 2 has no dependencies and always runs. Stage 3 needs a LaTeX toolchain
(`pdflatex`); when that is absent the renderer degrades honestly -- it returns the
CV.tex as the artifact and marks `pdf_status: "pending_compile"` in metadata,
rather than pretending a PDF exists. On a host with LaTeX installed the same call
produces a real PDF artifact. Stdlib only apart from the optional pdflatex
subprocess.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from main import build_cv_tex

from .storage import DEFAULT_USER_ID, utc_now

# Files the LaTeX template references (\includegraphics{photo.jpg}, logos). They
# live at the CoverGemini repo root; we copy whichever exist next to CV.tex before
# compiling so pdflatex can find them. Missing assets are simply skipped.
_TEMPLATE_ASSETS = ("photo.jpg", "logo_cefipa.png", "logo_cesi.png")
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _slugify(text: str) -> str:
    """Lowercase, keep [a-z0-9], collapse the rest to single dashes."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "cv"


def _artifact_id(offer_ref: str, sections: dict[str, Any]) -> str:
    """Deterministic id so the same offer renders to a stable artifact id.

    Prefer the stable offer_ref (e.g. 'offer:off_123'); fall back to the company
    name so a fixture with no offer_ref still gets a sensible, repeatable id.
    """
    basis = offer_ref or str(sections.get("company") or "")
    return "art_cv_" + _slugify(basis)


def _artifact_ref(
    *,
    artifact_id: str,
    kind: str,
    path: Path,
    user_id: str,
    sections: dict[str, Any],
    created_at: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Shape a rendered file into a contracts/artifact-ref.schema.json object."""
    company = str(sections.get("company") or "")
    role = str(sections.get("role_title") or "")
    title = f"CV -- {company} / {role}".strip(" -/") or "CV"
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "title": title,
        "owner_user_id": user_id,
        "storage_ref": path.resolve().as_uri(),
        "content_type": "application/pdf" if kind == "pdf" else "application/x-tex",
        "size_bytes": path.stat().st_size,
        "created_at": created_at,
        "metadata": metadata,
    }


def _compile_pdf(out_dir: Path) -> Path | None:
    """Stage 3: run pdflatex twice on CV.tex, return CV.pdf or None.

    Copies the template's image assets in first. Returns None -- never raises --
    when pdflatex is absent (FileNotFoundError) or the compile fails, so the caller
    can degrade to the .tex artifact.
    """
    for name in _TEMPLATE_ASSETS:
        src = _REPO_ROOT / name
        if src.exists():
            shutil.copy(src, out_dir / name)

    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "CV.tex"]
    try:
        # Twice: LaTeX needs a second pass to resolve references/layout.
        subprocess.run(cmd, cwd=out_dir, capture_output=True)
        subprocess.run(cmd, cwd=out_dir, capture_output=True)
    except FileNotFoundError:
        return None  # no LaTeX toolchain on this host

    pdf = out_dir / "CV.pdf"
    return pdf if pdf.exists() else None


def render_cv(
    sections: dict[str, Any],
    *,
    out_dir: str | Path,
    user_id: str = DEFAULT_USER_ID,
    offer_ref: str = "",
    artifact_id: str | None = None,
    created_at: str | None = None,
    run_pdflatex: bool = True,
) -> dict[str, Any]:
    """Render semantic `sections` to a CV artifact-ref.

    Always writes CV.tex (deterministic). Attempts a PDF when run_pdflatex is True
    and a toolchain exists; otherwise returns the .tex artifact with
    pdf_status="pending_compile". The returned dict validates against
    contracts/artifact-ref.schema.json.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    created_at = created_at or utc_now()
    aid = artifact_id or _artifact_id(offer_ref, sections)

    # Stage 2 -- deterministic, no dependency.
    company = str(sections.get("company") or "")
    role = str(sections.get("role_title") or "")
    cv_tex = build_cv_tex(sections, company, role)
    tex_path = out / "CV.tex"
    tex_path.write_text(cv_tex, encoding="utf-8")

    # Stage 3 -- optional, degrades honestly.
    pdf_path = _compile_pdf(out) if run_pdflatex else None

    base_meta = {"offer_ref": offer_ref, "source": "cv_render"}
    if pdf_path is not None:
        return _artifact_ref(
            artifact_id=aid, kind="pdf", path=pdf_path, user_id=user_id,
            sections=sections, created_at=created_at,
            metadata={**base_meta, "tex_ref": tex_path.resolve().as_uri()},
        )
    return _artifact_ref(
        artifact_id=aid, kind="text", path=tex_path, user_id=user_id,
        sections=sections, created_at=created_at,
        metadata={
            **base_meta,
            "pdf_status": "pending_compile",
            "reason": "pdflatex unavailable" if run_pdflatex else "pdflatex skipped",
        },
    )
