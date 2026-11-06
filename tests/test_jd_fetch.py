import json

import pytest

from resume_agent.jd_fetch import FetchError, fetch_jd, parse_html_page, slugify

LONG = "<p>" + ("Build retrieval and ranking systems at scale. " * 15) + "</p>"


def fake(routes):
    calls = []

    def get(url, as_json=False):
        calls.append(url)
        if url not in routes:
            raise RuntimeError(f"404 {url}")
        body = routes[url]
        return body if as_json or isinstance(body, str) else json.dumps(body)

    get.calls = calls
    return get


def test_greenhouse_uses_board_api_and_unescapes():
    escaped = LONG.replace("<", "&lt;").replace(">", "&gt;")
    get = fake(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123": {
                "title": "ML Infra Engineer",
                "location": {"name": "Remote"},
                "content": "&lt;h2&gt;About&lt;/h2&gt;" + escaped,
                "company_name": "Acme",
            }
        }
    )
    jd = fetch_jd("https://job-boards.greenhouse.io/acme/jobs/123", get=get)
    assert (jd.source, jd.title, jd.company, jd.location) == ("greenhouse", "ML Infra Engineer", "Acme", "Remote")
    assert "## About" in jd.text and "<p>" not in jd.text


def test_lever_combines_lists():
    get = fake(
        {
            "https://api.lever.co/v0/postings/acme/abc-def": {
                "text": "Search Engineer",
                "categories": {"location": "Toronto", "team": "Search"},
                "descriptionPlain": "Intro " * 50,
                "lists": [{"text": "Requirements", "content": "<li>OpenSearch</li><li>Python</li>"}],
                "additionalPlain": "Benefits " * 20,
            }
        }
    )
    jd = fetch_jd("https://jobs.lever.co/acme/abc-def/apply", get=get)
    assert jd.source == "lever" and jd.location == "Toronto"
    assert "## Requirements" in jd.text and "- OpenSearch" in jd.text


def test_ashby_finds_job_by_id():
    get = fake(
        {
            "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true": {
                "jobs": [
                    {"id": "other", "title": "x", "descriptionHtml": LONG},
                    {"id": "u-1", "title": "AI Engineer", "location": "SF", "descriptionHtml": LONG},
                ]
            }
        }
    )
    jd = fetch_jd("https://jobs.ashbyhq.com/acme/u-1", get=get)
    assert jd.title == "AI Engineer" and jd.source == "ashby"


def test_workday_maps_to_cxs_api():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Toronto-ON/ML-Engineer_R123"
    api = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/Toronto-ON/ML-Engineer_R123"
    get = fake({api: {"jobPostingInfo": {"title": "ML Engineer", "jobDescription": LONG, "location": "Toronto"}}})
    jd = fetch_jd(url, get=get)
    assert jd.source == "workday" and jd.company == "acme"


def test_linkedin_guest_api():
    page = (
        '<h2 class="top-card-layout__title">Staff ML Engineer</h2>'
        '<a class="topcard__org-name-link x">Acme</a>'
        f'<div class="show-more-less-html__markup">{LONG}</div>'
    )
    get = fake({"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4012345678": page})
    jd = fetch_jd("https://www.linkedin.com/jobs/view/staff-ml-engineer-at-acme-4012345678/", get=get)
    assert (jd.title, jd.company, jd.source) == ("Staff ML Engineer", "Acme", "linkedin")


def test_jsonld_on_custom_career_page():
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Search Engineer",
          "hiringOrganization": {"name": "Acme"},
          "jobLocation": {"address": {"addressLocality": "Toronto", "addressCountry": "CA"}},
          "description": LONG}
    page = f'<html><script type="application/ld+json">{json.dumps({"@graph": [ld]})}</script><body>nav</body></html>'
    jd = parse_html_page("https://careers.acme.com/1", page)
    assert (jd.source, jd.company, jd.location) == ("json-ld", "Acme", "Toronto, CA")


def test_ats_failure_falls_back_to_html():
    page = f"<html><head><title>Role</title></head><body><nav>menu</nav><main>{LONG}</main></body></html>"
    get = fake({"https://boards.greenhouse.io/acme/jobs/9": page})  # API route missing -> fallback
    jd = fetch_jd("https://boards.greenhouse.io/acme/jobs/9", get=get)
    assert jd.source == "html" and "menu" not in jd.text


def test_js_only_page_raises_with_hint():
    get = fake({"https://example.com/job": "<html><body><div id='root'></div></body></html>"})
    with pytest.raises(FetchError, match="JavaScript"):
        fetch_jd("https://example.com/job", get=get)


def test_slugify():
    assert slugify("Acme, Inc.", "Senior ML Engineer (Search)") == "acme-inc-senior-ml-engineer-search"
