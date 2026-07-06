from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .storage import DEFAULT_USER_ID, CoverAiStore, offer_dedupe_hash, utc_now

FetchFn = Callable[[str], str]

JOB_WORDS = {
    "alternance",
    "apprentissage",
    "apprentice",
    "intern",
    "stage",
    "job",
    "emploi",
    "offre",
    "poste",
    "career",
    "recrut",
    "embedded",
    "firmware",
    "electronics",
    "electronique",
    "embarque",
    "iot",
    "fpga",
    "linux",
}

NAV_LINK_WORDS = {
    "about",
    "account",
    "aide",
    "career advice",
    "conditions",
    "cookie",
    "employers",
    "espace candidat",
    "espace recruteur",
    "find jobs",
    "find salaries",
    "help",
    "hire",
    "login",
    "offres de stage",
    "offres en alternance",
    "passer au contenu",
    "post job",
    "privacy",
    "rechercher une offre",
    "rechercher un profil",
    "recruteur",
    "salary",
    "salaries",
    "se connecter",
    "sign in",
    "skip to main content",
    "terms",
}

NAV_URL_FRAGMENTS = (
    "/account",
    "/career/salaries",
    "/candidat/etre-accompagne",
    "/candidat/recherche-emploi.html/emploi",
    "/fr-fr/emploi/metier_",
    "/guide-de-diffusion",
    "/hire",
    "/login",
    "/recruteur",
    "/salaries",
    "/signin",
)

DIRECT_OFFER_URL_PATTERNS = (
    re.compile(r"apec\.fr/.*/detail-offre/", re.IGNORECASE),
    re.compile(r"hellowork\.com/.*/emplois/\d+\.html", re.IGNORECASE),
    re.compile(r"linkedin\.com/jobs/view/", re.IGNORECASE),
    re.compile(r"welcometothejungle\.com/.*/jobs/", re.IGNORECASE),
)

# Link texts that are site chrome, not a job title. Matched as substrings (a real
# title never contains these), so nav rows never become offers -- even when their
# URL passes the direct-offer patterns.
NAV_TITLE_SUBSTRINGS = (
    "déconnexion",
    "deconnexion",
    "se déconnecter",
    "parcourir les offres",
    "multiple opportunities",
    "conditions générales",
    "conditions generales",
    "diffusion des offres",
    "offres de stage",
    "offres d'emploi",
    "offres d'alternance",
)

# Sign-in / cookie-consent signals. LinkedIn serves guests an identical ~1900-char
# login wall; storing it as the job body gives downstream motivation/CV steps
# nothing real to work with, so we flag such offers as thin_body.
LOGIN_WALL_SIGNALS = (
    "sign up | linkedin",
    "agree & join linkedin",
    "linkedin respects your privacy",
    "new to linkedin",
    "identifiez-vous",
    "cookie policy",
    "user agreement",
)

FIT_WORDS = {
    "embedded",
    "embarque",
    "firmware",
    "electronics",
    "electronique",
    "apprentissage",
    "alternance",
    "apprentice",
    "stage",
    "intern",
    "linux",
    "python",
    "c++",
    "c ",
    "fpga",
    "rtos",
    "iot",
    "sensor",
    "capteur",
    "ai",
    "ia",
}

SCORE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "company": {"type": "string"},
        "title": {"type": "string"},
        "location": {"type": "string"},
    },
    "required": ["score", "summary", "company", "title", "location"],
}


@dataclass
class OfferCandidate:
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    source: str = ""
    raw_text: str = ""
    snippet: str = ""

    def to_offer(self, score: int = 0, summary: str = "") -> dict[str, Any]:
        snippet = self.snippet or self.raw_text[:500] or self.title
        return {
            "dedupe_hash": offer_dedupe_hash(self.url, self.title, self.company, self.location, snippet),
            "url": self.url,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "source": self.source,
            "raw_text": self.raw_text,
            "snippet": snippet,
            "score": score,
            "summary": summary,
        }


class LinkExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []
        self._skip_depth = 0
        self.visible_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._current_href = href
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "a" and self._current_href:
            text = clean_text(" ".join(self._current_text))
            url = urllib.parse.urljoin(self.base_url, html.unescape(self._current_href))
            self.links.append({"url": url, "text": text})
            self._current_href = ""
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = clean_text(data)
        if not text:
            return
        self.visible_text.append(text)
        if self._current_href:
            self._current_text.append(text)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def extract_page(html_text: str, base_url: str) -> tuple[list[dict[str, str]], str]:
    parser = LinkExtractor(base_url)
    parser.feed(html_text)
    return parser.links, clean_text(" ".join(parser.visible_text))


def looks_like_job_link(url: str, text: str, keywords: list[str]) -> bool:
    haystack = f"{url} {text}".lower()
    clean_label = clean_text(text).lower()
    if is_direct_offer_url(url):
        return True
    if clean_label in NAV_LINK_WORDS or any(fragment in url.lower() for fragment in NAV_URL_FRAGMENTS):
        return False
    if len(clean_label) < 8 and not any(keyword.lower() in haystack for keyword in keywords if keyword.strip()):
        return False
    if any(word in haystack for word in JOB_WORDS):
        return True
    return any(keyword.lower() in haystack for keyword in keywords if keyword.strip())


def is_direct_offer_url(url: str) -> bool:
    return any(pattern.search(url) for pattern in DIRECT_OFFER_URL_PATTERNS)


def is_noise_title(text: str) -> bool:
    """True when a link's text is site chrome (nav/label), not a job title."""
    label = clean_text(text).lower().strip()
    if not label or label in {"n/a", "na", "-"}:
        return True
    return any(fragment in label for fragment in NAV_TITLE_SUBSTRINGS)


def looks_like_login_wall(raw_text: str) -> bool:
    """True when raw_text is a sign-in/consent wall rather than a job description.

    Keys on content signals (not a brittle exact length), and requires two hits so
    an offer that merely links to a cookie policy is not misflagged.
    """
    low = (raw_text or "").lower()
    return sum(1 for signal in LOGIN_WALL_SIGNALS if signal in low) >= 2


def source_entries(config: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for key in ("source_urls", "search_urls", "career_pages"):
        for index, item in enumerate(config.get(key, []) or []):
            if isinstance(item, str):
                entries.append({"name": f"{key}:{index + 1}", "url": item})
            elif isinstance(item, dict) and item.get("url"):
                entries.append({"name": str(item.get("name") or f"{key}:{index + 1}"), "url": str(item["url"])})
    if entries:
        return entries

    keywords = [str(item) for item in config.get("keywords", []) if str(item).strip()]
    locations = [str(item) for item in config.get("locations", []) if str(item).strip()]
    if not keywords:
        keywords = ["alternance systemes embarques"]
    if not locations:
        locations = [""]
    for keyword in keywords[:5]:
        for location in locations[:3]:
            query = " ".join(part for part in [keyword, location, "emploi"] if part).strip()
            entries.append({"name": f"duckduckgo:{query}", "url": f"https://duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"})
    return entries


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Job search config not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    apply_env_overrides(config)
    return config


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def apply_env_overrides(config: dict[str, Any]) -> None:
    sms = config.setdefault("sms", {})
    if not isinstance(sms, dict):
        sms = {}
        config["sms"] = sms
    if "COVERAI_SMS_ENABLED" in os.environ:
        sms["enabled"] = env_bool("COVERAI_SMS_ENABLED", bool(sms.get("enabled")))
    if os.environ.get("COVERAI_SMS_NUMBER"):
        sms["number"] = os.environ["COVERAI_SMS_NUMBER"]
    if os.environ.get("COVERAI_SMS_MIN_SCORE"):
        sms["min_score"] = int(os.environ["COVERAI_SMS_MIN_SCORE"])
    if os.environ.get("COVERAI_SMS_MAX_REPORTS_PER_RUN"):
        sms["max_reports_per_run"] = int(os.environ["COVERAI_SMS_MAX_REPORTS_PER_RUN"])


def fetch_url(url: str, use_playwright: bool = True) -> str:
    if url.startswith("file://"):
        return Path(urllib.parse.urlparse(url).path).read_text(encoding="utf-8", errors="ignore")
    if use_playwright:
        try:
            from playwright.sync_api import sync_playwright

            profile_dir = Path(os.environ.get("COVERAI_PLAYWRIGHT_PROFILE", ".coverai-browser")).expanduser()
            profile_dir.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch_persistent_context(str(profile_dir), headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                content = page.content()
                browser.close()
                return content
        except Exception:
            pass
    request = urllib.request.Request(url, headers={"User-Agent": "CoverAI/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def discover_offer_candidates(config: dict[str, Any], fetcher: FetchFn | None = None) -> list[OfferCandidate]:
    fetch = fetcher or (lambda url: fetch_url(url, bool(config.get("use_playwright", True))))
    keywords = [str(item) for item in config.get("keywords", [])]
    max_per_source = int(config.get("max_candidates_per_source", 15))
    max_total = int(config.get("max_offers_per_run", 30))
    seen: set[str] = set()
    candidates: list[OfferCandidate] = []
    for source in source_entries(config):
        try:
            page_html = fetch(source["url"])
        except Exception:
            continue
        links, page_text = extract_page(page_html, source["url"])
        links = sorted(links, key=lambda link: 0 if is_direct_offer_url(link["url"]) else 1)
        source_count = 0
        for link in links:
            url = link["url"].split("#", 1)[0]
            text = link["text"]
            if not url or url.rstrip("/") == source["url"].rstrip("/") or url in seen or not looks_like_job_link(url, text, keywords):
                continue
            if is_noise_title(text):  # nav/label link the scraper would store as a job
                continue
            seen.add(url)
            raw_text = ""
            try:
                detail_html = fetch(url)
                _, raw_text = extract_page(detail_html, url)
            except Exception:
                raw_text = text or page_text[:1200]
            candidates.append(
                OfferCandidate(
                    url=url,
                    title=text[:180],
                    source=source["name"],
                    raw_text=raw_text[:12000],
                    snippet=(raw_text or text or page_text)[:500],
                )
            )
            source_count += 1
            if source_count >= max_per_source or len(candidates) >= max_total:
                break
        if len(candidates) >= max_total:
            break
    return candidates


def heuristic_score(candidate: OfferCandidate, config: dict[str, Any]) -> tuple[int, str]:
    text = f"{candidate.title} {candidate.snippet} {candidate.raw_text}".lower()
    score = 20
    for word in FIT_WORDS:
        if word in text:
            score += 5
    for keyword in config.get("keywords", []) or []:
        if str(keyword).lower() in text:
            score += 8
    for location in config.get("locations", []) or []:
        if str(location).lower() in text:
            score += 4
    score = max(0, min(score, 95))
    summary = candidate.snippet or candidate.raw_text[:220] or candidate.title
    return score, clean_text(summary)[:240]


def score_offer(candidate: OfferCandidate, config: dict[str, Any], openai_client: Any = None, model: str = "gpt-4o-mini") -> OfferCandidate:
    if openai_client is None:
        score, summary = heuristic_score(candidate, config)
        candidate.snippet = summary
        candidate.raw_text = candidate.raw_text or summary
        candidate.company = candidate.company or infer_company(candidate)
        candidate.location = candidate.location or infer_location(candidate.raw_text, config)
        candidate.title = candidate.title or "Untitled offer"
        candidate._score = score  # type: ignore[attr-defined]
        candidate._summary = summary  # type: ignore[attr-defined]
        return candidate

    payload = {
        "offer": {
            "url": candidate.url,
            "title": candidate.title,
            "text": (candidate.raw_text or candidate.snippet)[:6000],
        },
        "target": {
            "keywords": config.get("keywords", []),
            "locations": config.get("locations", []),
            "companies": config.get("companies", []),
        },
    }
    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You score apprenticeship and engineering job offers for Julien Gonzales. "
                    "Prefer embedded systems, electronics, firmware, applied AI, Linux, sensors, FPGA, IoT, and alternance/apprentissage. "
                    "Return concise JSON only."
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "coverai_offer_score", "schema": SCORE_SCHEMA, "strict": True}},
        )
        parsed = json.loads(response.choices[0].message.content)
        candidate._score = int(parsed.get("score") or 0)  # type: ignore[attr-defined]
        candidate._summary = str(parsed.get("summary") or "")[:240]  # type: ignore[attr-defined]
        candidate.company = str(parsed.get("company") or candidate.company or infer_company(candidate))
        candidate.title = str(parsed.get("title") or candidate.title or "Untitled offer")
        candidate.location = str(parsed.get("location") or candidate.location or infer_location(candidate.raw_text, config))
    except Exception:
        score, summary = heuristic_score(candidate, config)
        candidate._score = score  # type: ignore[attr-defined]
        candidate._summary = summary  # type: ignore[attr-defined]
        candidate.company = candidate.company or infer_company(candidate)
        candidate.location = candidate.location or infer_location(candidate.raw_text, config)
    return candidate


def infer_company(candidate: OfferCandidate) -> str:
    host = urllib.parse.urlparse(candidate.url).hostname or ""
    host = host.removeprefix("www.")
    return host.split(".")[0].title() if host else ""


def infer_location(text: str, config: dict[str, Any]) -> str:
    haystack = text.lower()
    for location in config.get("locations", []) or []:
        if str(location).lower() in haystack:
            return str(location)
    return ""


def format_offer_sms(offer: dict[str, Any]) -> str:
    title = clean_text(str(offer.get("title") or "Untitled offer"))[:70]
    company = clean_text(str(offer.get("company") or "Unknown company"))[:40]
    location = clean_text(str(offer.get("location") or "Location unknown"))[:35]
    score = int(offer.get("score") or 0)
    return (
        f"CoverAI: {score}% {company} - {title}. {location}. "
        "Reply naturally: 'tell me about this one', 'why apply?', or 'start applying'."
    )


def report_offer_by_sms(store: CoverAiStore, offer_id: str, number: str, sms_client: Any, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    offer = store.get_offer(offer_id)
    if not offer:
        raise KeyError(f"Unknown offer: {offer_id}")
    text = format_offer_sms(offer)
    try:
        response = sms_client.send_sms(number, text)
        status = "sent"
    except Exception as error:
        response = {"error": str(error)}
        status = "failed"
    return store.record_sms_report(offer_id, number, text, status, response, user_id=user_id)


def run_offer_explorer(
    store: CoverAiStore,
    config_path: str | Path,
    openai_client: Any = None,
    model: str = "gpt-4o-mini",
    sms_client: Any = None,
    fetcher: FetchFn | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    config = load_config(config_path)
    run = store.create_explorer_run(str(config_path), user_id=user_id)
    run_id = run["id"]
    found = 0
    created = 0
    reported = 0
    saved_offers: list[dict[str, Any]] = []
    try:
        candidates = discover_offer_candidates(config, fetcher=fetcher)
        found = len(candidates)
        min_score = int(config.get("minimum_score", config.get("min_score", 65)))
        for candidate in candidates:
            scored = score_offer(candidate, config, openai_client=openai_client, model=model)
            score = int(getattr(scored, "_score", 0))
            summary = str(getattr(scored, "_summary", scored.snippet or ""))[:240]
            offer_payload = scored.to_offer(score=score, summary=summary)
            if looks_like_login_wall(scored.raw_text):
                offer_payload["cleanup_status"] = "thin_body"
            offer, is_new = store.upsert_offer(offer_payload, user_id=user_id)
            if is_new:
                created += 1
                store.create_queue_item("offer.discovered", "offer", offer["id"], {"run_id": run_id}, user_id=user_id)
            saved_offers.append(offer)
            store.add_event("offer.seen", "offer", offer["id"], {"run_id": run_id, "is_new": is_new}, user_id=user_id)

        sms_config = config.get("sms") if isinstance(config.get("sms"), dict) else {}
        sms_number = str(sms_config.get("number") or "")
        sms_enabled = bool(sms_config.get("enabled"))
        sms_min_score = int(sms_config.get("min_score") or min_score)
        max_reports = int(sms_config.get("max_reports_per_run") or 0)
        if sms_enabled and sms_client and sms_number and max_reports > 0:
            for offer in sorted(saved_offers, key=lambda item: int(item.get("score") or 0), reverse=True):
                if reported >= max_reports:
                    break
                if str(offer.get("status") or "new") != "new":
                    continue
                if int(offer.get("score") or 0) < sms_min_score:
                    continue
                report = report_offer_by_sms(store, offer["id"], sms_number, sms_client, user_id=user_id)
                if report.get("status") == "sent":
                    store.mark_offer_status(offer["id"], "reported", user_id=user_id)
                    offer["status"] = "reported"
                    reported += 1

        final = store.update_explorer_run(
            run_id,
            status="completed",
            completed_at=utc_now(),
            offers_found=found,
            offers_new=created,
            offers_reported=reported,
        )
        return {"run": final, "offers": saved_offers}
    except Exception as error:
        final = store.update_explorer_run(
            run_id,
            status="failed",
            completed_at=utc_now(),
            offers_found=found,
            offers_new=created,
            offers_reported=reported,
            error=str(error),
        )
        return {"run": final, "offers": saved_offers, "error": str(error)}


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    config_path = Path(os.environ.get("COVERAI_JOB_SEARCH_CONFIG", str(base_dir / "config" / "job_search.json")))
    db_path = Path(os.environ.get("COVERAI_DB_PATH", str(base_dir / "coverai.db")))
    result = run_offer_explorer(CoverAiStore(db_path), config_path)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
