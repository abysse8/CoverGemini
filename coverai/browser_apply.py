"""Browser Apply agent (Helene) -- read-only form evidence + autofill mapping.

This module is deliberately SAFE by construction:

  * scan_form()        loads a page and reports every form control. It never types,
                       clicks submit, or mutates the page.
  * prepare_autofill() overlays a reviewed submission packet onto a scan and returns
                       a field-mapping report (logical field -> selector -> value).
                       It plans fills; it does not perform them.

Nothing here touches a live form's values. Actual injection (WP-H4) will live in a
separate function gated on packet['approved_for_autofill'] == True, and final submit
(WP-H5) behind a second approval. Keeping the read-only core isolated means the risky
paths are small and auditable.

The field vocabulary below is the WP-H2 seam with Marie (coverai.forms). It is v0 and
bilingual (fr/en) because the current offer pipeline is HelloWork-dominated (French).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .platforms import absolute_profile_dir, playwright_available


# --- WP-H2: logical field vocabulary (submission-packet seam) --------------------
#
# Each logical field maps to keyword hints matched against a control's label, name,
# id, and placeholder (all lowercased). Keep this list minimal: a field earns a slot
# only when a real submission page demands it. Extend it from scan evidence, not guesses.
LOGICAL_FIELDS: dict[str, dict[str, Any]] = {
    "full_name":          {"kind": "text",     "hints": ["full name", "nom complet", "your name", "votre nom"]},
    "first_name":         {"kind": "text",     "hints": ["first name", "prénom", "prenom", "given name"]},
    "last_name":          {"kind": "text",     "hints": ["last name", "surname", "nom de famille", "nom"]},
    "email":              {"kind": "text",     "hints": ["email", "e-mail", "courriel", "adresse mail"]},
    "phone":              {"kind": "text",     "hints": ["phone", "téléphone", "telephone", "portable", "mobile"]},
    "location":           {"kind": "text",     "hints": ["city", "ville", "location", "localisation", "adresse"]},
    "linkedin_url":       {"kind": "text",     "hints": ["linkedin"]},
    "portfolio_url":      {"kind": "text",     "hints": ["portfolio", "website", "site web", "github"]},
    "cv_upload":          {"kind": "file",     "hints": ["cv", "resume", "résumé", "curriculum"]},
    "cover_letter_upload":{"kind": "file",     "hints": ["cover letter", "lettre de motivation", "motivation letter"]},
    "motivation":         {"kind": "textarea", "hints": ["motivation", "cover letter", "message", "pourquoi", "why", "lettre"]},
    "start_date":         {"kind": "text",     "hints": ["start date", "date de début", "disponibilité", "availability", "disponible"]},
    "work_authorization": {"kind": "text",     "hints": ["work authorization", "autorisation de travail", "visa", "permit", "titre de séjour"]},
    "salary_expectation": {"kind": "text",     "hints": ["salary", "salaire", "prétentions", "pretentions", "rémunération"]},
    "notice_period":      {"kind": "text",     "hints": ["notice period", "préavis", "preavis"]},
    "consent_gdpr":       {"kind": "checkbox", "hints": ["consent", "consentement", "rgpd", "gdpr", "privacy", "politique de confidentialité"]},
}

# Known ATS / apply hosts, so evidence can tell us what we are really dealing with.
KNOWN_ATS = {
    "workday": "myworkdayjobs.com",
    "taleo": "taleo.net",
    "smartrecruiters": "smartrecruiters.com",
    "lever": "jobs.lever.co",
    "greenhouse": "greenhouse.io",
    "successfactors": "successfactors.com",
    "icims": "icims.com",
    "teamtailor": "teamtailor.com",
    "welcometothejungle": "welcometothejungle.com",
    "hellowork": "hellowork.com",
    "linkedin": "linkedin.com",
    "apec": "apec.fr",
    "jobteaser": "jobteaser.com",
}


# JS run inside the page to enumerate form controls with a best-effort label + selector.
# Kept in the page context because label resolution (for=, ancestor <label>, aria-*,
# preceding text) is far simpler against the live DOM than reconstructed server-side.
_SCAN_JS = r"""
() => {
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 5) {
      let sel = node.tagName.toLowerCase();
      const sibs = node.parentNode ? Array.from(node.parentNode.children).filter(c => c.tagName === node.tagName) : [];
      if (sibs.length > 1) sel += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      parts.unshift(sel);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const anc = el.closest('label');
    if (anc && anc.innerText.trim()) return anc.innerText.trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = document.getElementById(lb); if (t && t.innerText.trim()) return t.innerText.trim(); }
    // fall back to nearest preceding text node in the same container
    let p = el.previousElementSibling;
    while (p) { if (p.innerText && p.innerText.trim()) return p.innerText.trim().slice(0, 120); p = p.previousElementSibling; }
    return '';
  };
  const out = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    const rect = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      type,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      selector: cssPath(el),
      label: labelFor(el),
      placeholder: el.getAttribute('placeholder') || '',
      required: el.required || el.getAttribute('aria-required') === 'true',
      visible: rect.width > 0 && rect.height > 0,
      options: el.tagName.toLowerCase() === 'select'
        ? Array.from(el.options).map(o => o.text.trim()).slice(0, 25) : [],
    });
  }
  // Candidate "apply" affordances so we can trace posting -> form.
  const applyLinks = [];
  for (const a of document.querySelectorAll('a, button')) {
    const t = (a.innerText || '').trim().toLowerCase();
    if (!t) continue;
    if (['apply', 'postuler', 'candidater', 'je postule', 'apply now'].some(k => t.includes(k))) {
      applyLinks.push({ text: (a.innerText || '').trim().slice(0, 60), href: a.getAttribute('href') || '' });
    }
  }
  return { controls: out, applyLinks: applyLinks.slice(0, 10), title: document.title, url: location.href };
}
"""


def _detect_ats(url: str) -> str:
    low = url.lower()
    for name, host in KNOWN_ATS.items():
        if host in low:
            return name
    return "unknown"


def scan_form(
    url: str,
    profile_dir: str | Path | None = None,
    base_dir: str | Path = ".",
    headless: bool = True,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Load `url` read-only and report its form controls and apply affordances.

    If `profile_dir` is given, a persistent (logged-in) Chromium context is used so
    forms behind auth are visible. Never types or submits.
    """
    if not playwright_available():
        return {"url": url, "error": "playwright_missing", "controls": []}

    from playwright.sync_api import sync_playwright

    result: dict[str, Any] = {"requested_url": url}
    with sync_playwright() as pw:
        if profile_dir is not None:
            profile_path = absolute_profile_dir(base_dir, str(profile_dir))
            profile_path.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(str(profile_path), headless=headless)
            page = context.pages[0] if context.pages else context.new_page()
            closer = context
        else:
            browser = pw.chromium.launch(headless=headless)
            page = browser.new_page()
            closer = browser
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
            data = page.evaluate(_SCAN_JS)
            result.update(data)
            result["final_url"] = page.url
            result["ats"] = _detect_ats(page.url)
        except Exception as error:  # noqa: BLE001 -- evidence tool: report, don't raise
            result["error"] = str(error)
            result.setdefault("controls", [])
        finally:
            closer.close()
    return result


def _kind_ok(logical_kind: str, control: dict[str, Any]) -> bool:
    """Reject cross-kind matches (e.g. a file field claiming a textarea)."""
    ctype = control["type"]
    if logical_kind == "file":
        return ctype == "file"
    if logical_kind == "textarea":
        return control["tag"] == "textarea"
    if logical_kind == "checkbox":
        return ctype in ("checkbox", "radio")
    # "text" logical fields accept any single-line text-like input
    return ctype not in ("file", "checkbox", "radio") and control["tag"] != "textarea"


_APPLY_TEXTS = [
    "candidature simplifiée", "easy apply", "postuler sur le site",
    "je postule", "postuler", "candidater", "apply",
]


def _looks_like_application(controls: list[dict[str, Any]]) -> bool:
    """Heuristic: does this control set resemble a real application form (not site chrome)?

    Application forms carry personal-data inputs (email + name/file/motivation), whereas
    aggregator chrome is search boxes and contract filters. We require an email-ish field
    plus at least one of name / file-upload / long-text.
    """
    # An auth wall (login OR signup) also carries name+email, so exclude it first:
    # a password field, or LinkedIn/APEC session markers, means we are NOT on the form yet.
    blob = " ".join((c["label"] + " " + c["name"] + " " + c["id"]).lower() for c in controls)
    if any(c["type"] == "password" for c in controls):
        return False
    if any(m in blob for m in ("session_key", "mot de passe", "sign in", "connexion", "s'identifier")):
        return False
    has_email = any(c["type"] == "email" or "mail" in (c["label"] + c["name"]).lower() for c in controls)
    has_identity = any(
        c["type"] == "file" or c["tag"] == "textarea"
        or any(h in (c["label"] + c["name"] + c["id"]).lower() for h in ("nom", "name", "prénom", "prenom"))
        for c in controls
    )
    return has_email and has_identity


def resolve_apply_target(
    url: str,
    profile_dir: str | Path | None = None,
    base_dir: str | Path = ".",
    headless: bool = True,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Follow a posting's apply affordance to the real application form and scan it.

    Handles three handoff shapes: same-tab navigation, a popup/new tab (aggregators that
    open "the recruiter's site"), and an in-page modal that reveals the form. Read-only:
    it clicks the apply control to REACH the form but never fills or submits.

    Returns the navigation chain, the final ATS host, and a form scan of the destination.
    """
    if not playwright_available():
        return {"requested_url": url, "error": "playwright_missing"}

    from playwright.sync_api import sync_playwright

    result: dict[str, Any] = {"requested_url": url, "chain": [], "clicked": None}
    with sync_playwright() as pw:
        if profile_dir is not None:
            profile_path = absolute_profile_dir(base_dir, str(profile_dir))
            profile_path.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(str(profile_path), headless=headless)
            page = context.pages[0] if context.pages else context.new_page()
        else:
            context = pw.chromium.launch(headless=headless)
            page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
            result["chain"].append(page.url)
            controls_before = len(page.evaluate(_SCAN_JS)["controls"])

            # Find the apply control by visible text (first match wins).
            target = None
            for text in _APPLY_TEXTS:
                loc = page.locator(
                    f"a:has-text('{text}'), button:has-text('{text}')"
                ).filter(visible=True)
                if loc.count() > 0:
                    target = loc.first
                    result["clicked"] = text
                    break
            if target is None:
                result["error"] = "no_apply_affordance_found"
                result["scan"] = scan_current(page)
                return result

            # Click, racing three outcomes: popup, navigation, or in-page modal.
            dest_page = page
            try:
                with context.expect_page(timeout=6000) as popup_info:
                    target.click()
                dest_page = popup_info.value  # a new tab opened
                dest_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 -- no popup; same-tab nav or modal
                page.wait_for_timeout(2500)
                dest_page = page
            dest_page.wait_for_timeout(1500)

            result["chain"].append(dest_page.url)
            scan = scan_current(dest_page)
            controls_after = len(scan.get("controls", []))
            result["scan"] = scan
            result["form_reached"] = _looks_like_application(scan.get("controls", []))
            result["control_delta"] = controls_after - controls_before
            result["ats"] = _detect_ats(dest_page.url)
        except Exception as error:  # noqa: BLE001 -- evidence tool: report, don't raise
            result["error"] = str(error)
        finally:
            context.close()
    return result


def scan_current(page: Any) -> dict[str, Any]:
    """Run the read-only control enumeration against an already-open page."""
    data = page.evaluate(_SCAN_JS)
    data["final_url"] = page.url
    data["ats"] = _detect_ats(page.url)
    return data


def _match_field(control: dict[str, Any], hints: list[str]) -> int:
    """Cheap keyword score: how many hints appear in the control's text signals."""
    hay = " ".join(
        str(control.get(k, "")).lower()
        for k in ("label", "name", "id", "placeholder")
    )
    return sum(1 for h in hints if h in hay)


def map_fields(scan: dict[str, Any]) -> dict[str, Any]:
    """Map logical field names onto scanned controls. Read-only planning."""
    controls = scan.get("controls", [])
    mapping: dict[str, Any] = {}
    used: set[str] = set()
    for logical, spec in LOGICAL_FIELDS.items():
        best, best_score = None, 0
        for c in controls:
            if c["selector"] in used or not _kind_ok(spec["kind"], c):
                continue
            score = _match_field(c, spec["hints"])
            if score > best_score:
                best, best_score = c, score
        if best and best_score > 0:
            used.add(best["selector"])
            mapping[logical] = {
                "selector": best["selector"],
                "control_type": best["type"],
                "label": best["label"],
                "required": best["required"],
                "confidence": "high" if best_score >= 2 else "low",
            }
    unmapped = [c for c in controls if c["selector"] not in used]
    return {"mapped": mapping, "unmapped_controls": unmapped}


def prepare_autofill(packet: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
    """Overlay a submission packet onto a form scan -> a fill plan. Performs no fills.

    Honors the contract: does NOT expose sensitive values, and flags whether the
    packet is even cleared for autofill. Actual injection is a separate, gated step.
    """
    field_map = map_fields(scan)
    mapped = field_map["mapped"]
    packet_fields = {f["name"]: f for f in packet.get("fields", [])}

    plan, missing_on_form, missing_in_packet = [], [], []
    for logical, target in mapped.items():
        pf = packet_fields.get(logical)
        if pf is None:
            missing_in_packet.append(logical)
            continue
        entry = {
            "logical": logical,
            "selector": target["selector"],
            "control_type": target["control_type"],
            "status": pf.get("status"),
            "sensitive": bool(pf.get("sensitive", False)),
        }
        # Contract: never echo sensitive values into a report/log.
        entry["value_preview"] = "***" if entry["sensitive"] else pf.get("value", "")
        plan.append(entry)

    for logical in packet_fields:
        if logical not in mapped:
            missing_on_form.append(logical)

    return {
        "offer_ref": packet.get("offer_ref"),
        "target_url": scan.get("final_url") or scan.get("requested_url"),
        "ats": scan.get("ats"),
        "approved_for_autofill": bool(packet.get("approved_for_autofill", False)),
        "fill_plan": plan,
        "packet_fields_not_on_form": missing_on_form,
        "form_fields_not_in_packet": missing_in_packet,
        "unmapped_form_controls": len(field_map["unmapped_controls"]),
        "note": "read-only plan; no values were injected",
    }


def _cli() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "scan":
        url = sys.argv[2]
        profile = sys.argv[3] if len(sys.argv) > 3 else None
        scan = scan_form(url, profile_dir=profile, headless=True)
        print(json.dumps(scan, ensure_ascii=False, indent=2))
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "resolve":
        url = sys.argv[2]
        profile = sys.argv[3] if len(sys.argv) > 3 else None
        res = resolve_apply_target(url, profile_dir=profile, headless=True)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    raise SystemExit(
        "Usage: python3 -m coverai.browser_apply scan|resolve <url> [profile_dir]"
    )


if __name__ == "__main__":
    _cli()
