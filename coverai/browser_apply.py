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
from urllib.parse import urlparse
from urllib.request import url2pathname

from .platforms import absolute_profile_dir, playwright_available


# --- WP-H2: logical field vocabulary (submission-packet seam) --------------------
#
# Each logical field maps to keyword hints matched against a control's label, name,
# id, and placeholder (all lowercased). Keep this list minimal: a field earns a slot
# only when a real submission page demands it. Extend it from scan evidence, not guesses.
# Frozen v1 against real-form evidence; see personal-agentic-workflow/work-items/
# 20260706-forms-vocabulary-freeze.md. Two categories share this table:
#   * DERIVED targets  -- Helene synthesizes the value (full_name, confirm_email); Marie
#     never emits these. The fill rule lives in fill_form(), not the packet.
#   * CANONICAL fields -- the names Marie emits in the packet. This half is the shared
#     seam (see LOGICAL_FIELD_VOCABULARY below); the hints/kinds here are Helene's.
LOGICAL_FIELDS: dict[str, dict[str, Any]] = {
    # --- Helene-derived targets (mapped on the form; value synthesized in fill_form) ---
    "full_name":          {"kind": "text",     "hints": ["full name", "nom complet", "your name", "votre nom"]},
    "confirm_email":      {"kind": "text",     "hints": ["confirm", "confirmez", "confirmer", "verify email", "re-enter"]},
    # --- Marie-emitted canonical fields ---
    "first_name":         {"kind": "text",     "hints": ["first name", "prénom", "prenom", "given name"]},
    "last_name":          {"kind": "text",     "hints": ["last name", "surname", "nom de famille", "nom"]},
    "email":              {"kind": "text",     "hints": ["email", "e-mail", "courriel", "adresse mail"]},
    "phone":              {"kind": "text",     "hints": ["phone", "téléphone", "telephone", "portable", "mobile"]},
    "location_city":      {"kind": "text",     "hints": ["city", "ville", "town"]},
    "location_country":   {"kind": "text",     "hints": ["country", "pays", "région", "region", "nationality", "nationalité"]},
    "linkedin_url":       {"kind": "text",     "hints": ["linkedin"]},
    "portfolio_url":      {"kind": "text",     "hints": ["portfolio", "website", "site web", "github"]},
    "cv_upload":          {"kind": "file",     "hints": ["cv", "resume", "résumé", "curriculum"]},
    "cover_letter_upload":{"kind": "file",     "hints": ["cover letter", "lettre de motivation", "motivation letter"]},
    "motivation":         {"kind": "textarea", "hints": ["motivation", "cover letter", "message", "pourquoi", "why", "lettre"]},
    "start_date":         {"kind": "text",     "hints": ["start date", "date de début", "disponibilité", "availability", "disponible"]},
    "work_authorization": {"kind": "text",     "hints": ["work authorization", "autorisation de travail", "visa", "permit", "titre de séjour"]},
    "consent_gdpr":       {"kind": "checkbox", "hints": ["consent", "consentement", "rgpd", "gdpr", "privacy", "politique de confidentialité"]},
    # --- PARKED (redline #6): no real form has demanded these yet. Re-activate on evidence.
    #   "salary_expectation": {"kind": "text", "hints": ["salary", "salaire", "prétentions", "rémunération"]},
    #   "notice_period":      {"kind": "text", "hints": ["notice period", "préavis", "preavis"]},
    #   "location":           {"kind": "text", "hints": ["location", "localisation", "adresse", "address"]},
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
    "francetravail": "francetravail.fr",      # ex-Pole emploi; candidat.francetravail.fr
    "poleemploi": "pole-emploi.fr",           # legacy domain, still redirects live
    "vinci": "jobs.vinci.com",                # partner ATS France Travail redirects into
}


# JS run inside the page to enumerate form controls with a best-effort label + selector.
# Kept in the page context because label resolution (for=, ancestor <label>, aria-*,
# preceding text) is far simpler against the live DOM than reconstructed server-side.
_SCAN_JS = r"""
() => {
  // Selector Playwright can resolve. Playwright's CSS engine pierces OPEN shadow roots,
  // so an #id or [name] captured inside a web component still locates from the page.
  const cssSel = (el) => {
    // Tag-qualify so an id shared with a wrapper web-component (common in ATS forms:
    // <custom-input id="x"> around the real <input id="x">) still targets the fillable one.
    if (el.id) return el.tagName.toLowerCase() + '#' + CSS.escape(el.id);
    if (el.getAttribute('name')) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.getAttribute('name')) + '"]';
    if (el.getAttribute('aria-label')) return el.tagName.toLowerCase() + '[aria-label="' + CSS.escape(el.getAttribute('aria-label')) + '"]';
    return el.tagName.toLowerCase();
  };
  // Resolve the label within the element's OWN root (document or a shadow root), because
  // label[for=]/aria-labelledby targets live in the same tree, not the top document.
  const labelFor = (el) => {
    const root = el.getRootNode();
    if (el.id && root.querySelector) {
      const l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const anc = el.closest('label');
    if (anc && anc.innerText.trim()) return anc.innerText.trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb && root.getElementById) { const t = root.getElementById(lb); if (t && t.innerText.trim()) return t.innerText.trim(); }
    let p = el.previousElementSibling;
    while (p) { if (p.innerText && p.innerText.trim()) return p.innerText.trim().slice(0, 120); p = p.previousElementSibling; }
    return '';
  };
  // Which region a control lives in. Climbs ancestors, crossing shadow boundaries via
  // the host, so we can keep application fields (in a <form>/dialog) and drop page chrome
  // (nav/header/footer/search). Lets the catalog stop eating global search boxes etc.
  const regionOf = (el) => {
    let node = el, inDialog = false, inForm = false, inChrome = false;
    while (node) {
      if (node.nodeType === 1) {
        const tag = (node.tagName || '').toLowerCase();
        const role = ((node.getAttribute && node.getAttribute('role')) || '').toLowerCase();
        if (tag === 'form') inForm = true;
        if (tag === 'dialog' || role === 'dialog') inDialog = true;
        if (['nav', 'header', 'footer'].includes(tag)
            || ['navigation', 'search', 'banner', 'contentinfo'].includes(role)) inChrome = true;
      }
      node = node.parentNode || node.host || null;  // shadow root -> host
    }
    return { inDialog, inForm, inChrome };
  };
  const out = [];
  const seen = new Set();
  // Walk the light DOM AND recurse into every open shadow root. Modern ATS forms
  // (SmartRecruiters) render their inputs inside web components; a plain
  // document.querySelectorAll sees only the ~1 field that leaks into the light DOM.
  const collect = (root) => {
    for (const el of root.querySelectorAll('input, select, textarea')) {
      if (seen.has(el)) continue;
      seen.add(el);
      const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
      if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
      const rect = el.getBoundingClientRect();
      const region = regionOf(el);
      out.push({
        tag: el.tagName.toLowerCase(),
        type,
        name: el.getAttribute('name') || '',
        id: el.id || '',
        selector: cssSel(el),
        label: labelFor(el),
        placeholder: el.getAttribute('placeholder') || '',
        required: el.required || el.getAttribute('aria-required') === 'true',
        visible: rect.width > 0 && rect.height > 0,
        in_dialog: region.inDialog,
        in_form: region.inForm,
        in_chrome: region.inChrome,
        options: el.tagName.toLowerCase() === 'select'
          ? Array.from(el.options).map(o => o.text.trim()).slice(0, 25) : [],
      });
    }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) collect(el.shadowRoot);
    }
  };
  collect(document);
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


# Anti-bot / CAPTCHA services. If a frame is served from one of these, an automated
# browser has been challenged and the real form is unreachable headless -- this is the
# signal the assistive (human-in-the-loop) model exists to handle.
CAPTCHA_HOSTS = (
    "captcha-delivery.com",  # DataDome
    "datadome",
    "recaptcha",
    "hcaptcha.com",
    "arkoselabs.com",
    "funcaptcha",
    "perimeterx",
    "px-cloud",
)


def _scan_all_frames(page: Any) -> dict[str, Any]:
    """Scan the top document AND child iframes; flag any CAPTCHA frame.

    Modern ATS apply forms (SmartRecruiters, Workday) render inside iframes, and some
    sit behind a CAPTCHA iframe. Scanning only the top document misses both, and reports
    a misleading "0 controls". This gathers controls across frames and names the blocker.
    """
    controls: list[dict[str, Any]] = []
    captcha_host = ""
    for frame in page.frames:
        furl = (frame.url or "").lower()
        if any(h in furl for h in CAPTCHA_HOSTS):
            captcha_host = frame.url
            continue
        try:
            data = frame.evaluate(_SCAN_JS)
            controls.extend(data.get("controls", []))
        except Exception:  # noqa: BLE001 -- cross-origin frame or detached; skip it
            continue
    # Shadow-DOM elements can share an id, so a selector like "#file-input" may match
    # several controls. Record which occurrence each is, so a filler can target it with
    # locator(selector).nth(selector_index) instead of hitting the wrong field.
    occurrences: dict[str, int] = {}
    for c in controls:
        sel = c["selector"]
        c["selector_index"] = occurrences.get(sel, 0)
        occurrences[sel] = c["selector_index"] + 1
    return {
        "controls": controls,
        "captcha_detected": bool(captcha_host),
        "captcha_host": captcha_host,
    }


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
            result.update(scan_current(page))
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
        # A consent is a checkbox ("I agree"), never a Yes/No radio. Excluding radios stops
        # consent_gdpr from stealing a radio whose name merely contains "consent" (Vinci's
        # #consent-yes), leaving the real GDPR checkbox for the required-checkbox fallback.
        return ctype == "checkbox"
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
    # France Travail (and peers) ship a "Signaler cette offre" report dialog that also carries
    # name + email fields but is NOT an application. Its report reasons are the tell-tale: no
    # real candidature form offers "tentative d'escroquerie" as a choice.
    if any(m in blob for m in ("escroquerie", "signaler cette offre", "offre commerciale",
                               "ne correspond pas", "n'existe plus", "tentative d'escroquerie")):
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
            # Aggregators ("Postuler sur le site du recruteur") open a NEW TAB to an
            # external ATS, and that redirect can be slow -- wait long enough to catch it.
            dest_page = page
            try:
                with context.expect_page(timeout=12000) as popup_info:
                    target.click()
                dest_page = popup_info.value  # a new tab opened
                dest_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 -- no popup; same-tab nav or modal
                page.wait_for_timeout(3000)
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


def _scope_to_application(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the application-form controls, drop page chrome (nav/search/footer).

    Preference order, most-specific first:
      1. a dialog is open (Easy Apply modal) -> keep only its controls;
      2. else there is a <form> -> keep only in-form controls;
      3. else keep everything that is NOT inside nav/header/footer/search.
    Falls back to the raw list if a rule would empty it (better noisy than blind).
    """
    dialog = [c for c in controls if c.get("in_dialog")]
    if dialog:
        return dialog
    in_form = [c for c in controls if c.get("in_form")]
    if in_form:
        return in_form
    not_chrome = [c for c in controls if not c.get("in_chrome")]
    return not_chrome or controls


def scan_current(page: Any) -> dict[str, Any]:
    """Run the read-only control enumeration against an already-open page.

    Merges controls from the top document and any child iframes, flags a CAPTCHA frame,
    and scopes `controls` to the application form (page chrome kept under `controls_all`).
    """
    data = page.evaluate(_SCAN_JS)  # top frame: applyLinks + title
    frames = _scan_all_frames(page)
    all_controls = frames["controls"]
    data["controls_all"] = all_controls
    data["controls"] = _scope_to_application(all_controls)
    data["captcha_detected"] = frames["captcha_detected"]
    data["captcha_host"] = frames["captcha_host"]
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


def _ctrl_key(control: dict[str, Any]) -> tuple[str, int]:
    """Identity of a control, unique even when a selector repeats across shadow roots."""
    return (control["selector"], control.get("selector_index", 0))


def _map_entry(control: dict[str, Any], score: int = 0) -> dict[str, Any]:
    return {
        "selector": control["selector"],
        "selector_index": control.get("selector_index", 0),
        "control_type": control["type"],
        "label": control["label"],
        "required": control["required"],
        "confidence": "high" if score >= 2 else "low",
    }


def map_fields(scan: dict[str, Any]) -> dict[str, Any]:
    """Map logical field names onto scanned controls. Read-only planning."""
    controls = scan.get("controls", [])
    mapping: dict[str, Any] = {}
    used: set[tuple[str, int]] = set()

    # Pass 1: keyword match on label/name/id/placeholder.
    for logical, spec in LOGICAL_FIELDS.items():
        best, best_score = None, 0
        for c in controls:
            if _ctrl_key(c) in used or not _kind_ok(spec["kind"], c):
                continue
            score = _match_field(c, spec["hints"])
            if score > best_score:
                best, best_score = c, score
        if best and best_score > 0:
            used.add(_ctrl_key(best))
            mapping[logical] = _map_entry(best, best_score)

    # Pass 2: evidence-driven rules for fields real forms label generically.
    # (a) File inputs are often just "Choose a file"; assign the non-photo ones in order.
    free_files = [
        c for c in controls
        if c["type"] == "file" and _ctrl_key(c) not in used
        and "photo" not in (c["label"] + c["name"] + c["id"]).lower()
    ]
    for logical in ("cv_upload", "cover_letter_upload"):
        if logical not in mapping and free_files:
            c = free_files.pop(0)
            used.add(_ctrl_key(c))
            entry = _map_entry(c)
            entry["by_rule"] = "file_order"
            mapping[logical] = entry
    # (b) A required checkbox with a weak label is the GDPR consent box.
    if "consent_gdpr" not in mapping:
        for c in controls:
            if c["type"] == "checkbox" and c["required"] and _ctrl_key(c) not in used:
                used.add(_ctrl_key(c))
                entry = _map_entry(c)
                entry["by_rule"] = "required_checkbox"
                mapping["consent_gdpr"] = entry
                break

    unmapped = [c for c in controls if _ctrl_key(c) not in used]
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


# Logical fields a machine must never set on the user's behalf. Consent is a legal act by
# the human; a start of typing here would be us asserting agreement for them.
HUMAN_ONLY_FIELDS = frozenset({"consent_gdpr"})


def _resolve_artifact_path(ref: str, packet: dict[str, Any]) -> str | None:
    """Translate an 'artifact:<id>' reference into a real local file path, or None.

    Marie names uploads symbolically (cv_upload -> "artifact:art_cv_..."), never as a raw
    path. The packet carries its own resolution table under `artifacts`: a list of
    ArtifactRef objects, each with an `artifact_id` and a `storage_ref` file:// URL. We look
    the id up there and turn the URL into a filesystem path Playwright can upload.

    Fail-closed: returns None if the ref is malformed, the artifact is absent, its storage is
    not a local file, or the file does not exist -- so an unresolved CV skips the upload
    rather than erroring or attaching the wrong document.
    """
    if not ref.startswith("artifact:"):
        return None
    artifact_id = ref[len("artifact:"):]
    for art in packet.get("artifacts", []):
        if art.get("artifact_id") != artifact_id:
            continue
        storage = art.get("storage_ref", "")
        if not storage.startswith("file://"):
            return None  # a link/remote artifact can't be handed to a file input
        # file:///home/j3/.../cv.pdf -> /home/j3/.../cv.pdf (url2pathname also decodes %20 etc.)
        path = url2pathname(urlparse(storage).path)
        return path if Path(path).exists() else None
    return None


def fill_form(page: Any, packet: dict[str, Any], scan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill the mapped fields on an ALREADY-OPEN, human-blessed form. Never submits.

    Safety rules, all fail-closed:
      * refuses entirely unless packet['approved_for_autofill'] is True;
      * never touches HUMAN_ONLY_FIELDS (GDPR consent);
      * never clicks a submit button -- filling a form creates no application;
      * file uploads are only performed for a real local path, never a bare artifact ref.

    Returns a fill record. Sensitive values are masked in the record, never logged raw.
    """
    if not packet.get("approved_for_autofill", False):
        return {"submitted": False, "filled": [], "skipped": [],
                "refused": "approved_for_autofill is not True"}

    if scan is None:
        scan = scan_current(page)
    mapped = map_fields(scan)["mapped"]
    packet_fields = {f["name"]: f for f in packet.get("fields", [])}
    filled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def record(logical, status, sensitive=False, **extra):
        entry = {"logical": logical, "status": status, "sensitive": bool(sensitive)}
        entry.update(extra)
        (filled if status == "filled" else skipped).append(entry)

    for logical, target in mapped.items():
        if logical in HUMAN_ONLY_FIELDS:
            record(logical, "skipped_human_only")
            continue
        # Derived targets: Marie emits neither, so Helene synthesizes the value here.
        pf = packet_fields.get(logical)
        if logical == "confirm_email" and pf is None:
            pf = packet_fields.get("email")           # confirm-email mirrors email
        if logical == "full_name" and pf is None:
            # join(first,last) is safe (roles known); splitting a full name is not.
            first, last = packet_fields.get("first_name"), packet_fields.get("last_name")
            if first and last and first.get("value") and last.get("value"):
                ready = first.get("status") == "ready" and last.get("status") == "ready"
                pf = {"value": f"{first['value']} {last['value']}".strip(),
                      "status": "ready" if ready else "needs_review"}
        if pf is None:
            record(logical, "no_packet_value")
            continue
        # Fill confirmed (ready) values AND drafts (needs_review): the human reviews the
        # whole form before the separate submit approval, so a draft in the real textarea
        # is useful, not dangerous. Only a value-less / missing field is skipped.
        packet_status = pf.get("status")
        if not pf.get("value") or packet_status not in ("ready", "needs_review"):
            record(logical, f"not_fillable:{packet_status}")
            continue

        value = str(pf["value"])
        sensitive = bool(pf.get("sensitive", False))
        loc = page.locator(target["selector"]).nth(target.get("selector_index", 0))
        ctype = target["control_type"]
        try:
            if ctype == "file":
                # Resolve an "artifact:<id>" ref via the packet's own artifacts table;
                # a bare path (rare) is used as-is. Either way we only upload a real file.
                path = _resolve_artifact_path(value, packet) if value.startswith("artifact:") else value
                if not path or not Path(path).exists():
                    record(logical, "file_needs_real_path", sensitive)
                    continue
                loc.set_input_files(path, timeout=8000)
                record(logical, "filled", sensitive, packet_status=packet_status, file=Path(path).name)
                continue
            elif ctype == "checkbox":
                loc.check(timeout=8000)
            elif target.get("control_type") == "select":
                loc.select_option(value, timeout=8000)
            else:
                loc.fill(value, timeout=8000)
            record(logical, "filled", sensitive, packet_status=packet_status)
        except Exception as error:  # noqa: BLE001 -- one bad field must not abort the rest
            skipped.append({"logical": logical, "status": "error", "detail": str(error)[:80]})

    return {
        "offer_ref": packet.get("offer_ref"),
        "target_url": scan.get("final_url") or scan.get("requested_url"),
        "ats": scan.get("ats"),
        "submitted": False,
        "filled": filled,
        "skipped": skipped,
        "note": "fields filled on a blessed session; NOT submitted (final submit needs its own approval)",
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
