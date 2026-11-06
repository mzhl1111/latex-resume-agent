"""Fetch a job description from a URL.

Strategy, in order:
1. Known ATS public APIs (Greenhouse, Lever, Ashby, Workday, LinkedIn guest API)
   -- these return clean data and don't need JavaScript rendering.
2. schema.org ``JobPosting`` JSON-LD embedded in the page (most career sites).
3. Readable-text extraction from the HTML ``<main>``/``<article>``/``<body>``.

If all of that yields too little text the page probably renders client-side;
``FetchError`` is raised with a hint so the caller (Claude Code, or another
agent) can fall back to a browser / WebFetch or ask for pasted text.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests

from .html_text import html_to_text

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 latex-resume-agent"
)
MIN_TEXT_CHARS = 400
TIMEOUT = 20


class FetchError(RuntimeError):
    """Raised when no usable JD text could be extracted."""


@dataclass
class JobDescription:
    url: str
    source: str
    title: str = ""
    company: str = ""
    location: str = ""
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        head = [f"# {self.title or 'Job Description'}"]
        meta = [
            ("Company", self.company),
            ("Location", self.location),
            ("Source", f"{self.url} ({self.source})"),
        ]
        head += [f"- **{k}:** {v}" for k, v in meta if v]
        return "\n".join(head) + "\n\n" + self.text.strip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- http


def _get(url: str, *, as_json: bool = False, session: requests.Session | None = None):
    s = session or requests
    resp = s.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json() if as_json else resp.text


# --------------------------------------------------------------------------- ATS handlers


def _greenhouse(url: str, get: Callable) -> JobDescription | None:
    p = urlparse(url)
    m = re.match(r"^/(?:embed/job_app\?for=)?([^/]+)/jobs/(\d+)", p.path)
    board, job_id = (m.group(1), m.group(2)) if m else (None, None)
    if not m and "greenhouse.io" in p.netloc:
        q = parse_qs(p.query)
        board, job_id = (q.get("for") or [None])[0], (q.get("token") or [None])[0]
    if not (board and job_id and "greenhouse.io" in p.netloc):
        return None
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}", as_json=True)
    return JobDescription(
        url=url,
        source="greenhouse",
        title=data.get("title", ""),
        company=data.get("company_name") or board,
        location=(data.get("location") or {}).get("name", ""),
        text=html_to_text(data.get("content", "")),
    )


def _lever(url: str, get: Callable) -> JobDescription | None:
    p = urlparse(url)
    if not p.netloc.endswith("lever.co"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None
    company, posting = parts[0], parts[1]
    api_host = "api.eu.lever.co" if ".eu." in p.netloc else "api.lever.co"
    d = get(f"https://{api_host}/v0/postings/{company}/{posting}", as_json=True)
    chunks = [d.get("descriptionPlain") or html_to_text(d.get("description", ""))]
    for lst in d.get("lists", []):
        chunks.append(f"## {lst.get('text', '').strip()}\n{html_to_text(lst.get('content', ''))}")
    chunks.append(d.get("additionalPlain") or html_to_text(d.get("additional", "")))
    cats = d.get("categories") or {}
    return JobDescription(
        url=url,
        source="lever",
        title=d.get("text", ""),
        company=company,
        location=cats.get("location", ""),
        text="\n\n".join(c.strip() for c in chunks if c and c.strip()),
        extra={k: v for k, v in cats.items() if v},
    )


def _ashby(url: str, get: Callable) -> JobDescription | None:
    p = urlparse(url)
    if p.netloc != "jobs.ashbyhq.com":
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None
    org, job_id = parts[0], parts[1]
    board = get(
        f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true",
        as_json=True,
    )
    job = next((j for j in board.get("jobs", []) if j.get("id") == job_id), None)
    if not job:
        raise FetchError(f"Ashby board '{org}' has no published job {job_id} (closed?)")
    comp = (job.get("compensation") or {}).get("compensationTierSummary")
    return JobDescription(
        url=url,
        source="ashby",
        title=job.get("title", ""),
        company=org,
        location=job.get("location", ""),
        text=html_to_text(job.get("descriptionHtml", "")) or job.get("descriptionPlain", ""),
        extra={"compensation": comp} if comp else {},
    )


def _workday(url: str, get: Callable) -> JobDescription | None:
    p = urlparse(url)
    m = re.match(r"^([^.]+)\.wd\d+\.myworkdayjobs\.com$", p.netloc)
    if not m:
        return None
    tenant = m.group(1)
    parts = [x for x in p.path.split("/") if x]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]  # drop locale
    if "job" not in parts:
        return None
    site = parts[0]
    rest = "/".join(parts[parts.index("job") :])
    d = get(f"https://{p.netloc}/wday/cxs/{tenant}/{site}/{rest}", as_json=True)
    info = d.get("jobPostingInfo") or {}
    return JobDescription(
        url=url,
        source="workday",
        title=info.get("title", ""),
        company=(d.get("hiringOrganization") or {}).get("name") or tenant,
        location=info.get("location", ""),
        text=html_to_text(info.get("jobDescription", "")),
    )


def _linkedin(url: str, get: Callable) -> JobDescription | None:
    p = urlparse(url)
    if "linkedin.com" not in p.netloc:
        return None
    m = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})", p.path)
    job_id = m.group(1) if m else (parse_qs(p.query).get("currentJobId") or [None])[0]
    if not job_id:
        return None
    page = get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")
    body = re.search(
        r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', page, re.S
    )
    title = re.search(r'class="[^"]*top-card-layout__title[^"]*"[^>]*>(.*?)</h2>', page, re.S)
    org = re.search(r'class="[^"]*topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', page, re.S)
    loc = re.search(r'class="[^"]*topcard__flavor--bullet[^"]*"[^>]*>(.*?)</span>', page, re.S)
    strip = lambda s: html_to_text(s.group(1)).strip() if s else ""  # noqa: E731
    return JobDescription(
        url=url,
        source="linkedin",
        title=strip(title),
        company=strip(org),
        location=strip(loc),
        text=html_to_text(body.group(1)) if body else "",
    )


ATS_HANDLERS: list[Callable[[str, Callable], JobDescription | None]] = [
    _greenhouse,
    _lever,
    _ashby,
    _workday,
    _linkedin,
]


# --------------------------------------------------------------------------- generic HTML


def _iter_jsonld(obj: Any):
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_jsonld(item)
    elif isinstance(obj, dict):
        yield obj
        if "@graph" in obj:
            yield from _iter_jsonld(obj["@graph"])


def _jsonld_location(loc: Any) -> str:
    locs = loc if isinstance(loc, list) else [loc]
    out = []
    for item in locs:
        if not isinstance(item, dict):
            continue
        addr = item.get("address") or {}
        if isinstance(addr, str):
            out.append(addr)
            continue
        bits = [addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
        bits = [b if isinstance(b, str) else (b or {}).get("name", "") for b in bits]
        out.append(", ".join(b for b in bits if b))
    return " / ".join(o for o in out if o)


def parse_html_page(url: str, page: str) -> JobDescription:
    """Extract a JD from a raw HTML page (JSON-LD first, then readable text)."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I
    ):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _iter_jsonld(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" not in types:
                continue
            org = node.get("hiringOrganization") or {}
            return JobDescription(
                url=url,
                source="json-ld",
                title=html_to_text(node.get("title", "")),
                company=org.get("name", "") if isinstance(org, dict) else str(org),
                location=_jsonld_location(node.get("jobLocation")),
                text=html_to_text(node.get("description", "")),
            )

    title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    region = None
    for tag in ("main", "article"):
        m = re.search(rf"<{tag}\b[^>]*>(.*)</{tag}>", page, re.S | re.I)
        if m:
            region = m.group(1)
            break
    if region is None:
        m = re.search(r"<body\b[^>]*>(.*)</body>", page, re.S | re.I)
        region = m.group(1) if m else page
    return JobDescription(
        url=url,
        source="html",
        title=html_to_text(title_m.group(1)) if title_m else "",
        text=html_to_text(region),
    )


# --------------------------------------------------------------------------- entry point


def fetch_jd(url: str, *, get: Callable | None = None) -> JobDescription:
    """Fetch and normalise a JD. ``get`` is injectable for tests."""
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise FetchError(f"Not an http(s) URL: {url!r}")
    get = get or _get
    errors: list[str] = []

    for handler in ATS_HANDLERS:
        try:
            jd = handler(url, get)
        except FetchError:
            raise
        except Exception as exc:  # network / schema drift: fall through to generic
            errors.append(f"{handler.__name__.strip('_')}: {exc}")
            jd = None
        if jd and len(jd.text) >= MIN_TEXT_CHARS:
            return jd
        if jd:
            errors.append(f"{jd.source}: only {len(jd.text)} chars")
            break

    try:
        page = get(url)
    except Exception as exc:
        errors.append(f"html: {exc}")
        raise FetchError(_hint(url, errors)) from exc
    jd = parse_html_page(url, page)
    if len(jd.text) < MIN_TEXT_CHARS:
        errors.append(f"{jd.source}: only {len(jd.text)} chars of text")
        raise FetchError(_hint(url, errors))
    return jd


def _hint(url: str, errors: list[str]) -> str:
    return (
        f"Could not extract a job description from {url}. "
        "The page probably renders with JavaScript or blocks bots. "
        "Fall back to a browser/WebFetch, or pass the JD text directly. "
        f"Details: {'; '.join(errors)}"
    )


def slugify(*parts: str, max_len: int = 60) -> str:
    raw = "-".join(p for p in parts if p)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "job"
