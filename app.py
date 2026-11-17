"""Damir Job Radar v6: automatic vacancy search + explainable fit scoring.

Single-file Streamlit app. No API keys, no database, no paid services.

Automatic sources (public career-site pages and endpoints, no login):
    UNDP, IOM       Oracle Recruiting Cloud
    WFP, UNHCR      Workday           (UNHCR falls back to UNjobs if Workday refuses)
    UNICEF          jobs.unicef.org   (falls back to UNjobs if the site cannot be read)
    Arbeitnow       free European job API
    UNjobs          duty-station pages for Bosnia and Herzegovina, Serbia, Croatia
    UN in BiH       bosniaherzegovina.un.org/en/jobs
    Local boards    Klix Posao, MojPosao.ba (BiH), Infostud (Serbia), MojPosao.hr (Croatia)
    Europe          EuroBrussels, OSCE
Any other careers page, job board listing or RSS/Atom feed can be added in the Sources tab.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import html
import json
import random
import re
import threading
import unicodedata
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st

try:  # Browser-like TLS fingerprint and cookie session. Helps with Cloudflare checks (Workday).
    from curl_cffi import requests as _cffi
    _CffiSession = _cffi.Session
    _cffi_request = _cffi.request
    CFFI_OK = True
except Exception:  # optional: the app works without it
    _CffiSession = None
    _cffi_request = None
    CFFI_OK = False

# =====================================================================
# 1. SETTINGS YOU MAY EDIT
# =====================================================================

APP_TITLE = "Damir Job Radar"
CACHE_HOURS = 6                  # successful results are reused for this long
FAIL_RETRY_MINUTES = 20          # minimum pause before a failed source is retried automatically
MAX_COOLDOWN_HOURS = 6           # longest pause, even if a site asks for more
HTTP_TIMEOUT = 25
HTTP_ATTEMPTS = 3                # per request, with back-off
MAX_INLINE_WAIT = 30             # longest wait inside one search; longer waits become a cooldown
PACE_SECONDS = 1.0               # minimum gap between requests to the same site (all users, all sources)
SOURCE_TIME_BUDGET = 120         # seconds per source
FALLBACK_TIME_BUDGET = 60        # seconds for a fallback source
DETAIL_LIMIT_PER_SOURCE = 25     # full vacancy texts fetched for the most relevant titles
WORKDAY_FETCH_ALL_MAX = 300      # Workday sites with up to this many jobs are read completely
MAX_LISTING_PAGES = 24           # page cap for HTML career sites
UNJOBS_MAX_PAGES = 3
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

DEFAULT_KEYWORDS = [
    "construction", "engineer", "infrastructure", "rehabilitation", "WASH",
    "procurement", "project manager", "shelter", "architect", "heritage",
]

DEFAULT_SOURCES = [
    # International organizations (official career sites)
    {"name": "UNDP", "url": "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "fallback": "", "country": "", "enabled": True},
    {"name": "IOM", "url": "https://fa-evlj-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
     "fallback": "", "country": "", "enabled": True},
    {"name": "WFP", "url": "https://wfp.wd3.myworkdayjobs.com/job_openings", "fallback": "", "country": "", "enabled": True},
    {"name": "UNHCR", "url": "https://unhcr.wd3.myworkdayjobs.com/External",
     "fallback": "https://unjobs.org/organizations/unhcr", "country": "", "enabled": True},
    {"name": "UNICEF", "url": "https://jobs.unicef.org/en-us/listing/",
     "fallback": "https://unjobs.org/organizations/unicef", "country": "", "enabled": True},
    {"name": "OSCE", "url": "https://vacancies.osce.org/latest-jobs", "fallback": "", "country": "", "enabled": True},
    # UN and international posts by duty station (Sarajevo, region)
    {"name": "UN in Bosnia and Herzegovina", "url": "https://bosniaherzegovina.un.org/en/jobs",
     "fallback": "", "country": "Bosnia and Herzegovina", "enabled": True},
    {"name": "UNjobs: Bosnia and Herzegovina", "url": "https://unjobs.org/duty_stations/bosnia-and-herzegovina",
     "fallback": "", "country": "Bosnia and Herzegovina", "enabled": True},
    {"name": "UNjobs: Serbia", "url": "https://unjobs.org/duty_stations/serbia", "fallback": "", "country": "Serbia", "enabled": True},
    {"name": "UNjobs: Croatia", "url": "https://unjobs.org/duty_stations/croatia", "fallback": "", "country": "Croatia", "enabled": True},
    # Local job boards
    {"name": "Klix Posao: Građevina (BiH)", "url": "https://posao.klix.ba/kat/gradjevina",
     "fallback": "", "country": "Bosnia and Herzegovina", "enabled": True},
    {"name": "MojPosao.ba: Građevinarstvo (BiH)", "url": "https://www.mojposao.ba/pretraga-poslova?positions=Gra%C4%91evinarstvo&sortBy=adtype",
     "fallback": "", "country": "Bosnia and Herzegovina", "enabled": True},
    {"name": "Infostud: Građevinski inženjer (Serbia)", "url": "https://poslovi.infostud.com/oglasi-za-posao-gra%C4%91evinski-in%C5%BEenjer",
     "fallback": "", "country": "Serbia", "enabled": True},
    {"name": "Infostud: Građevina, geodezija (Serbia)", "url": "https://poslovi.infostud.com/oglasi-za-posao-gra%C4%91evina-geodezija",
     "fallback": "", "country": "Serbia", "enabled": True},
    {"name": "MojPosao.hr (Croatia)", "url": "https://mojposao.hr/", "fallback": "", "country": "Croatia", "enabled": True},
    # Europe
    {"name": "Arbeitnow (Europe)", "url": "https://www.arbeitnow.com/api/job-board-api", "fallback": "", "country": "", "enabled": True},
    {"name": "EuroBrussels (EU)", "url": "https://www.eurobrussels.com/", "fallback": "", "country": "", "enabled": True},
    {"name": "UNjobs: WASH engineering", "url": "https://unjobs.org/skills/wash-related-engineering",
     "fallback": "", "country": "", "enabled": False},
]
SOURCE_FIELDS = ["enabled", "name", "url", "fallback", "country"]
UNICEF_CATEGORIES = ["wash (water sanitation and hygiene)", "facilities management", "supply management"]

PROFILE = {
    "experience_start_year": 2001,
    "languages_confirmed": ["bosnian", "croatian", "serbian", "english"],
    "languages_uncertain": ["french", "italian"],   # spoken; level confirmed in the sidebar
}
WEIGHTS = {"title": 20, "competencies": 25, "sector": 15, "experience": 15,
           "education": 10, "location": 5, "language": 5, "contract": 5}

PRIMARY_TITLES = [
    "construction", "civil engineer", "architectural engineer", "architect", "infrastructure",
    "rehabilitation", "site engineer", "site supervisor", "field engineer", "wash engineer",
    "wash specialist", "wash officer", "shelter", "facilities manager", "facility manager",
    "procurement", "contract manager", "contracts manager", "quantity surveyor",
    "project manager", "heritage", "engineer",
    # Bosnian/Croatian/Serbian (matched without diacritics: građevina -> gradjevina)
    "gradjevin", "gradevin", "inzenjer", "inzinjer", "arhitekt", "nadzorni", "projektant",
    "voditelj projekta", "rukovodilac projekta", "upravitelj projekta", "menadzer projekta",
    "projekt menadzer", "projektni menadzer", "sef gradilista", "voditelj gradilista",
    "rukovodilac gradilista", "odgovorni izvodjac", "izvodjac radova", "nabavk", "hidrotehni",
    # German
    "bauingenieur", "bauleiter", "oberbauleiter", "projektleiter", "projektmanager", "architekt",
    "ingenieur", "einkauf",
]
SECONDARY_TITLES = [
    "project coordinator", "programme officer", "program officer", "programme specialist",
    "technical adviser", "technical advisor", "technical specialist", "operations manager",
    "operations officer", "monitoring and evaluation", "supply", "consultant", "specialist",
    "strucni saradnik", "strucni suradnik", "koordinator", "konsultant", "konzultant", "ekspert",
    "specijalist", "savjetnik", "savetnik",
]
NEGATIVE_TITLES = [
    "software engineer", "software developer", "data scientist", "data engineer", "devops",
    "frontend", "backend", "full stack", "machine learning", "nurse", "physician",
    "medical doctor", "pharmacist", "midwife", "sales representative", "account executive",
    "accountant", "intern", "internship", "graduate programme", "trainee", "driver",
    "chauffeur", "cleaner", "security guard",
    "radnik", "radnici", "radnica", "majstor", "monter", "limar", "bravar", "keramicar", "zidar",
    "tesar", "vozac", "prodavac", "prodavacica", "trgovac", "konobar", "kuvar", "kuhar", "skladistar",
    "magacioner", "blagajnik", "cistac", "cistacica", "zastitar", "pomocni", "fizicki", "rukovalac",
    "pripravnik", "praksa", "praktikant", "volonter", "medicinski", "medicinska", "call center",
    "kundenberater", "kundenbetreuer", "kundenservice", "fahrer", "lagerist", "verkaufer",
]
COMPETENCIES = [
    "construction", "rehabilitation", "infrastructure", "site supervision", "supervision of works",
    "architecture", "architectural", "civil engineering", "wash", "water supply", "sanitation",
    "solar", "photovoltaic", "procurement", "tender", "bill of quantities", "bills of quantities",
    "boq", "cost estimate", "contract administration", "contract management", "fidic",
    "budget monitoring", "quality assurance", "quality control", "handover", "defects liability",
    "technical specifications", "design review", "stakeholder", "municipalit", "eu-funded",
    "european union", "ipa", "project management", "pmp", "heritage", "conservation",
    "post-conflict", "accessibility", "energy efficiency", "hvac", "schools", "preschool",
    "health facilit",
    "gradjevin", "gradevin", "gradilist", "nadzor", "sanacij", "rekonstrukcij", "adaptacij", "obnov",
    "infrastruktur", "vodovod", "kanalizacij", "vodosnabd", "predmjer", "predracun", "predmer",
    "javne nabavk", "tehnicki prijem", "primopredaj", "gradjevinska knjiga", "gradjevinski dnevnik",
    "autocad", "revit", "visokogradnj", "niskogradnj", "energetsk", "fotonapon", "projektn",
    "strucni ispit", "bauleitung", "bauuberwachung", "sanierung", "ausschreibung", "vergabe",
    "kostenschatz",
]
TARGET_ORGS = [
    "unicef", "undp", "unops", "wfp", "unhcr", "iom", "osce", "unesco", "world bank",
    "european union", "ebrd", "eib", "giz", "usaid", "save the children",
    "norwegian refugee council", "danish refugee council", "caritas", "strabag", "arup",
    "aecom", "jacobs", "stantec",
]
SECTOR_TERMS = ["humanitarian", "international development", "united nations", "ngo",
                "donor", "eu-funded", "reconstruction", "recovery"]
BIH_TERMS = ["bosnia", "herzegovina", "hercegovina", "bosna", "bih", "sarajevo", "banja luka", "mostar",
             "tuzla", "zenica", "bihac", "brcko", "bijeljina", "prijedor", "doboj", "trebinje", "zivinice",
             "tesanj", "kakanj", "konjic", "visoko", "ilidza", "ilijas", "vogosca", "istocno sarajevo", "neum",
             "gracanica", "jajce", "breza", "siroki brijeg", "livno", "gorazde", "travnik", "bugojno", "cazin",
             "republika srpska", "federacija"]
REGION_BIH, REGION_WB, REGION_EU, REGION_REMOTE, REGION_OTHER = (
    "Bosnia and Herzegovina", "Western Balkans", "Europe", "Remote / home-based", "Other / not stated")
REGIONS = [
    (REGION_BIH, BIH_TERMS),
    (REGION_WB, ["croatia", "hrvatska", "zagreb", "split", "rijeka", "osijek", "zadar", "dubrovnik", "serbia",
                 "srbija", "beograd", "belgrade", "novi sad", "nis", "kragujevac", "subotica", "montenegro",
                 "crna gora", "podgorica", "north macedonia", "macedonia", "skopje", "albania", "tirana", "kosovo",
                 "pristina", "prishtine", "slovenia", "slovenija", "ljubljana", "western balkans", "balkans"]),
    (REGION_EU, ["europe", "germany", "deutschland", "njemacka", "austria", "austrija", "vienna", "wien", "berlin",
                 "munich", "munchen", "hamburg", "frankfurt", "brussels", "belgium", "netherlands", "the hague",
                 "france", "paris", "italy", "italija", "rome", "geneva", "switzerland", "copenhagen", "denmark",
                 "sweden", "norway", "poland", "warsaw", "hungary", "budapest", "czech", "prague", "spain", "portugal",
                 "ireland", "luxembourg", "greece", "athens", "romania", "bulgaria", "turkey", "turkiye", "istanbul",
                 "ankara", "ukraine", "kyiv", "moldova", "chisinau", "saint-germain-en-laye"]),
    (REGION_REMOTE, ["remote", "home-based", "home based", "rad od kuce", "home office"]),
]
REGION_NAMES = [r[0] for r in REGIONS] + [REGION_OTHER]
KNOWN_LANGUAGES = ["english", "french", "italian", "spanish", "german", "arabic", "russian",
                   "portuguese", "ukrainian", "turkish", "chinese", "bosnian", "croatian",
                   "serbian", "albanian", "macedonian", "dutch", "polish"]

# =====================================================================
# 2. TEXT HELPERS
# =====================================================================

JOB_COLUMNS = [
    "job_id", "title", "organization", "location", "country", "grade", "grade_family",
    "contract_type", "posted_date", "closing_date", "url", "source", "description", "score",
    "category", "status", "days_left", "region", "seen_on", "strengths", "gaps", "uncertain",
    "keywords", "breakdown",
]
IMPORT_COLUMNS = ["title", "organization", "location", "country", "contract_type",
                  "posted_date", "closing_date", "url", "description"]
TRACKER_STATES = ["", "Saved", "Applied", "Interview", "Rejected", "Not interested"]
GRADE_FAMILIES = ["International (P/D)", "National officer (NO/NPSA/LICA)", "Consultancy (IICA/IPSA/consultant)",
                  "Support (G/GS/SC)", "Not stated"]


class SourceError(Exception):
    """A source (or one request) could not be read.

    kind: 'rate' (HTTP 429), 'blocked' (bot protection / refused), 'empty', 'error'.
    retry_after: seconds the site asked us to wait, when it said so.
    """

    def __init__(self, message: str, kind: str = "error", retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def clean_text(value: Any) -> str:
    """Plain text without HTML or scripts."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    raw = html.unescape(html.unescape(str(value)))
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<\s*(br|/p|/li|/div|/h\d|/tr|/td|/a)\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[ \t\r\f\v\xa0\u200b]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def html_lines(fragment: str) -> list[str]:
    """Text lines of an HTML fragment, one per element."""
    return [x.strip() for x in clean_text(re.sub(r"<[^>]+>", "\n", fragment or "")).splitlines() if x.strip()]


_ISO_PREFIX = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_DOTTED = re.compile(r"^\s*(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\.?")
_SLASHED = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def _safe_date(y: int, m: int, d: int) -> str:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def parse_date(value: Any) -> str:
    """Any date-like value -> YYYY-MM-DD, or ''.

    ISO text keeps the date as written (no time-zone shift); '13. 12. 2026.' and '13.12.2026' are
    day-first; '01/12/2026' is day-first unless that is impossible (European sources); epoch s/ms;
    'Nov 24 2026', '29 Nov 2026', 'Thursday, 19 November 2026'."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        text = str(value).strip()
        m = _ISO_PREFIX.match(text)
        if m:
            return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _DOTTED.match(text)
        if m:
            return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = _SLASHED.match(text)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return _safe_date(y, b, a) if b <= 12 else _safe_date(y, a, b)
        text = re.sub(r"^(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+", "", text, flags=re.I)
        parsed = pd.to_datetime(text, errors="coerce", utc=True)
        return "" if pd.isna(parsed) else parsed.date().isoformat()
    except (ValueError, OverflowError, OSError, TypeError):
        return ""


def parse_relative_posted(text: str, today: date | None = None) -> str:
    """Workday 'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / '30+ Days Ago'."""
    today = today or date.today()
    t = (text or "").lower()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else ""


def canonical_url(value: Any) -> str:
    """Only http(s) links; drop query string and fragment."""
    try:
        parts = urlsplit(clean_text(value))
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def safe_link(value: Any) -> str:
    """Original http(s) link (query kept, needed by some portals) or ''."""
    text = clean_text(value)
    return text if canonical_url(text) else ""


def norm_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(text).lower()).strip()


def fingerprint(job: dict[str, Any]) -> str:
    raw = "|".join(norm_title(job.get(k)) for k in ("organization", "title", "location"))
    return "fp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def job_identifier(job: dict[str, Any]) -> str:
    return canonical_url(job.get("url")) or fingerprint(job)


_FOLD = str.maketrans({"đ": "dj", "Đ": "dj", "ß": "ss", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l"})


@lru_cache(maxsize=4096)
def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.translate(_FOLD).lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def fold(text: Any) -> str:
    """Lower case without diacritics, so 'Građevinski inženjer' matches 'gradjevinski inzenjer'."""
    return _fold(str(text or ""))


@lru_cache(maxsize=8192)
def _term_re(term: str, whole: bool) -> re.Pattern:
    end = r"(?![a-z0-9])" if whole else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(fold(term)) + end)


def matched(terms: list[str], source: str, whole: bool = False) -> list[str]:
    """Terms found in the source (word-start match, diacritics ignored). The source is folded once."""
    s = fold(source)
    return [t for t in terms if _term_re(t, whole).search(s)]


def contains(term: str, source: str, whole: bool = False) -> bool:
    return bool(matched([term], source, whole))


def region_of(text: str) -> str:
    for name, terms in REGIONS:
        if matched(terms, text, True):
            return name
    return REGION_OTHER


def title_is_relevant(title: str) -> bool:
    t = (title or "").lower()
    if matched(NEGATIVE_TITLES, t, True):
        return False
    return bool(matched(PRIMARY_TITLES, t) or matched(SECONDARY_TITLES, t))


def title_matches_keywords(title: str, keywords: list[str]) -> bool:
    return bool(matched([k.lower() for k in keywords], (title or "").lower()))


def extract_grade(text: str) -> str:
    m = re.search(r"\b(P-?[1-6]|D-?[12]|NO-?[1-4A-D]|NO[A-D]|G-?[1-7]|GS-?[1-7]|SC-?\d{1,2}|"
                  r"IPSA-?\d{1,2}|NPSA-?\d{1,2}|IICA-?\d|LICA-?\d{1,2}|CST\s?I{1,3})\b", text or "")
    return m.group(1).upper() if m else ""


def grade_family(grade: str, title: str) -> str:
    g = (grade or "").upper()
    if re.match(r"(P|D)-?\d", g):
        return GRADE_FAMILIES[0]
    if re.match(r"(NO|NPSA|LICA)", g):
        return GRADE_FAMILIES[1]
    if re.match(r"(IICA|IPSA|CST)", g) or re.search(r"\bconsult", (title or "").lower()):
        return GRADE_FAMILIES[2]
    if re.match(r"(G|GS|SC)-?\d", g):
        return GRADE_FAMILIES[3]
    return GRADE_FAMILIES[4]


def days_left(closing: str, today: date | None = None) -> int | None:
    if not closing:
        return None
    try:
        return (date.fromisoformat(closing[:10]) - (today or date.today())).days
    except ValueError:
        return None


# Closing dates: "Closes Nov 24 2026", "Closing at: 29 Nov 2026 - 23:55 CAT", "Closing date: Thursday,
# 19 November 2026", "Deadline: 2026-12-01", "Apply before 5 January 2027", "Prijava do 13. 12. 2026.",
# "Rok za prijavu: 27.09.2024.", "Trajanje konkursa: 19.11.2026.", "Ističe: 13.12.2026", "Bewerbungsfrist: 01.12.2026".
_DATE = (r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{4}\.?|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|"
         r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})")
CLOSING_RE = re.compile(
    r"\b(?:closes|closing(?:\s+date)?(?:\s+at)?|apply\s+(?:before|by)|deadline(?:\s+for\s+applications)?|"
    r"prijav[ae]\s+do|rok\s+za\s+prijav\w*|rok\s+prijav\w*|trajanje\s+konkursa|konkurs\s+(?:je\s+)?otvoren\s+do|"
    r"oglas\s+(?:je\s+)?otvoren\s+do|isti[cč]e|bewerbungsfrist|bewerbungsschluss)"
    r"\s*[:\-]?\s*(?:on\s+|do\s+)?(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?" + _DATE, re.I)
RANGE_RE = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{4}\.?)?\s*[–\-]\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})")
_BARE_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})\.?(?![\d])")


def find_closing(text: str, allow_bare: bool = False, today: date | None = None) -> str:
    """Closing date from free text: keyword dates first, then 'from – to' ranges, then (listing pages only)
    the latest future dd.mm.yyyy date. Past bare dates are posting dates and are ignored."""
    hit = CLOSING_RE.search(text or "")
    if hit:
        found = parse_date(hit.group(1))
        if found:
            return found
    hit = RANGE_RE.search(text or "")
    if hit:
        found = parse_date(hit.group(1))
        if found:
            return found
    if allow_bare:
        today = today or date.today()
        future = [d for d in (parse_date(x) for x in _BARE_DATE_RE.findall(text or "")) if d and d >= today.isoformat()]
        if future:
            return max(future)
    return ""


_ANCHOR_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_GENERIC_LINK_TEXT = {"", "read more", "more", "apply", "apply now", "view", "view job", "details",
                      "learn more", "see more", "job details"}
_LD_RE = re.compile(r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)


def parse_jsonld_jobs(page: str) -> list[dict]:
    """schema.org JobPosting records embedded in a page (many career sites publish them for Google)."""
    found: list[dict] = []
    for block in _LD_RE.findall(page or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            for key in ("@graph", "itemListElement", "item"):
                if key in node:
                    stack.append(node[key])
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types:
                found.append(node)
    out = []
    for x in found:
        locs = x.get("jobLocation")
        locs = locs if isinstance(locs, list) else [locs]
        places = []
        for loc in locs:
            if not isinstance(loc, dict):
                continue
            addr = loc.get("address") or {}
            if isinstance(addr, dict):
                country = addr.get("addressCountry")
                country = country.get("name") if isinstance(country, dict) else country
                places.append(", ".join(str(p) for p in (addr.get("addressLocality"), country) if p))
        org = x.get("hiringOrganization")
        org = org.get("name") if isinstance(org, dict) else org
        kind = x.get("employmentType")
        out.append({"title": clean_text(x.get("title") or x.get("name")),
                    "description": x.get("description") or "",
                    "posted_date": x.get("datePosted") or "", "closing_date": x.get("validThrough") or "",
                    "location": "; ".join(p for p in places if p), "url": x.get("url") or "",
                    "organization": clean_text(org) if org else "",
                    "contract_type": ", ".join(kind) if isinstance(kind, list) else (kind or "")})
    return out


def enrich_from_page(job: dict, page: str) -> None:
    """Fill description, closing date and location from a vacancy page (JSON-LD first, then text)."""
    ld = parse_jsonld_jobs(page)
    if ld:
        rec = ld[0]
        if len(clean_text(rec["description"])) > len(clean_text(job.get("description"))):
            job["description"] = rec["description"]
        job["closing_date"] = job.get("closing_date") or parse_date(rec["closing_date"])
        job["location"] = job.get("location") or rec["location"]
        job["posted_date"] = job.get("posted_date") or rec["posted_date"]
        job["contract_type"] = job.get("contract_type") or rec["contract_type"]
    text = clean_text(page)
    if len(text) > len(clean_text(job.get("description"))) and not ld:
        job["description"] = text[:15000]
    if not job.get("closing_date"):
        job["closing_date"] = find_closing(text)


# =====================================================================
# 3. SCORING (deterministic and explainable)
# =====================================================================

def score_job(raw: dict[str, Any], today: date | None = None, profile: dict | None = None) -> dict[str, Any]:
    """Normalize one job and score it 0-100 against Damir's profile."""
    today = today or date.today()
    profile = profile or PROFILE
    job = {
        "title": clean_text(raw.get("title")),
        "organization": clean_text(raw.get("organization")),
        "location": clean_text(raw.get("location")),
        "country": clean_text(raw.get("country")),
        "contract_type": clean_text(raw.get("contract_type")),
        "posted_date": parse_date(raw.get("posted_date")),
        "closing_date": parse_date(raw.get("closing_date")),
        "url": safe_link(raw.get("url")),
        "source": clean_text(raw.get("source")) or "Manual",
        "description": clean_text(raw.get("description")),
    }
    job["grade"] = clean_text(raw.get("grade")) or extract_grade(f"{raw.get('title', '')} {job['description'][:600]}")
    job["grade_family"] = grade_family(job["grade"], job["title"])
    title = job["title"].lower()
    org = job["organization"].lower()
    loc = f"{job['location']} {job['country']}".lower()
    desc = job["description"].lower()
    full = f"{title}\n{org}\n{loc}\n{desc}"
    fdesc = fold(desc)
    region = region_of(f"{loc} {title}")
    if region == REGION_OTHER and not loc.strip():
        region = region_of(" ".join(matched(BIH_TERMS + REGIONS[3][1], desc[:400], True)))
    in_bih = region == REGION_BIH

    strengths: list[str] = []
    gaps: list[str] = []
    uncertain: list[str] = []
    pts: dict[str, int] = {}
    status = "OK"

    def review() -> None:
        nonlocal status
        if status == "OK":
            status = "Review required"

    # 1. Title (20)
    neg = matched(NEGATIVE_TITLES, title, True)
    prim = matched(PRIMARY_TITLES, title)
    sec = matched(SECONDARY_TITLES, title)
    if neg:
        pts["title"] = 0
        gaps.append("Role family not a fit: " + ", ".join(neg[:3]))
        status = "Disqualified"
    elif prim:
        pts["title"] = 20
        strengths.append("Primary target title: " + ", ".join(prim[:3]))
    elif sec:
        pts["title"] = 12
        strengths.append("Secondary target title: " + ", ".join(sec[:3]))
    else:
        pts["title"] = 0
        gaps.append("Title outside target role families")

    # 2. Competencies (25): distinct terms only, so repetition adds nothing
    comps = matched(COMPETENCIES, f"{title}\n{desc}")
    pts["competencies"] = round(25 * min(1.0, len(comps) / 8))
    if comps:
        strengths.append(f"{len(comps)} matching competencies")
    else:
        gaps.append("No target technical competencies found")

    # 3. Organization / sector (15)
    orgs = matched(TARGET_ORGS, f"{org}\n{title}", True)
    sectors = matched(SECTOR_TERMS, full)
    if orgs:
        pts["sector"] = 15
        strengths.append("Target organization: " + orgs[0].upper())
    elif sectors:
        pts["sector"] = 9
        strengths.append("Relevant sector: " + ", ".join(sectors[:3]))
    else:
        pts["sector"] = 4

    # 4. Experience (15)
    year_patterns = (r"(\d{1,2})\s*\+?\s*(?:or more\s*)?years?",
                     r"(\d{1,2})\s*\+?\s*godin\w*\s+(?:\w+\s+){0,2}(?:iskustv|staz)",
                     r"(?:minimalno|najmanje|min\.)\s*(\d{1,2})\s*godin",
                     r"(\d{1,2})\s*\+?\s*jahr\w*\s+(?:\w+\s+)?(?:berufserfahrung|erfahrung)")
    req_years = [int(n) for pat in year_patterns for n in re.findall(pat, fdesc) if 0 < int(n) <= 40]
    have = today.year - int(profile.get("experience_start_year", 2001))
    if re.search(r"\b(entry[- ]level|junior|no experience required|bez (?:radnog )?iskustva|"
                 r"iskustvo nije (?:potrebno|obavezno)|berufseinsteiger)\b", fold(full)):
        pts["experience"] = 0
        gaps.append("Appears below your seniority")
    elif req_years:
        need = max(req_years)
        pts["experience"] = 15 if need <= have else 6
        (strengths if need <= have else gaps).append(f"Requires {need}+ years; you have about {have}")
    else:
        pts["experience"] = 9
        uncertain.append("Required years of experience not stated")

    # 5. Education (10)
    edu = matched(["architecture", "architectural", "civil engineering", "engineering", "master",
                   "advanced university degree", "phd", "pmp", "construction management", "vss",
                   "diplomirani", "magistar", "inzenjer", "inzinjer", "arhitekt", "gradjevinarstv",
                   "studium", "bauingenieurwesen", "architektur", "dipl.-ing"], desc)
    pts["education"] = 10 if edu else 5
    if edu:
        strengths.append("Education aligned: " + ", ".join(edu[:3]))
    else:
        uncertain.append("Required degree not clearly stated")

    if re.search(r"licensed (professional )?engineer|chartered engineer|registered (professional )?engineer|"
                 r"professional engineer licen[cs]e", desc):
        uncertain.append("Engineering licence/registration mentioned: verify whether mandatory")
        review()

    # 6. Location and eligibility (5)
    nat = (re.search(r"nationals of ([a-z ]+?)(?: only|\)|\.|,|;|\n|$)", full)
           or re.search(r"(?:only|open|restricted) (?:open )?(?:to|for) ([a-z]+) nationals", full))
    explicit_national = re.search(r"nationals? only|national consultant|national individual consult|"
                                  r"locally recruited|open to nationals", full)
    local_grade = re.match(r"(NO|G|GS|SC|NPSA|LICA)", job["grade"] or "")
    if nat and not any(x in nat.group(1) for x in ("bosnia", "bih", "herzegovina", "all countries",
                                                    "all nationalities", "any country")):
        pts["location"] = 0
        gaps.append("Nationality restriction: " + nat.group(0).strip())
        status = "Disqualified"
    elif (explicit_national or local_grade) and not in_bih:
        pts["location"] = 0
        gaps.append("National/locally recruited post outside Bosnia and Herzegovina: usually requires local nationality")
        if explicit_national and loc.strip():
            status = "Disqualified"
        else:
            review()
    elif (explicit_national or local_grade) and in_bih:
        pts["location"] = 5
        strengths.append("National post in Bosnia and Herzegovina")
    elif region in (REGION_BIH, REGION_WB, REGION_EU, REGION_REMOTE):
        pts["location"] = 5
        strengths.append(f"Location: {region}")
    else:
        pts["location"] = 3
        uncertain.append("Relocation or duty-station conditions need review")
    international_post = bool(orgs) or bool(re.match(r"(P|D)-?\d", job["grade"] or ""))
    if region in (REGION_WB, REGION_EU) and not international_post:
        uncertain.append("Outside Bosnia and Herzegovina: check work permit and eligibility")

    if re.search(r"\beu citizens?\b|\beu nationals?\b|eu/eea", full):
        gaps.append("Limited to EU citizens or permit holders: check your eligibility")
        review()
    if re.search(r"tiers? 1 (?:and|&) 2|internal (?:candidates|applicants) only|restricted to internal|"
                 r"open to internal", full):
        uncertain.append("Internal or tier-restricted vacancy: check whether external candidates may apply")
        review()
    if re.search(r"\broster\b", full):
        uncertain.append("Roster: inclusion does not guarantee a contract")

    # 7. Language (5)
    lang_ctx = " ".join(re.findall(r"(?:fluen\w*|proficien\w*|native|required|mandatory|working knowledge)"
                                   r"[^.\n]{0,100}", desc))
    req_langs = set(matched(KNOWN_LANGUAGES, lang_ctx, True))
    for m in re.finditer(r"\b(engl\w*|njemack\w*|nemack\w*|francusk\w*|talijansk\w*|italijansk\w*|"
                         r"tursk\w*|arapsk\w*|spansk\w*)\s+jezik", fdesc):
        if re.search(r"pozeljn|prednost|nije obavez", fdesc[max(0, m.start() - 60):m.end() + 40]):
            continue                          # 'poželjno' = preferred, not required
        stem = m.group(1)[:4]
        req_langs.add({"engl": "english", "njem": "german", "nema": "german", "fran": "french", "tali": "italian",
                       "ital": "italian", "turs": "turkish", "arap": "arabic", "span": "spanish"}[stem])
    german_words = len(re.findall(r"\b(?:und|der|die|das|mit|fur|wir|sie|ihre|bei|eine|einen|sind|werden)\b", fdesc))
    if german_words >= 12 and len(fdesc) > 300:
        req_langs.add("german")              # vacancy written in German: German is in practice required
    confirmed = profile.get("languages_confirmed", [])
    uncertain_langs = profile.get("languages_uncertain", [])
    unknown = sorted(l for l in req_langs if l not in confirmed + uncertain_langs)
    level_unc = sorted(l for l in req_langs if l in uncertain_langs)
    if unknown:
        pts["language"] = 0
        gaps.append("Language not in profile: " + ", ".join(unknown))
        review()
    elif level_unc:
        pts["language"] = 3
        uncertain.append("Confirm your level: " + ", ".join(l.title() for l in level_unc))
    else:
        pts["language"] = 5

    # 8. Contract (5)
    if re.search(r"\b(intern|internship|unpaid)\b", full):
        pts["contract"] = 0
        gaps.append("Internship or unpaid contract")
    else:
        pts["contract"] = 5

    if len(desc) < 250:
        uncertain.append("Short description: low confidence, open the vacancy")

    score = max(0, min(100, sum(pts.values())))
    remaining = days_left(job["closing_date"], today)
    if remaining is not None and remaining < 0:
        status = "Expired"

    if status in ("Disqualified", "Expired"):
        category = status
    elif status == "Review required" and score >= 65:
        category = "Review required"
    elif score >= 85:
        category = "Excellent fit"
    elif score >= 75:
        category = "Strong fit"
    elif score >= 65:
        category = "Good fit"
    elif score >= 50:
        category = "Possible fit"
    else:
        category = "Low priority"

    return {**job, "job_id": job_identifier(job), "score": score, "category": category,
            "status": status, "days_left": remaining, "region": region, "seen_on": job["source"],
            "strengths": " | ".join(strengths),
            "gaps": " | ".join(gaps), "uncertain": " | ".join(uncertain),
            "keywords": ", ".join(comps),
            "breakdown": "; ".join(f"{k} {v}/{WEIGHTS[k]}" for k, v in pts.items())}


def _org_key(org: str) -> str:
    toks = re.findall(r"[a-z0-9]+", fold(org))
    return toks[0] if toks else ""


def _title_core(title: str) -> str:
    first = norm_title(fold(title).split(",")[0])
    return first if len(first) >= 12 else norm_title(fold(title))


def _city(job: dict) -> str:
    """First place word of the location (or of the title after the first comma)."""
    text = job.get("location") or ",".join(str(job.get("title", "")).split(",")[1:])
    words = re.findall(r"[a-z]{3,}", fold(text))
    return words[0] if words else ""


def _same_posting(a: dict, b: dict) -> bool:
    """Same vacancy listed on two different sites (for example UNHCR Workday and UNjobs)."""
    if a["source"] == b["source"]:
        return False
    if a["url"] and b["url"] and urlsplit(a["url"]).netloc == urlsplit(b["url"]).netloc:
        return False                       # one site lists them separately: distinct postings
    if a["closing_date"] and b["closing_date"] and a["closing_date"] != b["closing_date"]:
        return False
    ca, cb = _city(a), _city(b)
    return not ca or not cb or ca == cb


def _merge_raw(primary: dict, other: dict) -> dict:
    merged = dict(primary)
    for k in ("closing_date", "location", "country", "grade", "posted_date", "contract_type", "url"):
        if not clean_text(merged.get(k)) and clean_text(other.get(k)):
            merged[k] = other[k]
    if len(clean_text(merged.get("description"))) < 400 and \
            len(clean_text(other.get("description"))) > len(clean_text(merged.get("description"))):
        merged["description"] = other["description"]
    return merged


def score_jobs(raw_jobs: list[dict[str, Any]], today: date | None = None, profile: dict | None = None) -> pd.DataFrame:
    """Score, de-duplicate and drop expired jobs.

    1. Same link (query string ignored) or, without a link, same organization/title/location: one entry.
    2. Same vacancy on two different sites (same organization, same title core, compatible place and
       closing date): merged, keeping the official source and filling missing details from the other."""
    entries: list[dict] = []
    by_id: dict[str, dict] = {}
    by_core: dict[tuple, list[dict]] = {}
    for raw in raw_jobs:
        try:
            job = score_job(raw, today, profile)
        except Exception:  # one malformed record must never break the list
            continue
        if not job["title"]:
            continue
        entry = by_id.get(job["job_id"])
        key = (_org_key(job["organization"]), _title_core(job["title"]))
        if entry is None and all(key):
            entry = next((e for e in by_core.get(key, []) if _same_posting(e["job"], job)), None)
        if entry is None:
            entry = {"raw": dict(raw), "job": job, "seen": {job["source"]}, "changed": False}
            entries.append(entry)
            if all(key):
                by_core.setdefault(key, []).append(entry)
        else:
            entry["seen"].add(job["source"])
            if job["job_id"] != entry["job"]["job_id"]:
                if int(raw.get("_prio", 0)) < int(entry["raw"].get("_prio", 0)):
                    entry["raw"] = _merge_raw(dict(raw), entry["raw"])
                else:
                    entry["raw"] = _merge_raw(entry["raw"], raw)
                entry["changed"] = True
        by_id[job["job_id"]] = entry
    rows = []
    for e in entries:
        job = score_job(e["raw"], today, profile) if e["changed"] else e["job"]
        if job["status"] == "Expired":
            continue
        job["seen_on"] = ", ".join(sorted(e["seen"]))
        rows.append(job)
    if not rows:
        return pd.DataFrame(columns=JOB_COLUMNS)
    df = pd.DataFrame(rows, columns=JOB_COLUMNS)
    df["_c"] = df["closing_date"].replace("", "9999-12-31")
    return df.sort_values(["score", "_c"], ascending=[False, True]).drop(columns="_c").reset_index(drop=True)


# =====================================================================
# 4. INTERNET ACCESS: pacing, sessions, back-off, bot-check handling
# =====================================================================

_LOCAL_PACE: dict = {"lock": threading.Lock(), "next": {}}


def pace_key(url: str) -> str:
    """All Workday tenants share one Cloudflare front, so they share one pace."""
    host = urlsplit(url).netloc.lower()
    return "myworkdayjobs.com" if host.endswith("myworkdayjobs.com") else host


def _pace(url: str, src: dict | None) -> None:
    pace = (src or {}).get("_pace") or _LOCAL_PACE
    key = pace_key(url)
    with pace["lock"]:
        now = time.time()
        slot = max(now, pace["next"].get(key, 0.0))
        pace["next"][key] = slot + PACE_SECONDS
    if slot > now:
        time.sleep(slot - now)


def _penalize(url: str, src: dict | None, seconds: float) -> None:
    """After a 429, every request to that site (from any source or user) waits."""
    pace = (src or {}).get("_pace") or _LOCAL_PACE
    key = pace_key(url)
    with pace["lock"]:
        pace["next"][key] = max(pace["next"].get(key, 0.0), time.time() + seconds)


def _session(src: dict | None, engine: str) -> Any:
    """One cookie-keeping session per source and engine (needed for Workday's session cookies)."""
    if src is None:
        return None
    sessions = src.setdefault("_sessions", {})
    if engine not in sessions:
        if engine == "cffi":
            sessions[engine] = _CffiSession(impersonate="chrome")
        else:
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            sessions[engine] = s
    return sessions[engine]


def _send(method: str, url: str, *, params: dict | None, body: dict | None, headers: dict,
          src: dict | None = None, engine: str = "std") -> Any:
    """One HTTP request with the chosen engine: 'cffi' (browser-like) or 'std' (requests)."""
    if engine == "cffi":
        s = _session(src, "cffi")
        if s is not None:
            return s.request(method, url, params=params, json=body, headers=headers, timeout=HTTP_TIMEOUT)
        return _cffi_request(method, url, params=params, json=body, headers=headers,
                             timeout=HTTP_TIMEOUT, impersonate="chrome")
    h = {**headers, "User-Agent": USER_AGENT}
    s = _session(src, "std")
    if s is not None:
        return s.request(method, url, params=params, json=body, headers=h, timeout=HTTP_TIMEOUT)
    return requests.request(method, url, params=params, json=body, headers=h, timeout=HTTP_TIMEOUT)


def _header(resp: Any, name: str) -> str:
    try:
        return str(resp.headers.get(name) or "")
    except Exception:
        return ""


def _retry_after_seconds(resp: Any) -> float | None:
    value = _header(resp, "Retry-After").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _is_bot_challenge(resp: Any) -> bool:
    if _header(resp, "cf-mitigated").lower() == "challenge":
        return True
    body = (getattr(resp, "text", "") or "")[:4000]
    return any(m in body for m in ("_cf_chl_opt", "challenge-platform", "Just a moment...",
                                   "Attention Required! | Cloudflare"))


def in_budget(src: dict) -> bool:
    return time.time() < src.get("deadline", float("inf"))


def http(method: str, url: str, *, params: dict | None = None, body: dict | None = None,
         expect: str = "json", src: dict | None = None) -> Any:
    """HTTP request with:
    - shared pacing per site, so parallel sources and users do not overload it;
    - Retry-After aware exponential back-off for 429/5xx, longer waits become a cooldown;
    - Cloudflare challenge detection, one retry with the other engine, then a clear 'blocked' error;
    - a 'throttled' flag on the source, so optional requests (vacancy texts) stop early.
    """
    headers = {"Accept": "application/json" if expect == "json" else "text/html,application/xhtml+xml,*/*",
               "Accept-Language": "en-US,en;q=0.9"}
    if src:
        headers.update(src.get("headers") or {})
    engine = "cffi" if CFFI_OK else "std"
    switched = False
    last = "unknown error"
    for attempt in range(HTTP_ATTEMPTS):
        if src is not None and not in_budget(src):
            raise SourceError(f"time budget used up ({last})" if last != "unknown error" else "time budget used up")
        _pace(url, src)
        try:
            r = _send(method, url, params=params, body=body, headers=headers, src=src, engine=engine)
        except Exception as exc:  # network errors from requests or curl_cffi
            last = f"network error ({type(exc).__name__})"
            if engine == "cffi":
                engine = "std"   # browser-like engine failing locally: continue with the standard one
            time.sleep(1.5 * (attempt + 1))
            continue
        status = int(r.status_code)
        if status in (403, 429, 503) and _is_bot_challenge(r):
            alternative = "std" if engine == "cffi" else ("cffi" if CFFI_OK else "")
            if alternative and not switched:
                switched, engine = True, alternative
                continue
            if src is not None:
                src["throttled"] = True
            raise SourceError(f"HTTP {status}: blocked by the site's bot protection (Cloudflare). "
                              "Not a rate limit; retrying now will not help", kind="blocked")
        if status == 429 or status >= 500:
            asked = _retry_after_seconds(r)
            if status == 429:
                kind = "rate"
                last = "HTTP 429 (rate limited by the site)"
                if src is not None:
                    src["throttled"] = True
            else:
                kind = "error"
                last = f"HTTP {status}"
            delay = asked if asked is not None else 3 * (2 ** attempt) + random.uniform(0, 1.5)
            if status == 429:
                _penalize(url, src, min(delay, MAX_INLINE_WAIT))
            if delay > MAX_INLINE_WAIT or attempt == HTTP_ATTEMPTS - 1:
                suffix = f"; site asks to wait {int(asked)} s" if asked else ""
                raise SourceError(last + suffix, kind=kind, retry_after=asked)
            time.sleep(delay)
            continue
        if status in (401, 403):
            raise SourceError(f"HTTP {status}: access refused (possibly bot protection)", kind="blocked")
        if status >= 400:
            raise SourceError(f"HTTP {status}")
        if expect == "json":
            try:
                return r.json()
            except ValueError as exc:
                raise SourceError("the site returned a web page instead of data (layout or protection changed)") from exc
        return r.text
    raise SourceError(last)


def safe_keyword(kw: str) -> str:
    return re.sub(r'["\';,]', " ", kw).strip()


# =====================================================================
# 5. SOURCE DETECTION AND ADAPTERS
# =====================================================================

def detect_source(url: str, name: str = "") -> dict[str, Any] | None:
    """Recognise the platform from a public careers URL."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    host = urlsplit(url).netloc.lower()
    m = re.match(r"(https://[^/]+)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+)", url)
    if m:
        return {"type": "oracle", "host": m.group(1), "site": m.group(2), "name": name or host}
    if "/wday/cxs/" in url:  # an API URL pasted instead of the careers page
        m = re.match(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/[^/]+/([^/?#]+)", url)
        if m:
            return {"type": "workday", "host": f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com",
                    "tenant": m.group(1), "site": m.group(3), "name": name or m.group(1).upper()}
    m = re.match(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
    if m:
        return {"type": "workday", "host": f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com",
                "tenant": m.group(1), "site": m.group(3), "name": name or m.group(1).upper()}
    m = re.match(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/?#]+)", url)
    if m:
        return {"type": "workday", "host": f"{m.group(2)}.{m.group(1)}.myworkdayjobs.com",
                "tenant": m.group(2), "site": m.group(3), "name": name or m.group(2).upper()}
    m = re.match(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)", url)
    if m:
        return {"type": "greenhouse", "board": m.group(1), "name": name or m.group(1)}
    m = re.match(r"https?://jobs(\.eu)?\.lever\.co/([^/?#]+)", url)
    if m:
        return {"type": "lever", "board": m.group(2), "eu": bool(m.group(1)), "name": name or m.group(2)}
    m = re.match(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", url)
    if m:
        return {"type": "ashby", "board": m.group(1), "name": name or m.group(1)}
    if "arbeitnow.com/api/job-board-api" in url:
        return {"type": "arbeitnow", "url": url, "name": name or "Arbeitnow"}
    if host.endswith("unjobs.org"):
        return {"type": "unjobs", "url": url, "name": name or "UNjobs"}
    if host == "jobs.unicef.org" or (host.endswith("unicef.org") and "/careers" in url):
        loc = re.search(r"/([a-z]{2}-[a-z]{2})(?:/|$)", urlsplit(url).path)
        prefix = "/" + (loc.group(1) if loc else "en-us")
        return {"type": "pageup", "url": f"https://jobs.unicef.org{prefix}/listing/", "origin": "https://jobs.unicef.org",
                "prefix": prefix, "unicef": True, "name": name or "UNICEF"}
    if host.endswith("pageuppeople.com"):
        parts = urlsplit(url)
        m = re.match(r"(.*?)/(?:listing|search|filter)\b", parts.path)
        return {"type": "pageup", "url": url, "origin": f"{parts.scheme}://{parts.netloc}",
                "prefix": m.group(1) if m else "", "unicef": False, "name": name or parts.netloc}
    return {"type": "web", "url": url, "name": name or host}


def _run_keywords(src: dict, keywords: list[str], fn: Callable[[str], None], found: dict) -> None:
    """One query per keyword. Stops at the first rate limit or block (it applies to every keyword),
    after two straight failures, or when the time budget is used. Raises only if nothing was found."""
    errors: list[SourceError] = []
    successes = 0
    for kw in keywords:
        if not in_budget(src):
            break
        try:
            fn(kw)
            successes += 1
        except SourceError as exc:
            errors.append(exc)
            if exc.kind in ("rate", "blocked"):
                break
            if not successes and len(errors) >= 2:
                break
    if errors and not found:
        raise errors[-1]


def _detail_order(jobs: list[dict], keywords: list[str]) -> list[dict]:
    """Fetch full texts for the most promising titles first."""
    def rank(j: dict) -> int:
        t = clean_text(j.get("title"))
        return (0 if matched(PRIMARY_TITLES, t.lower()) else 1) + (0 if title_matches_keywords(t, keywords) else 1)
    return sorted([j for j in jobs if title_is_relevant(clean_text(j.get("title")))], key=rank)


def _can_fetch_details(src: dict) -> bool:
    return in_budget(src) and not src.get("throttled")


# ---------- Oracle Recruiting Cloud (UNDP, IOM) ----------

def fetch_oracle(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    host, site, org = src["host"], src["site"], src["name"]
    api = f"{host}/hcmRestApi/resources/latest"
    found: dict[str, dict] = {}

    def query(kw: str) -> None:
        offset = 0
        while offset < max_per_kw:
            finder = (f'findReqs;siteNumber={site},keyword="{safe_keyword(kw)}",limit=25,'
                      f"offset={offset},sortBy=POSTING_DATES_DESC")
            data = http("GET", f"{api}/recruitingCEJobRequisitions", src=src,
                        params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                                "finder": finder})
            items = (data.get("items") or []) if isinstance(data, dict) else []
            reqs = (items[0].get("requisitionList") or []) if items else []
            new = 0
            for r in reqs:
                rid = str(r.get("Id") or "")
                if rid and rid not in found:
                    found[rid] = r
                    new += 1
            offset += 25
            if len(reqs) < 25 or new == 0:
                break

    _run_keywords(src, keywords, query, found)
    src["_raw_count"] = len(found)

    def end_date(rec: dict) -> str:
        for k, v in rec.items():
            if "enddate" in k.lower() and v:
                return parse_date(v)
        return ""

    jobs = []
    for rid, r in found.items():
        jobs.append({"_id": rid, "title": r.get("Title"), "organization": org, "location": r.get("PrimaryLocation"),
                     "country": r.get("PrimaryLocationCountry"), "posted_date": r.get("PostedDate"),
                     "closing_date": end_date(r), "contract_type": r.get("WorkplaceType") or "",
                     "description": r.get("ShortDescriptionStr") or "",
                     "url": f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}", "source": org})
    if deep:
        for job in _detail_order(jobs, keywords)[:DETAIL_LIMIT_PER_SOURCE]:
            if not _can_fetch_details(src):
                break
            try:
                d = http("GET", f"{api}/recruitingCEJobRequisitionDetails", src=src,
                         params={"onlyData": "true", "expand": "all",
                                 "finder": f'ById;Id="{job["_id"]}",siteNumber={site}'})
            except SourceError:
                continue
            item = ((d.get("items") or [{}])[0]) if isinstance(d, dict) else {}
            parts = [str(v) for k, v in item.items() if isinstance(v, str) and k.endswith("Str") and len(v) > 40]
            if parts:
                job["description"] = "\n\n".join(parts)
            job["closing_date"] = end_date(item) or job["closing_date"]
    return jobs


# ---------- Workday (WFP, UNHCR) ----------

def fetch_workday(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    """Warm-up visit (session cookies), then one complete read for small sites or keyword search for large ones."""
    host, tenant, site, org = src["host"], src["tenant"], src["site"], src["name"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    src["headers"] = {"Origin": f"https://{host}", "Referer": f"https://{host}/{site}"}
    try:
        http("GET", f"https://{host}/{site}", expect="text", src=src)
    except SourceError as exc:
        if exc.kind in ("blocked", "rate"):
            raise
    found: dict[str, dict] = {}

    def page(search: str, offset: int) -> dict:
        data = http("POST", f"{api}/jobs", src=src,
                    body={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search})
        if not isinstance(data, dict) or "jobPostings" not in data:
            raise SourceError("unexpected Workday response")
        return data

    def collect(search: str, limit: int, first: dict | None = None) -> None:
        offset, total = 0, None
        while offset < limit and in_budget(src):
            data = first if (first is not None and offset == 0) else page(search, offset)
            if total is None:
                total = int(data.get("total") or 0)      # only the first page reports total
            posts = data.get("jobPostings") or []
            new = 0
            for p in posts:
                path = p.get("externalPath")
                if path and path not in found:
                    found[path] = p
                    new += 1
            offset += 20
            if not posts or new == 0 or offset >= total:  # Workday loops past the end
                break

    first = page("", 0)
    total = int(first.get("total") or 0)
    mode_all = total <= WORKDAY_FETCH_ALL_MAX
    if mode_all:
        try:
            collect("", WORKDAY_FETCH_ALL_MAX, first)
        except SourceError:
            if not found:
                raise
    else:
        _run_keywords(src, keywords, lambda kw: collect(kw, max_per_kw), found)

    src["_raw_count"] = len(found)
    jobs = []
    for path, p in found.items():
        title = clean_text(p.get("title"))
        if mode_all and not (title_matches_keywords(title, keywords) or title_is_relevant(title)):
            continue                     # full listing: keep only titles related to the search
        jobs.append({"_path": path, "title": title, "organization": org, "location": p.get("locationsText"),
                     "posted_date": parse_relative_posted(p.get("postedOn", "")), "description": "",
                     "url": f"https://{host}/en-US/{site}{path}", "source": org})
    if deep:
        for job in _detail_order(jobs, keywords)[:DETAIL_LIMIT_PER_SOURCE]:
            if not _can_fetch_details(src):
                break
            try:
                info = (http("GET", f"{api}{job['_path']}", src=src) or {}).get("jobPostingInfo") or {}
            except (SourceError, AttributeError):
                continue
            job["description"] = info.get("jobDescription") or ""
            job["location"] = info.get("location") or job["location"]
            job["posted_date"] = info.get("startDate") or job["posted_date"]
            job["closing_date"] = info.get("endDate") or ""
            job["contract_type"] = info.get("timeType") or ""
            job["url"] = safe_link(info.get("externalUrl")) or job["url"]
    return jobs


# ---------- PageUp career sites (UNICEF) ----------

_PAGEUP_RESERVED = {"search", "filter", "listing", "login", "alerts", "saved", "apply"}


def pageup_job_key(href: str) -> str:
    """Identify a vacancy link. Handles '/en-us/job/596783/slug' (old) and '/jobs/<slug>' (new UNICEF design)."""
    h = html.unescape(href or "")
    m = re.search(r"/job/(\d{3,})(?:[/?#]|$)", h)
    if m:
        return "id:" + m.group(1)
    m = re.search(r"[?&](?:jobid|job_id|in_jncounter)=(\d+)", h, re.I)
    if m:
        return "id:" + m.group(1)
    m = re.search(r"/jobs/([a-z0-9][a-z0-9\-_%]{5,})/?(?:[?#]|$)", h, re.I)
    if m and m.group(1).lower() not in _PAGEUP_RESERVED:
        return "slug:" + m.group(1).lower()
    return ""


def parse_pageup_html(page: str, page_url: str, org: str) -> dict[str, dict]:
    """Extract vacancies from a PageUp listing/search/filter page (desktop, mobile or new layout)."""
    anchors = [(m, pageup_job_key(m.group(1))) for m in _ANCHOR_RE.finditer(page or "")]
    anchors = [(m, k) for m, k in anchors if k]
    if not anchors:
        return {}
    # Mobile layout shows 'Closes <date>' BEFORE the title; desktop and new layouts show it after.
    lead = clean_text(page[max(0, anchors[0][0].start() - 400):anchors[0][0].start()])
    closes_before = bool(CLOSING_RE.search(lead[-160:]))
    found: dict[str, dict] = {}
    for i, (m, key) in enumerate(anchors):
        label = clean_text(m.group(2))
        prev_end = anchors[i - 1][0].end() if i else max(0, m.start() - 600)
        next_start = anchors[i + 1][0].start() if i + 1 < len(anchors) else m.end() + 3000
        segment = page[m.end():min(next_start, m.end() + 3000)]
        after = clean_text(segment)
        before = clean_text(page[max(prev_end, m.start() - 600):m.start()])
        if closes_before:
            hits = list(CLOSING_RE.finditer(before))
            closing = parse_date(hits[-1].group(1)) if hits else ""
            after = CLOSING_RE.split(after)[0] if CLOSING_RE.search(after) else after
        else:
            hit = CLOSING_RE.search(after)
            closing = parse_date(hit.group(1)) if hit else ""
        lines = html_lines(segment)
        first = lines[0] if lines else ""
        loc = re.match(r"in\s+(.{2,60})$", first) or re.search(r"Location:\s*([^\n|]{2,60}?)(?:\s+Deadline|\s+Closing|$)",
                                                               "\n".join(lines[:6]))
        url = urljoin(page_url, html.unescape(m.group(1)))
        rec = found.get(key)
        if label.lower() in _GENERIC_LINK_TEXT:
            if rec and not rec["closing_date"] and closing:
                rec["closing_date"] = closing
            continue
        if rec is None or len(label) > len(rec["title"]):
            found[key] = {"title": label, "organization": org, "description": after[:3000],
                          "location": loc.group(1).strip() if loc else "",
                          "closing_date": closing or (rec or {}).get("closing_date", ""),
                          "url": url, "source": org}
    return found


def pageup_pages(src: dict, keywords: list[str]) -> list[str]:
    origin, prefix = src["origin"], src["prefix"]
    pages = [f"{origin}{prefix}/search/?search-keyword={quote_plus(k)}" for k in keywords]
    if src.get("unicef"):
        pages += [f"{origin}{prefix}/filter/?category={quote_plus(c)}" for c in UNICEF_CATEGORIES]
        pages += [f"{origin}/mob/cw{prefix}/listing/", f"{origin}/"]
    pages += [f"{origin}{prefix}/listing/", src["url"]]
    return list(dict.fromkeys(pages))[:MAX_LISTING_PAGES]


def fetch_pageup(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    """Keyword searches, category filters and listings, merged by job key AND by title
    (the same vacancy can appear with an old numeric link and a new slug link)."""
    by_title: dict[str, dict] = {}
    errors: list[SourceError] = []
    diagnostics: list[str] = []
    for url in pageup_pages(src, keywords):
        if not in_budget(src) or src.get("throttled"):
            break
        try:
            page = http("GET", url, expect="text", src=src)
        except SourceError as exc:
            errors.append(exc)
            if exc.kind in ("blocked", "rate"):
                break
            continue
        got = list(parse_pageup_html(page, url, src["name"]).values())
        for ld in parse_jsonld_jobs(page):
            if ld["title"] and safe_link(ld["url"]):
                got.append({**ld, "organization": src["name"], "source": src["name"]})
        if not got and len(diagnostics) < 2:
            title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
            template = "unfilled page template" if "{pages}" in page or "{search-keyword" in page else ""
            diagnostics.append(f"'{clean_text(title.group(1))[:50] if title else 'no title'}', "
                               f"{len(page)} characters, 0 vacancy links{', ' + template if template else ''}")
        for rec in got:
            k = norm_title(rec["title"])
            old = by_title.get(k)
            if old is None:
                by_title[k] = rec
                continue
            old["closing_date"] = old.get("closing_date") or rec.get("closing_date", "")
            old["location"] = old.get("location") or rec.get("location", "")
            if "/mob/" in old["url"] and "/mob/" not in rec["url"]:
                old["url"] = rec["url"]
            if len(clean_text(rec.get("description"))) > len(clean_text(old.get("description"))):
                old["description"] = rec["description"]
    if not by_title:
        if errors and errors[-1].kind in ("blocked", "rate"):
            raise errors[-1]
        reason = "; ".join(diagnostics) or (str(errors[-1]) if errors else "no pages read")
        raise SourceError(f"no vacancies read ({reason})", kind="empty")
    jobs = list(by_title.values())
    src["_raw_count"] = len(jobs)
    if deep:
        for job in _detail_order(jobs, keywords)[:DETAIL_LIMIT_PER_SOURCE]:
            if not _can_fetch_details(src):
                break
            try:
                enrich_from_page(job, http("GET", job["url"], expect="text", src=src))
            except SourceError:
                continue
    return jobs


# ---------- UNjobs (fallback for UNHCR and UNICEF; any organization or skills page) ----------

def unjobs_location(label: str) -> str:
    """'M&E Officer, NO-1, Temporary Position, 364 days, Sarajevo, Bosnia & Herzegovina, #00139027' -> 'Sarajevo, Bosnia & Herzegovina'."""
    parts = [p.strip() for p in label.split(",")[1:]]
    places = [p for p in parts if p and not re.match(r"^[#\d(]", p) and not extract_grade(p)
              and not re.search(r"\b(temporary|position|days?|months?|consultan|appointment|roster|office based|"
                                r"home based|remote|re-?advertis|nationals?)\b", p, re.I)]
    return ", ".join(places[-2:])


def parse_unjobs_html(page: str, page_url: str, default_org: str) -> tuple[dict[str, dict], str]:
    """Vacancy links ('/vacancies/...') with organization and 'Closing date:' text; plus the 'Next' page link."""
    all_anchors = list(_ANCHOR_RE.finditer(page or ""))
    next_url = ""
    for m in all_anchors:
        if re.match(r"^next\b", clean_text(m.group(2)).lower()):
            next_url = urljoin(page_url, html.unescape(m.group(1)))
            break
    vac = [m for m in all_anchors if "/vacancies/" in m.group(1) and clean_text(m.group(2)).lower() not in _GENERIC_LINK_TEXT]
    jobs: dict[str, dict] = {}
    for i, m in enumerate(vac):
        label = clean_text(m.group(2))
        url = urljoin(page_url, html.unescape(m.group(1)))
        key = canonical_url(url)
        if not key or key in jobs:
            continue
        nxt = vac[i + 1].start() if i + 1 < len(vac) else m.end() + 2500
        lines = html_lines(page[m.end():min(nxt, m.end() + 2500)])
        text = " ".join(lines)
        hit = CLOSING_RE.search(text)
        org = ""
        for j, line in enumerate(lines):
            if re.search(r"updated:", line, re.I):
                org = re.split(r"updated:", line, flags=re.I)[0].strip(" -·|")
                if not org and j > 0:
                    org = lines[j - 1]
                break
        jobs[key] = {"title": label, "organization": org if 2 < len(org) < 140 else default_org,
                     "location": unjobs_location(label),
                     "closing_date": parse_date(hit.group(1)) if hit else "",
                     "description": text[:1500], "url": url, "source": default_org}
    return jobs, next_url


def fetch_unjobs(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    url, found, pages = src["url"], {}, 0
    while url and pages < UNJOBS_MAX_PAGES and in_budget(src) and not src.get("throttled"):
        try:
            page = http("GET", url, expect="text", src=src)
        except SourceError:
            if pages == 0:
                raise
            break
        pages += 1
        got, nxt = parse_unjobs_html(page, url, src["name"])
        for k, v in got.items():
            found.setdefault(k, v)
        url = nxt if nxt and nxt != url else ""
    src["_raw_count"] = len(found)
    if not found:
        if pages and "No vacancies to display" in page:
            return []                     # valid page, simply no vacancies at this duty station
        raise SourceError("no vacancies read from UNjobs page", kind="empty")
    jobs = list(found.values())
    if deep:
        for job in _detail_order(jobs, keywords)[:DETAIL_LIMIT_PER_SOURCE]:
            if not _can_fetch_details(src):
                break
            try:
                enrich_from_page(job, http("GET", job["url"], expect="text", src=src))
            except SourceError:
                continue
    return jobs


# ---------- Other platforms ----------

def fetch_arbeitnow(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    jobs = []
    for page_no in (1, 2):
        data = http("GET", src["url"], params={"page": page_no}, src=src)
        rows = (data.get("data") or []) if isinstance(data, dict) else []
        src["_raw_count"] = src.get("_raw_count", 0) + len(rows)
        for j in rows:
            title = clean_text(j.get("title"))
            if not title_is_relevant(title):      # broad feed: keep only target role families
                continue
            jobs.append({"title": title, "organization": j.get("company_name"),
                         "location": "Remote" if j.get("remote") else j.get("location"),
                         "posted_date": j.get("created_at"), "description": j.get("description"),
                         "contract_type": ", ".join(j.get("job_types") or []),
                         "url": j.get("url"), "source": "Arbeitnow"})
        if not rows:
            break
    return jobs


def fetch_greenhouse(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    data = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs",
                params={"content": "true"}, src=src)
    return [{"title": j.get("title"), "organization": j.get("company_name") or src["name"],
             "location": (j.get("location") or {}).get("name"), "url": j.get("absolute_url"),
             "posted_date": j.get("first_published") or j.get("updated_at"),
             "description": j.get("content"), "source": src["name"]} for j in data.get("jobs") or []]


def fetch_lever(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    host = "api.eu.lever.co" if src.get("eu") else "api.lever.co"
    data = http("GET", f"https://{host}/v0/postings/{src['board']}", params={"mode": "json"}, src=src)
    if not isinstance(data, list):
        raise SourceError("unexpected Lever response")
    return [{"title": j.get("text"), "organization": src["name"],
             "location": (j.get("categories") or {}).get("location"), "url": j.get("hostedUrl"),
             "contract_type": (j.get("categories") or {}).get("commitment"),
             "posted_date": j.get("createdAt"),
             "description": (j.get("descriptionPlain") or "") + " " +
                            " ".join(clean_text(x.get("content")) for x in j.get("lists") or []),
             "source": src["name"]} for j in data]


def fetch_ashby(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    data = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}", src=src)
    return [{"title": j.get("title"), "organization": src["name"], "location": j.get("location"),
             "url": j.get("jobUrl"), "posted_date": j.get("publishedAt"),
             "contract_type": j.get("employmentType"),
             "description": j.get("descriptionPlain") or j.get("descriptionHtml"), "source": src["name"]}
            for j in data.get("jobs") or [] if j.get("isListed", True)]


def parse_feed(text: str, org: str) -> list[dict]:
    """RSS 2.0 / Atom items."""
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise SourceError("not a valid RSS/Atom feed") from exc
    jobs = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        children = {c.tag.split("}")[-1]: c for c in node}

        def txt(*names: str) -> str:
            for n in names:
                el = children.get(n)
                if el is not None:          # Element truthiness is unreliable: compare with None
                    return el.text or ""
            return ""

        link = children.get("link")
        href = (link.get("href") or link.text or "") if link is not None else ""
        description = txt("description", "summary", "content")
        jobs.append({"title": txt("title"), "organization": org, "url": href,
                     "posted_date": txt("pubDate", "published", "updated"),
                     "closing_date": find_closing(clean_text(description)),
                     "description": description, "source": org})
    return jobs


# Vacancy detail links on job boards: /posao/<id>/<slug> (MojPosao), /oglasi/<slug>/<id> (Klix),
# /job/<...>, /jobs/<slug>, /vacancies/<id>, /job_display/<id>/<slug> (EuroBrussels), ?jobId=123, ...
_JOB_PATH_RE = re.compile(r"/(?:posao|poslovi/oglas|oglas|oglasi|job|jobs|vacancy|vacancies|position|positions|"
                          r"job_display|jobdetails?|job-details?|stellenangebot\w*|stelle|careers?/job)/[^/?#\s]{2,}", re.I)
_JOB_QUERY_RE = re.compile(r"[?&](?:jobid|job_id|vacancyid|oglasid|reqid)=\w+", re.I)
_NOT_JOB_RE = re.compile(r"/(?:kat|kategorij\w*|category|categories|search|pretraga\w*|filter|login|register|"
                         r"registracija|alerts?|saved|apply|prijava)(?:/|$|\?)|^(?:mailto|javascript|tel):|^#", re.I)
_BADGES = {"izdvojeni", "izdvojeno", "novi posao", "novi oglas", "premium", "premium oglas", "featured", "new job",
           "new", "novo", "hitno", "top", "p", "p+", "b", "spotlight job", "istaknuti oglas", "standardni oglas"}
_GENERIC_ANCHOR = _GENERIC_LINK_TEXT | {"prijavi se", "pogledaj oglas", "saznajte vise", "vidi sve", "vise",
                                       "detaljnije", "brza prijava", "pogledaj", "save this job", "apply now"}
_COMPANY_RE = re.compile(r"\b(?:d\.?\s?o\.?\s?o\.?|doo|d\.\s?d\.|a\.\s?d\.|gmbh|ltd|llc|s\.r\.l\.|plc)(?![a-z])", re.I)


def is_job_link(href: str) -> bool:
    h = html.unescape(href or "").strip()
    return bool(h) and not _NOT_JOB_RE.search(h) and bool(_JOB_PATH_RE.search(h) or _JOB_QUERY_RE.search(h))


def parse_listing_html(page: str, page_url: str, org: str) -> tuple[list[dict], int]:
    """Generic job-board listing: JSON-LD JobPostings plus vacancy links with the text around them.
    Returns (records, number of vacancy links seen)."""
    records: dict[str, dict] = {}
    for ld in parse_jsonld_jobs(page):
        link = safe_link(urljoin(page_url, ld["url"])) if ld["url"] else ""
        if ld["title"] and link:
            records[canonical_url(link)] = {**ld, "url": link, "organization": ld["organization"] or org,
                                            "closing_date": parse_date(ld["closing_date"])}
    anchors = [m for m in _ANCHOR_RE.finditer(page or "") if is_job_link(m.group(1))]
    for i, m in enumerate(anchors):
        url = safe_link(urljoin(page_url, html.unescape(m.group(1)).strip()))
        key = canonical_url(url)
        if not key or key == canonical_url(page_url):
            continue
        inner = html_lines(m.group(2))
        lines = [x for x in inner if fold(x) not in _BADGES and len(x) >= 4]
        title = lines[0] if lines else ""
        if not title or fold(title) in _GENERIC_ANCHOR or len(title) > 220:
            if key in records and not records[key].get("closing_date"):
                nxt = anchors[i + 1].start() if i + 1 < len(anchors) else m.end() + 1200
                records[key]["closing_date"] = find_closing(clean_text(page[m.end():min(nxt, m.end() + 1200)]), True)
            continue
        nxt = anchors[i + 1].start() if i + 1 < len(anchors) else m.end() + 1500
        context_lines = lines[1:] + html_lines(page[m.end():min(nxt, m.end() + 1500)])
        context = " · ".join(context_lines)[:1500]
        company = next((x for x in context_lines[:6] if matched(TARGET_ORGS, x, True) and len(x) < 120), "") or \
            next((x for x in context_lines[:6] if _COMPANY_RE.search(x) and len(x) < 120), "")
        place = next((x for x in context_lines[:6] if len(x) < 80 and x != company
                      and region_of(x) != REGION_OTHER), "")
        rec = records.get(key)
        if rec is None or len(title) > len(rec["title"]):
            records[key] = {"title": title, "organization": company or (rec or {}).get("organization") or org,
                            "url": url, "description": context,
                            "closing_date": (rec or {}).get("closing_date") or find_closing(context, True),
                            "location": (rec or {}).get("location") or place, "source": org}
    return list(records.values()), max(len(anchors), len(records))


def fetch_web(src: dict, keywords: list[str], deep: bool, max_per_kw: int) -> list[dict]:
    """Any job board listing page or RSS/Atom feed. Broad boards are filtered to relevant titles."""
    page = http("GET", src["url"], expect="text", src=src)
    head = page.lstrip()[:300].lower()
    if head.startswith("<?xml") or "<rss" in head or "<feed" in head:
        raw = parse_feed(page, src["name"])
        seen = len(raw)
    else:
        raw, seen = parse_listing_html(page, src["url"], src["name"])
    src["_raw_count"] = seen
    if not raw:
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        raise SourceError(f"no vacancy links found ('{clean_text(title.group(1))[:50] if title else 'no title'}', "
                          f"{len(page)} characters; the page may load its jobs with scripts)", kind="empty")
    hint = clean_text(src.get("country"))
    hint_region = region_of(hint) if hint else ""
    jobs = []
    for job in raw:
        title = clean_text(job.get("title"))
        if not (title_is_relevant(title) or title_matches_keywords(title, keywords)):
            continue
        if hint and not job.get("country"):
            place_region = region_of(f"{job.get('location', '')} {title} {clean_text(job.get('description'))[:300]}")
            if place_region in (REGION_OTHER, hint_region):
                job["country"] = hint
        jobs.append(job)
    if deep:
        for job in _detail_order(jobs, keywords)[:DETAIL_LIMIT_PER_SOURCE]:
            if not _can_fetch_details(src):
                break
            try:
                enrich_from_page(job, http("GET", job["url"], expect="text", src=src))
            except SourceError:
                continue
    return jobs


FETCHERS: dict[str, Callable[..., list[dict]]] = {
    "oracle": fetch_oracle, "workday": fetch_workday, "pageup": fetch_pageup, "unjobs": fetch_unjobs,
    "arbeitnow": fetch_arbeitnow, "greenhouse": fetch_greenhouse, "lever": fetch_lever,
    "ashby": fetch_ashby, "web": fetch_web,
}


SOURCE_PRIORITY = {"unjobs": 2, "web": 1}      # official career sites (0) win when the same job is merged


def _fetch_url(url: str, name: str, keywords: list[str], deep: bool, depth: int,
               budget: float, pace: dict | None, country: str = "") -> tuple[list[dict], dict]:
    spec = detect_source(url, name)
    if not spec:
        raise SourceError("URL not recognised")
    spec["deadline"] = time.time() + budget
    spec["country"] = country
    if pace is not None:
        spec["_pace"] = pace
    try:
        jobs = FETCHERS[spec["type"]](spec, keywords, deep, depth)
    finally:
        for session in (spec.pop("_sessions", None) or {}).values():
            try:
                session.close()
            except Exception:
                pass
    prio = SOURCE_PRIORITY.get(spec["type"], 0)
    for j in jobs:
        j.pop("_id", None)
        j.pop("_path", None)
        j["source"] = name
        j["_prio"] = prio
        if country and not j.get("country") and spec["type"] != "web":
            loc_region = region_of(f"{j.get('location', '')} {j.get('title', '')}")
            if loc_region in (REGION_OTHER, region_of(country)):
                j["country"] = country
    return jobs, spec


def fetch_one(source: dict, keywords: list[str], deep: bool, depth: int, pace: dict | None = None) -> tuple[list[dict], dict]:
    """Search one source, then its fallback when the main site fails or returns nothing. Never raises."""
    started = time.time()
    spec0 = detect_source(source.get("url", ""), source.get("name", ""))
    health = {"source": source.get("name", ""), "platform": spec0["type"] if spec0 else "invalid URL",
              "status": "OK", "jobs": 0, "seconds": 0.0, "message": "", "retry_after": None}
    jobs: list[dict] = []
    primary_error: SourceError | None = None
    spec: dict = {}
    try:
        jobs, spec = _fetch_url(source["url"], source["name"], keywords, deep, depth, SOURCE_TIME_BUDGET, pace,
                                source.get("country", ""))
    except SourceError as exc:
        primary_error = exc
    except Exception as exc:  # unexpected format change must not crash the app
        primary_error = SourceError(f"{type(exc).__name__}: {exc}"[:200])

    fallback = (source.get("fallback") or "").strip()
    # Fallback when the main site failed, or answered but no vacancy at all could be read from it.
    # A site that was read correctly but has no relevant vacancy is NOT a failure.
    unreadable = not jobs and not primary_error and spec.get("_raw_count", 1) == 0
    if (primary_error or unreadable) and fallback:
        reason = str(primary_error) if primary_error else "no vacancies could be read from the main site"
        try:
            fb_jobs, _ = _fetch_url(fallback, source["name"], keywords, deep, depth, FALLBACK_TIME_BUDGET, pace,
                                    source.get("country", ""))
            if fb_jobs:
                jobs = fb_jobs
                health["status"] = "OK (via fallback)"
                health["message"] = f"Main site: {reason}. Results from {urlsplit(fallback).netloc}"
                primary_error = None
            elif not primary_error:
                health["message"] = "No matching vacancies right now"
        except Exception as exc:
            if primary_error:
                primary_error = SourceError(f"{primary_error} | fallback: {exc}", primary_error.kind,
                                            primary_error.retry_after)
    if primary_error:
        jobs = []
        health["status"] = "Failed"
        health["message"] = str(primary_error)
        health["retry_after"] = primary_error.retry_after
    elif health["status"] == "OK":
        if not jobs:
            health["message"] = "No matching vacancies right now"
        elif spec.get("throttled"):
            health["status"] = "OK (partial)"
            health["message"] = "The site asked the app to slow down; some full vacancy texts were skipped"
    health["jobs"] = len(jobs)
    health["seconds"] = round(time.time() - started, 1)
    return jobs, health


@st.cache_resource
def result_store() -> dict:
    """Shared across sessions: last success and last failure per source, pacing, and a search lock."""
    return {"lock": threading.Lock(), "search_lock": threading.Lock(), "entries": {},
            "pace": {"lock": threading.Lock(), "next": {}}}


def _entry_key(source: dict, keywords: tuple[str, ...], deep: bool, depth: int) -> str:
    raw = json.dumps([source.get("name"), source.get("url"), source.get("fallback", ""), source.get("country", ""),
                      keywords, deep, depth],
                     sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cooldown_seconds(fail: dict) -> float:
    asked = fail["health"].get("retry_after") or 0
    return max(FAIL_RETRY_MINUTES * 60, min(float(asked), MAX_COOLDOWN_HOURS * 3600))


def search_all(sources: list[dict], keywords: tuple[str, ...], deep: bool, depth: int, force: bool) -> dict:
    """Search enabled sources. Reuses fresh results; retries failures only after a cooldown that respects
    the site's Retry-After; keeps the last good results of a failing source visible. A search lock stops
    two users from searching the same sites at the same moment."""
    store = result_store()
    enabled = [s for s in sources if s.get("enabled")]
    with store["search_lock"]:
        now = time.time()
        plan, todo = [], []
        for s in enabled:
            key = _entry_key(s, keywords, deep, depth)
            with store["lock"]:
                entry = dict(store["entries"].get(key, {}))
            ok, fail = entry.get("ok"), entry.get("fail")
            fresh_ok = bool(ok) and now - ok["ts"] < CACHE_HOURS * 3600
            cooling = bool(fail) and now - fail["ts"] < _cooldown_seconds(fail) and (not ok or fail["ts"] > ok["ts"])
            if not force and cooling:
                plan.append((s, key, "cooldown"))
            elif not force and fresh_ok:
                plan.append((s, key, "cached"))
            else:
                plan.append((s, key, "fetch"))
                todo.append((s, key))
        if todo:
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
                outcomes = list(pool.map(
                    lambda sk: (sk[1], fetch_one(sk[0], list(keywords), deep, depth, store["pace"])), todo))
            with store["lock"]:
                for key, (jobs, h) in outcomes:
                    e = store["entries"].setdefault(key, {})
                    if h["status"].startswith("OK"):
                        e["ok"] = {"ts": time.time(), "jobs": jobs, "health": h}
                        e.pop("fail", None)
                    else:
                        e["fail"] = {"ts": time.time(), "health": h}
                cutoff = time.time() - 48 * 3600     # forget results of old keyword sets
                for k in [k for k, v in store["entries"].items()
                          if max([x["ts"] for x in v.values() if isinstance(x, dict) and "ts" in x] or [0]) < cutoff]:
                    store["entries"].pop(k, None)

    raw: list[dict] = []
    health: list[dict] = []
    with store["lock"]:
        entries = {k: dict(v) for k, v in store["entries"].items()}
    for s, key, mode in plan:
        e = entries.get(key, {})
        ok, fail = e.get("ok"), e.get("fail")
        if ok and (not fail or ok["ts"] >= fail["ts"]):
            h = dict(ok["health"])
            raw.extend(ok["jobs"])
            if mode == "cached":
                h["status"] = h["status"] + " (cached)" if h["status"] != "OK" else "OK (cached)"
        elif fail:
            h = dict(fail["health"])
            retry_in = max(0, int((_cooldown_seconds(fail) - (time.time() - fail["ts"])) / 60))
            if ok:  # keep last good results visible
                raw.extend(ok["jobs"])
                h["jobs"] = len(ok["jobs"])
                h["status"] = "Failed, showing last results"
                h["message"] += " | showing results from " + datetime.fromtimestamp(ok["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            h["message"] += f" | automatic retry in about {retry_in} min (or press Search now)"
        else:
            h = {"source": s["name"], "platform": "", "status": "Not searched", "jobs": 0, "seconds": 0.0,
                 "message": "", "retry_after": None}
        last_ts = max([x["ts"] for x in (ok, fail) if x] or [time.time()])
        h["last_checked"] = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        health.append(h)
    return {"jobs": raw, "health": health,
            "searched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


# =====================================================================
# 6. EXPORTS, TRACKER, PROMPTS
# =====================================================================

def csv_bytes(df: pd.DataFrame) -> bytes:
    """CSV protected against spreadsheet formula injection."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].astype(str).map(lambda v: "'" + v if v[:1] in ("=", "+", "-", "@") else v)
    return out.to_csv(index=False).encode("utf-8")


def shortlist_md(df: pd.DataFrame) -> str:
    lines = [f"# Damir Job Radar shortlist ({date.today().isoformat()})", ""]
    for _, r in df.head(30).iterrows():
        lines += [f"## {r['title']} | {r['organization']}",
                  f"- Score: {r['score']} ({r['category']})", f"- Grade: {r['grade'] or 'not stated'}",
                  f"- Location: {r['location'] or 'not stated'}", f"- Closing: {r['closing_date'] or 'not stated'}",
                  f"- Strengths: {r['strengths'] or '-'}", f"- Gaps: {r['gaps'] or '-'}",
                  f"- Verify: {r['uncertain'] or '-'}", f"- Link: {r['url']}", ""]
    return "\n".join(lines)


def tailoring_prompt(r: pd.Series) -> str:
    return (
        "Act as an HR prescreener and ATS specialist. Evaluate my fit for this vacancy and give an "
        "apply / do-not-apply recommendation. List hard requirements, gaps, ATS keywords and likely "
        "screening questions. Then draft a tailored CV and cover letter. Broaden the framing, not the "
        "facts. Do not invent experience.\n\n"
        f"Title: {r['title']}\nOrganization: {r['organization']}\nGrade: {r['grade']}\n"
        f"Location: {r['location']}\nClosing date: {r['closing_date']}\nLink: {r['url']}\n\n"
        f"Vacancy text:\n{str(r['description'])[:7000]}"
    )


def tracker() -> pd.DataFrame:
    if "tracker" not in st.session_state:
        st.session_state.tracker = pd.DataFrame(columns=["job_id", "state", "note"])
    return st.session_state.tracker


def set_state_cb(job_id: str, widget_key: str) -> None:
    """Selectbox callback: update tracker without a rerun loop; keeps an existing note."""
    state = st.session_state.get(widget_key, "")
    t = tracker()
    old_note = t.loc[t["job_id"] == job_id, "note"]
    note = str(old_note.iloc[0]) if len(old_note) else ""
    t = t[t["job_id"] != job_id]
    if state:
        t = pd.concat([t, pd.DataFrame([{"job_id": job_id, "state": state, "note": note}])], ignore_index=True)
    st.session_state.tracker = t.reset_index(drop=True)


BADGE = {"Excellent fit": "🟢", "Strong fit": "🟢", "Good fit": "🟡", "Possible fit": "🟠",
         "Review required": "🔵", "Low priority": "⚪", "Disqualified": "🔴"}


def job_card(r: pd.Series, tab: str, track: bool = True) -> None:
    dl = pd.to_numeric(pd.Series([r.get("days_left")]), errors="coerce").iloc[0]
    when = "no closing date" if pd.isna(dl) else ("closes today" if int(dl) == 0 else f"{int(dl)} days left")
    key = hashlib.md5(f"{tab}|{r['job_id']}".encode()).hexdigest()[:12]
    with st.expander(f"{int(r['score']):>3} · {r['title']} · {r['organization']} · {when}"):
        left, right = st.columns([4, 1])
        with left:
            st.markdown(f"{BADGE.get(r['category'], '⚪')} **{r['category']}** · **{int(r['score'])}/100**"
                        + (f" · Grade **{r['grade']}**" if r["grade"] else ""))
            st.caption(f"{r['location'] or r['country'] or 'Location not stated'} · {r['region']} · "
                       f"Posted {r['posted_date'] or 'n/a'} · Closes {r['closing_date'] or 'not stated'} · "
                       f"Source: {r['seen_on'] or r['source']}")
            for head, field in (("Strengths", "strengths"), ("Gaps", "gaps"), ("Verify", "uncertain")):
                if r[field]:
                    st.markdown(f"**{head}**\n" + "\n".join(f"- {x}" for x in str(r[field]).split(" | ")))
            st.caption("Score breakdown: " + str(r["breakdown"]))
        with right:
            if r["url"]:
                st.link_button("Open / Apply", r["url"], width="stretch")
            if track:
                row = tracker().loc[tracker()["job_id"] == r["job_id"]]
                cur = row["state"].iloc[0] if len(row) and row["state"].iloc[0] in TRACKER_STATES else ""
                wkey = f"trk_{key}"
                st.session_state[wkey] = cur      # keep every copy of this job in sync with the tracker
                st.selectbox("Status", TRACKER_STATES, key=wkey, on_change=set_state_cb, args=(r["job_id"], wkey))
                if len(row) and str(row["note"].iloc[0]).strip():
                    st.caption("Note: " + str(row["note"].iloc[0]))
        c1, c2 = st.columns(2)
        with c1.popover("Vacancy text", width="stretch"):
            st.text(str(r["description"])[:8000] or "No description captured. Open the vacancy link.")
        with c2.popover("CV tailoring prompt", width="stretch"):
            st.code(tailoring_prompt(r), language=None)


def show_cards(df: pd.DataFrame, tab: str, track: bool = True, page_size: int = 25) -> None:
    if df.empty:
        st.info("No vacancies here under the current filters.")
        return
    pages = max(1, -(-len(df) // page_size))
    page = st.selectbox("Page", list(range(1, pages + 1)), key=f"page_{tab}_{pages}") if pages > 1 else 1
    for _, r in df.iloc[(page - 1) * page_size: page * page_size].iterrows():
        job_card(r, tab, track)


# =====================================================================
# 7. USER INTERFACE
# =====================================================================

st.set_page_config(page_title=APP_TITLE, page_icon="🎯", layout="wide")

ss = st.session_state
ss.setdefault("keywords", "\n".join(DEFAULT_KEYWORDS))
if "pending_keywords" in ss:              # applied before the widget exists
    ss.keywords = ss.pop("pending_keywords")
ss.setdefault("sources", [dict(s) for s in DEFAULT_SOURCES])
ss.setdefault("extra_jobs", [])          # imported CSV + analysed vacancies
ss.setdefault("analysis", None)
ss.setdefault("force_search", False)
tracker()

st.title("🎯 Damir Job Radar")

# ---------------- Sidebar ----------------
with st.sidebar:
    st.header("Automatic search")
    kw_text = st.text_area("Search keywords (one per line)", key="keywords", height=180)
    keywords = tuple(dict.fromkeys(k.strip() for k in kw_text.splitlines() if k.strip()))[:20]
    deep = st.toggle("Read full vacancy texts (slower, more accurate)", value=True)
    depth = st.select_slider("Results per keyword per site", options=[20, 40, 60], value=20)
    if st.button("🔄 Search now", type="primary", width="stretch"):
        ss.force_search = True
    st.caption(f"Runs automatically on open. Results refresh every {CACHE_HOURS} hours; failed sources "
               f"retry after {FAIL_RETRY_MINUTES} minutes or when the site allows.")

    with st.expander("My profile"):
        fr_ok = st.checkbox("French level confirmed for job requirements", value=False)
        it_ok = st.checkbox("Italian level confirmed for job requirements", value=False)
        start_year = st.number_input("Professional experience since", 1980, date.today().year,
                                     PROFILE["experience_start_year"])
    extra_confirmed = [lang for lang, ok in (("french", fr_ok), ("italian", it_ok)) if ok]
    profile = {"experience_start_year": int(start_year),
               "languages_confirmed": PROFILE["languages_confirmed"] + extra_confirmed,
               "languages_uncertain": [l for l in PROFILE["languages_uncertain"] if l not in extra_confirmed]}

    st.header("Filters")
    q = st.text_input("Search in results")
    min_score = st.slider("Minimum score", 0, 100, 55, 5)
    regions = st.multiselect("Region", REGION_NAMES, placeholder="All regions")
    families = st.multiselect("Grade type", GRADE_FAMILIES, placeholder="All grade types")
    hide_review = st.checkbox("Hide 'Review required'", value=False)
    loc_q = st.text_input("Location contains")
    window = st.selectbox("Closing within", ["Any", "7 days", "14 days", "30 days"])
    sort_by = st.selectbox("Sort by", ["Best score", "Closing soonest", "Newest"])
    hide_rejected = st.checkbox("Hide rejected / not interested", value=True)

# ---------------- Search ----------------
force = bool(ss.force_search)
ss.force_search = False
with st.spinner("Searching UN agencies, OSCE, local and European job sites. A full search can take two to three minutes..."):
    result = search_all(ss.sources, keywords, deep, depth, force)

jobs = score_jobs(result["jobs"] + ss.extra_jobs, profile=profile)
health = pd.DataFrame(result["health"])
problems = health[~health["status"].str.startswith("OK")] if not health.empty else health
fallbacks = health[health["status"].str.contains("fallback")] if not health.empty else health

st.caption(f"Last check: **{result['searched_at']}** · {len(result['jobs'])} vacancies collected · "
           f"{len(jobs)} active after de-duplication and expiry check")
if not health.empty and len(problems) == len(health) and not result["jobs"]:
    st.error("All sources failed. Open the Sources tab for reasons. You can still analyse vacancies or import a CSV.")
elif len(problems):
    st.warning("Source issues: " + ", ".join(f"{r['source']} ({r['status']})" for _, r in problems.iterrows())
               + ". Details in the Sources tab. Other sources are working normally.")
if len(fallbacks):
    st.info("Using backup pages for: " + ", ".join(fallbacks["source"]) + ". Links open the backup listing, "
            "which links to the official vacancy.")

# ---------------- Filtering ----------------
f = jobs[(jobs["score"] >= min_score) & (jobs["category"] != "Disqualified")].copy()
if regions:
    f = f[f["region"].isin(regions)]
if families:
    f = f[f["grade_family"].isin(families)]
if hide_review:
    f = f[f["category"] != "Review required"]
if q:
    blob = (f["title"] + " " + f["organization"] + " " + f["description"]).str.lower()
    f = f[blob.str.contains(q.lower(), regex=False)]
if loc_q:
    f = f[(f["location"] + " " + f["country"]).str.contains(loc_q, case=False, regex=False)]
if window != "Any":
    d = pd.to_numeric(f["days_left"], errors="coerce")
    f = f[d.between(0, int(window.split()[0]))]
if hide_rejected and not tracker().empty:
    hidden = tracker().loc[tracker()["state"].isin(["Rejected", "Not interested"]), "job_id"]
    f = f[~f["job_id"].isin(hidden)]
if sort_by == "Closing soonest":
    f = f.sort_values("days_left", key=lambda c: pd.to_numeric(c, errors="coerce"), na_position="last")
elif sort_by == "Newest":
    f = f.sort_values("posted_date", ascending=False, na_position="last")

posted = pd.to_datetime(jobs["posted_date"], errors="coerce")
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Active vacancies", len(jobs))
m2.metric("Excellent / strong", int(jobs["category"].isin(["Excellent fit", "Strong fit"]).sum()))
m3.metric("BiH & region", int(jobs["region"].isin([REGION_BIH, REGION_WB]).sum()))
m4.metric("New (7 days)", int((posted >= pd.Timestamp(date.today() - timedelta(days=7))).sum()))
m5.metric("Closing ≤ 7 days", int(pd.to_numeric(jobs["days_left"], errors="coerce").between(0, 7).sum()))
m6.metric("Shown", len(f))

tabs = st.tabs(["⭐ Best matches", "📍 BiH & region", "📋 All results", "🆕 New", "⏰ Closing soon", "📌 My tracker",
                "🔍 Analyze vacancy", "📥 Import CSV", "🌐 Sources", "❓ Help"])

with tabs[0]:
    st.caption("Score 75+ · 🔵 Review required = strong match with a possible eligibility, licence or language issue.")
    show_cards(f[f["score"] >= 75], "best")

with tabs[1]:
    st.caption("Sarajevo, Bosnia and Herzegovina, Croatia, Serbia and neighbouring countries, from UN, OSCE and local job boards.")
    show_cards(f[f["region"].isin([REGION_BIH, REGION_WB])], "local")

with tabs[2]:
    if f.empty:
        st.info("No vacancies match the filters. Lower the minimum score or add keywords.")
    else:
        st.dataframe(f[["score", "category", "grade", "title", "organization", "location", "region",
                        "closing_date", "seen_on", "url"]], hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Link", display_text="Open"),
                                    "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d")})
        c1, c2, c3 = st.columns(3)
        c1.download_button("Download filtered CSV", csv_bytes(f), "damir-jobs.csv", "text/csv", width="stretch")
        c2.download_button("Download shortlist (Markdown)", shortlist_md(f), "damir-shortlist.md", "text/markdown", width="stretch")
        c3.download_button("Download all incl. disqualified", csv_bytes(jobs), "damir-jobs-all.csv", "text/csv", width="stretch")
        show_cards(f, "all")

with tabs[3]:
    fp = pd.to_datetime(f["posted_date"], errors="coerce")
    show_cards(f[fp >= pd.Timestamp(date.today() - timedelta(days=7))], "new")

with tabs[4]:
    d = pd.to_numeric(f["days_left"], errors="coerce")
    soon = f[d.between(0, 14)]
    show_cards(soon.sort_values("days_left", key=lambda c: pd.to_numeric(c, errors="coerce")), "soon")

with tabs[5]:
    t = tracker()
    st.caption("Statuses and notes live in this browser session. Download the tracker before closing; upload it next time.")
    if t.empty:
        st.info("Set a status (Saved, Applied...) inside any job card.")
    else:
        merged = t.merge(jobs[["job_id", "title", "organization", "closing_date", "score", "url"]], on="job_id", how="left")
        edited_t = st.data_editor(merged, hide_index=True, width="stretch", key="trk_editor",
                                  disabled=["job_id", "title", "organization", "closing_date", "score", "url"],
                                  column_config={"state": st.column_config.SelectboxColumn("State", options=TRACKER_STATES[1:]),
                                                 "note": st.column_config.TextColumn("Note", width="large"),
                                                 "url": st.column_config.LinkColumn("Link", display_text="Open")})
        if st.button("Save tracker changes"):
            keep = edited_t[edited_t["state"].fillna("").isin(TRACKER_STATES[1:])]
            ss.tracker = keep[["job_id", "state", "note"]].fillna("").reset_index(drop=True)
            st.success("Tracker saved.")
            st.rerun()
        st.download_button("Download tracker", csv_bytes(t), "damir-tracker.csv", "text/csv")
    up = st.file_uploader("Restore tracker CSV", type="csv", key="trk_up")
    if up is not None and st.button("Restore tracker"):
        try:
            r = pd.read_csv(up, dtype=str).fillna("")
            if {"job_id", "state"}.issubset(r.columns):
                r["job_id"] = r["job_id"].str.lstrip("'")
                if "note" not in r.columns:
                    r["note"] = ""
                ss.tracker = r.loc[r["state"].isin(TRACKER_STATES[1:]), ["job_id", "state", "note"]].reset_index(drop=True)
                st.success(f"Restored {len(ss.tracker)} entries.")
                st.rerun()
            else:
                st.error("The CSV needs job_id and state columns.")
        except Exception as exc:
            st.error(f"Cannot read the file: {exc}")

with tabs[6]:
    st.write("Paste any vacancy (LinkedIn, UN Careers, UNOPS, email alert...) to score it instantly.")
    with st.form("analyze"):
        a_title = st.text_input("Job title *")
        a_org = st.text_input("Organization *")
        a_loc = st.text_input("Location")
        a_close = st.date_input("Closing date", value=None)
        a_url = st.text_input("Vacancy URL")
        a_desc = st.text_area("Full vacancy text *", height=280)
        go = st.form_submit_button("Analyze", type="primary")
    if go:
        if not (a_title.strip() and a_org.strip() and a_desc.strip()):
            st.error("Title, organization and vacancy text are required.")
            ss.analysis = None
        else:
            ss.analysis = {"title": a_title, "organization": a_org, "location": a_loc,
                           "closing_date": a_close.isoformat() if a_close else "", "url": a_url,
                           "description": a_desc, "source": "Manual paste"}
    if ss.analysis:
        res = score_job(ss.analysis, profile=profile)
        if res["status"] == "Expired":
            st.warning("The closing date has passed.")
        job_card(pd.Series(res), "analysis", track=False)
        if st.button("Add this vacancy to my results"):
            ss.extra_jobs = ss.extra_jobs + [ss.analysis]
            ss.analysis = None
            st.success("Added. It now appears in the result tabs for this session.")
            st.rerun()

with tabs[7]:
    st.write("Upload a CSV with columns **title, organization, description** (required) and optionally "
             "location, country, contract_type, posted_date, closing_date, url.")
    tmpl = pd.DataFrame([{c: "" for c in IMPORT_COLUMNS}])
    tmpl.loc[0, ["title", "organization", "location", "closing_date", "url", "description"]] = [
        "Construction Engineer", "Example Organization", "Sarajevo", "2027-01-31",
        "https://example.org/vacancy", "Paste the full vacancy text here."]
    st.download_button("Download CSV template", csv_bytes(tmpl), "job-import-template.csv", "text/csv")
    up_jobs = st.file_uploader("Upload jobs CSV", type="csv", key="jobs_up")
    if up_jobs is not None and st.button("Import and score"):
        try:
            imp = pd.read_csv(up_jobs, dtype=str).fillna("")
            missing = [c for c in ("title", "organization", "description") if c not in imp.columns]
            if missing:
                st.error("Missing columns: " + ", ".join(missing))
            else:
                ok = imp[(imp["title"].str.strip() != "") & (imp["organization"].str.strip() != "")]
                rows = [{**{c: row.get(c, "") for c in IMPORT_COLUMNS}, "source": "CSV import"}
                        for _, row in ok.iterrows()]
                ss.extra_jobs = ss.extra_jobs + rows
                st.success(f"Imported {len(rows)} rows ({len(imp) - len(ok)} skipped for missing title/organization).")
                st.rerun()
        except Exception as exc:
            st.error(f"Cannot read the CSV: {exc}")
    if ss.extra_jobs and st.button("Remove imported / analysed vacancies"):
        ss.extra_jobs = []
        st.rerun()

with tabs[8]:
    st.subheader("Source health")
    st.caption("Browser-like connection: " + ("on (curl_cffi installed)" if CFFI_OK
                                               else "off (curl_cffi not installed; some sites may block the app)"))
    if not health.empty:
        st.dataframe(health[["source", "status", "jobs", "seconds", "last_checked", "platform", "message"]],
                     hide_index=True, width="stretch")
    st.markdown("**What the messages mean**\n"
                "- *OK (via fallback)*: the official site could not be read; results come from the backup page.\n"
                "- *OK (partial)*: the site asked the app to slow down; listings are complete, some full texts were skipped.\n"
                "- *rate limited*: the site asked for fewer requests. The app waits as long as the site asks, then retries.\n"
                "- *blocked by bot protection*: the site refuses automated requests from the hosting server. "
                "The backup page is used when one is set. Running the app on your own computer usually works.\n"
                "- *no vacancies read*: the page layout changed. The message shows what the page contained.")

    st.subheader("Test one source")
    names = [s["name"] for s in ss.sources]
    pick = st.selectbox("Source to test", names) if names else None
    if pick and st.button("Run test"):
        src_row = next(s for s in ss.sources if s["name"] == pick)
        with st.spinner(f"Testing {pick}..."):
            t_jobs, t_health = fetch_one(src_row, list(keywords), False, 20, result_store()["pace"])
        st.json(t_health)
        if t_jobs:
            st.dataframe(pd.DataFrame(t_jobs)[["title", "location", "closing_date", "url"]].head(20),
                         hide_index=True, width="stretch")

    st.subheader("Choose sources")
    edited = st.data_editor(pd.DataFrame(ss.sources, columns=SOURCE_FIELDS), hide_index=True,
                            width="stretch", num_rows="dynamic", key="src_editor",
                            column_config={"enabled": st.column_config.CheckboxColumn("On"),
                                           "url": st.column_config.TextColumn("Public careers URL", width="large"),
                                           "fallback": st.column_config.TextColumn("Backup URL (optional)", width="medium"),
                                           "country": st.column_config.TextColumn("Country (optional)", width="small")})
    st.caption("Supported: Workday (myworkdayjobs.com), Oracle (…oraclecloud.com/hcmUI/CandidateExperience/…/sites/…), "
               "Greenhouse, Lever, Ashby, PageUp, UNjobs (organization, duty-station or skills pages), RSS/Atom feeds, "
               "and most job-board listing pages (paste a category or search-result page). "
               "Country: used for vacancies on local boards that do not state a country.")
    if st.button("Save sources and search", type="primary"):
        new = []
        for _, row in edited.fillna("").iterrows():
            url, name, fb = str(row["url"]).strip(), str(row["name"]).strip(), str(row["fallback"]).strip()
            country = str(row.get("country", "")).strip()
            if not url:
                continue
            spec = detect_source(url, name)
            if spec is None or (fb and detect_source(fb) is None):
                st.error(f"Not a valid URL in row: {name or url}")
                st.stop()
            enabled = True if row["enabled"] in ("", None) else bool(row["enabled"])   # new rows default to on
            new.append({"name": name or spec["name"], "url": url, "fallback": fb, "country": country, "enabled": enabled})
        ss.sources = new
        st.rerun()
    if st.button("Reset to default sources"):
        ss.sources = [dict(s) for s in DEFAULT_SOURCES]
        st.rerun()
    st.download_button("Download my settings", json.dumps({"keywords": list(keywords), "sources": ss.sources}, indent=2),
                       "damir-radar-settings.json", "application/json")
    set_up = st.file_uploader("Load settings JSON", type="json", key="set_up")
    if set_up is not None and st.button("Apply settings"):
        try:
            cfg = json.load(set_up)
            srcs = [s for s in cfg.get("sources", []) if isinstance(s, dict) and detect_source(s.get("url", ""))]
            ss.sources = [{"name": s.get("name", ""), "url": s["url"], "fallback": s.get("fallback", ""),
                           "country": s.get("country", ""), "enabled": bool(s.get("enabled", True))} for s in srcs]
            if cfg.get("keywords"):
                ss.pending_keywords = "\n".join(str(k) for k in cfg["keywords"])
            st.rerun()
        except Exception as exc:
            st.error(f"Invalid settings file: {exc}")

with tabs[9]:
    st.markdown(f"""
**How it works**
1. On opening, the app searches every enabled source for your keywords. Results are reused for {CACHE_HOURS} hours; **Search now** forces a fresh search.
2. For the most relevant titles it reads the full vacancy text, then scores each job against your profile.
3. Duplicates and expired vacancies are removed. Disqualified roles are hidden from the main tabs (they remain in "Download all").
4. If a site fails, its backup page is used (UNHCR, UNICEF). Otherwise its last good results stay visible and it is retried later.

**Local and regional search**: UN, OSCE and local job boards in Bosnia and Herzegovina, Serbia and Croatia
(Klix Posao, MojPosao.ba, Infostud, MojPosao.hr), plus EuroBrussels and Arbeitnow for Europe. Bosnian, Croatian,
Serbian and German titles are understood (građevinski inženjer, nadzorni inženjer, voditelj projekta, Bauleiter...).
The same vacancy found on several sites is shown once; "Source" lists every site where it was found.

**Score (100)**: title 20 · competencies 25 · organization/sector 15 · experience 15 · education 10 · location/eligibility 5 · language 5 · contract 5.

- 🔴 **Disqualified**: unrelated role family or explicit nationality restriction.
- 🔵 **Review required**: strong match, but a possible blocker: national post outside Bosnia and Herzegovina, EU-citizens-only, internal/tier-restricted, licence, or a language you do not list (including vacancies written in German).
- Local jobs outside Bosnia and Herzegovina get a **Verify** note to check the work permit.
- **Verify** items are unknowns. They are never counted as matched. Confirm French/Italian under **My profile** if your level meets the requirement.

Not covered automatically: LinkedIn, Indeed, UN Careers (Inspira), UNOPS. Use **Analyze vacancy** for those.
""")
