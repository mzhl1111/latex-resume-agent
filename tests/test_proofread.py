import shutil

import pytest

from resume_agent.compile import check_dependencies, compile_resume
from resume_agent.proofread import number_tokens, proofread
from resume_agent.workspace import new_job

needs_tex = pytest.mark.skipif(bool(check_dependencies()), reason="pdflatex/poppler not installed")


@pytest.fixture
def job(tmp_path):
    return new_job("acme-ml", "# JD\n", jobs_dir=tmp_path)


def test_new_job_never_clobbers(tmp_path):
    a = new_job("x", jobs_dir=tmp_path)
    b = new_job("x", jobs_dir=tmp_path)
    assert a.dir.name == "x" and b.dir.name == "x-2"
    with pytest.raises(ValueError):
        new_job("../evil", jobs_dir=tmp_path)


def test_number_tokens_normalise():
    toks = number_tokens(r"serving 2,000+ customers, \textasciitilde20\% and 1--2M docs, ~1s SLA")
    assert {"2000", "20%", "1", "2m", "1s"} <= toks


@needs_tex
def test_base_template_passes(job):
    rep = proofread(job.tex)
    assert rep.ok, rep.to_markdown()
    assert rep.stats["pages"] == 1 and not rep.issues


@needs_tex
def test_catches_invented_metric_and_guardrail(job):
    src = job.tex.read_text().replace(r"build time by \textasciitilde60\%", r"build time by \textasciitilde65\%")
    src = src.replace(r"\resumeItem{Migrated CI", r"\resumeItem{Solely trained the ranking model.}\resumeItem{Migrated CI")
    job.tex.write_text(src)
    checks = {i.check for i in proofread(job.tex).issues if i.severity == "error"}
    assert {"unverified-number", "guardrail:sole-ownership", "guardrail:ranking-training-claimed"} <= checks


@needs_tex
def test_two_pages_fails(job):
    src = job.tex.read_text().replace(r"\section{Skills}", r"\section{Skills}" + "\n" + r"\small{filler}\\" * 60)
    job.tex.write_text(src)
    res = compile_resume(job.tex)
    assert res.pages > 1 and not res.ok
    assert any(i.check == "page-count" for i in proofread(job.tex).issues)


@needs_tex
def test_latex_error_is_reported(job):
    job.tex.write_text(job.tex.read_text().replace(r"\end{document}", r"\undefinedmacro \end{document}"))
    res = compile_resume(job.tex)
    assert not res.ok and any("Undefined control sequence" in e for e in res.errors)
