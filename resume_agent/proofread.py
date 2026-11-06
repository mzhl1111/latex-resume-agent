"""Deterministic proofreading of a compiled resume PDF.

This catches what can be checked mechanically, so the LLM/visual reviewer
(Claude Code subagent, or the server's review pass) can focus on judgment:

* layout   -- page count, orphan lines, bottom whitespace, line compression,
              layout/preamble drift from the canonical template, font shrinking
* facts    -- every number in the PDF must appear in the fact bank or template;
              guardrail phrases from profile/guardrails.json
* hygiene  -- LaTeX source leaking into the PDF text, overfull boxes, typos
              from repeated words, double spaces
"""

from __future__ import annotations

import html
import json
import re
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .compile import compile_resume

ORPHAN_MAX_WORDS = 2
ORPHAN_MAX_WIDTH_PT = 70.0
MIN_FILL_RATIO = 0.86  # last text baseline / page height
MIN_LINE_PITCH_PT = 10.8  # 11pt \small bullets sit ~12pt apart in the template
SIZE_COMMANDS = re.compile(r"\\(footnotesize|scriptsize|tiny|fontsize)\b")


@dataclass
class Issue:
    severity: str  # "error" | "warn" | "info"
    check: str
    message: str
    detail: str = ""


@dataclass
class ProofreadReport:
    ok: bool
    pdf: str
    png: str | None
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    text: str = ""

    def to_dict(self, include_text: bool = False) -> dict:
        d = asdict(self)
        if not include_text:
            d.pop("text")
        return d

    def to_markdown(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        out = [f"# Proofread: {head}", ""]
        out.append("Stats: " + ", ".join(f"{k}={v}" for k, v in self.stats.items()))
        out.append("")
        if not self.issues:
            out.append("No mechanical issues found.")
        for sev in ("error", "warn", "info"):
            items = [i for i in self.issues if i.severity == sev]
            if items:
                out.append(f"## {sev.upper()} ({len(items)})")
                for i in items:
                    out.append(f"- **{i.check}**: {i.message}" + (f" — `{i.detail}`" if i.detail else ""))
                out.append("")
        return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- PDF geometry


def _words(pdf: Path) -> tuple[list[tuple[float, float, float, float, str]], float, float]:
    xml = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-bbox", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    page = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', xml)
    pw, ph = (float(page.group(1)), float(page.group(2))) if page else (612.0, 792.0)
    words = [
        (float(a), float(b), float(c), float(d), html.unescape(t))
        for a, b, c, d, t in re.findall(
            r'xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)<', xml
        )
    ]
    return words, pw, ph


def _lines(words):
    rows: dict[int, list] = {}
    for w in words:
        rows.setdefault(round(w[3]), []).append(w)
    out = []
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda w: w[0])
        out.append(
            {
                "y": y,
                "x0": ws[0][0],
                "x1": ws[-1][2],
                "h": max(w[3] - w[1] for w in ws),
                "words": [w[4] for w in ws],
                "text": " ".join(w[4] for w in ws),
            }
        )
    return out


def _layout_checks(pdf: Path, issues: list[Issue], stats: dict) -> list[dict]:
    words, pw, ph = _words(pdf)
    lines = [ln for ln in _lines(words) if ln["text"].strip() not in {"•", "◦", "–"}]
    if not lines:
        issues.append(Issue("error", "empty", "PDF has no extractable text"))
        return lines

    body = [ln for ln in lines if 8.0 <= ln["h"] <= 9.6]
    bullet_x = statistics.mode(round(ln["x0"]) for ln in body) if body else None
    fill = max(ln["y"] for ln in lines) / ph
    stats.update(lines=len(lines), fill_ratio=round(fill, 3))

    if fill < MIN_FILL_RATIO:
        issues.append(
            Issue("warn", "whitespace", f"Page is only {fill:.0%} filled; consider restoring a strong bullet")
        )

    # Orphans: last line of a bullet with 1–2 words / very short.
    for prev, cur, nxt in zip(lines, lines[1:], lines[2:] + [None]):
        is_continuation = (
            bullet_x is not None
            and abs(cur["x0"] - bullet_x) < 2
            and abs(prev["x0"] - bullet_x) < 2
            and cur["y"] - prev["y"] < 16
        )
        ends_para = nxt is None or abs(nxt["x0"] - bullet_x) >= 2 or nxt["y"] - cur["y"] > 16
        if is_continuation and ends_para:
            width = cur["x1"] - cur["x0"]
            if len(cur["words"]) <= ORPHAN_MAX_WORDS or width < ORPHAN_MAX_WIDTH_PT:
                issues.append(
                    Issue(
                        "warn",
                        "orphan-line",
                        "Bullet ends with a short orphan line; trim or extend the bullet by a few words",
                        cur["text"],
                    )
                )

    # Vertical compression between consecutive bullet lines.
    pitches = [
        b["y"] - a["y"]
        for a, b in zip(body, body[1:])
        if bullet_x is not None and abs(a["x0"] - bullet_x) < 2 and abs(b["x0"] - bullet_x) < 2
        and 0 < b["y"] - a["y"] < 16
    ]
    if pitches:
        pitch = statistics.median(pitches)
        stats["line_pitch_pt"] = round(pitch, 1)
        if pitch < MIN_LINE_PITCH_PT:
            issues.append(Issue("warn", "compressed", f"Line spacing {pitch:.1f}pt looks squeezed"))
    if body:
        stats["body_text_height_pt"] = round(statistics.median(ln["h"] for ln in body), 2)
    return lines


# --------------------------------------------------------------------------- source checks


def _preamble(tex: str) -> str:
    pre = tex.split("\\begin{document}", 1)[0]
    pre = re.sub(r"%.*", "", pre)
    return re.sub(r"\s+", " ", pre).strip()


def _source_checks(tex_path: Path, issues: list[Issue]) -> None:
    src = tex_path.read_text()
    m = re.search(r"\\documentclass\[([^\]]*)\]", src)
    if not m or "11pt" not in m.group(1):
        issues.append(Issue("error", "font-size", "Document class is no longer 11pt"))
    if SIZE_COMMANDS.search(src.split("\\begin{document}", 1)[-1]):
        issues.append(Issue("warn", "font-size", "Body uses a smaller size command than the template"))
    if paths.BASE_TEMPLATE.exists() and _preamble(src) != _preamble(paths.BASE_TEMPLATE.read_text()):
        issues.append(
            Issue(
                "warn",
                "layout-drift",
                "Preamble (margins/spacing/macros) differs from templates/resume_base.tex; "
                "only acceptable if content edits could not make it fit",
            )
        )
    body = src.split("\\begin{document}", 1)[-1]
    if re.search(r"\\vspace\{-\s*(1[0-9]|[2-9]\d)pt\}", body):
        issues.append(Issue("warn", "compressed", "Large negative \\vspace in body"))


# --------------------------------------------------------------------------- fact checks

_NUM = re.compile(r"(?<![\w@])(\d[\d,]*(?:\.\d+)?)\s?([kmb%x]|s\b)?", re.I)


def _norm_text(s: str) -> str:
    return (
        s.replace("˜", "~").replace("\\textasciitilde", "~").replace("–", "-").replace("—", "-")
        .replace("\\%", "%").replace("--", "-")
    )


def number_tokens(text: str) -> set[str]:
    toks = set()
    for num, unit in _NUM.findall(_norm_text(text)):
        num = num.replace(",", "").rstrip(".")
        toks.add(num + (unit or "").lower())
    return toks


def _allowed_numbers() -> set[str]:
    allowed: set[str] = set()
    for p in (paths.WORK_HISTORY, paths.BASE_TEMPLATE):
        if p.exists():
            allowed |= number_tokens(p.read_text())
    # bare numbers are fine if the same number appears with any unit
    allowed |= {re.sub(r"[a-z%]+$", "", t) for t in allowed}
    return allowed


def _load_rules() -> list[dict]:
    if not paths.GUARDRAILS.exists():
        return []
    return json.loads(paths.GUARDRAILS.read_text()).get("rules", [])


def _fact_checks(text: str, issues: list[Issue]) -> None:
    allowed = _allowed_numbers()
    unknown = sorted(t for t in number_tokens(text) if t not in allowed)
    for tok in unknown:
        ctx = re.search(rf".{{0,40}}{re.escape(re.sub(r'[a-z%]+$', '', tok))}.{{0,25}}", _norm_text(text))
        issues.append(
            Issue(
                "error",
                "unverified-number",
                f"'{tok}' does not appear in profile/work_history.md — invented or altered metric?",
                ctx.group(0).strip() if ctx else "",
            )
        )
    flat = re.sub(r"\s+", " ", text)
    for rule in _load_rules():
        for m in re.finditer(rule["pattern"], flat, re.I):
            ctx = flat[max(0, m.start() - 40) : m.end() + 40]
            issues.append(Issue(rule["severity"], f"guardrail:{rule['id']}", rule["message"], ctx))
            break


def _hygiene_checks(text: str, issues: list[Issue]) -> None:
    leaks = re.findall(r"\\[a-zA-Z]+|[{}$]|\\\\", text)
    if leaks:
        issues.append(Issue("error", "latex-leak", "LaTeX markup visible in PDF text", " ".join(sorted(set(leaks)))[:80]))
    for m in re.finditer(r"\b(\w{2,})\s+\1\b", text, re.I):
        issues.append(Issue("warn", "repeated-word", "Repeated word", m.group(0)))
    if "??" in text:
        issues.append(Issue("error", "broken-ref", "Unresolved reference '??' in PDF"))


# --------------------------------------------------------------------------- entry point


def proofread(tex_path: str | Path, *, recompile: bool = True) -> ProofreadReport:
    tex = Path(tex_path).resolve()
    issues: list[Issue] = []
    stats: dict = {}
    pdf, png = tex.with_suffix(".pdf"), tex.with_suffix(".png")

    if recompile or not pdf.exists():
        res = compile_resume(tex)
        for e in res.errors:
            issues.append(Issue("error", "latex", e))
        for o in res.overfull_boxes:
            issues.append(Issue("warn", "overfull-box", "Text runs into the margin", o))
        if res.pages and res.pages != 1:
            issues.append(Issue("error", "page-count", f"Expected exactly 1 page, got {res.pages}"))
        if not res.pdf:
            return ProofreadReport(False, str(pdf), None, issues, stats)

    text = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True
    ).stdout
    stats["pages"] = text.count("\f") or 1
    _layout_checks(pdf, issues, stats)
    _source_checks(tex, issues)
    _fact_checks(text, issues)
    _hygiene_checks(text, issues)
    ok = not any(i.severity == "error" for i in issues)
    return ProofreadReport(ok, str(pdf), str(png) if png.exists() else None, issues, stats, text)
