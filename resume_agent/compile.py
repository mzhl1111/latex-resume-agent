"""Compile a resume .tex to PDF, verify page count, render a PNG preview."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

AUX_SUFFIXES = (".aux", ".out", ".toc", ".synctex.gz")


@dataclass
class CompileResult:
    ok: bool
    tex: str
    pdf: str | None = None
    png: str | None = None
    pages: int | None = None
    errors: list[str] = field(default_factory=list)
    overfull_boxes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log_tail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        if self.ok:
            s = f"OK: {Path(self.pdf).name} is 1 page; preview at {self.png}"
        elif self.errors:
            s = "FAIL: LaTeX error(s):\n  " + "\n  ".join(self.errors)
        else:
            s = f"FAIL: expected 1 page, got {self.pages}"
        if self.overfull_boxes:
            s += "\nOverfull boxes (text running into the margin):\n  " + "\n  ".join(
                self.overfull_boxes
            )
        return s


def check_dependencies() -> list[str]:
    return [b for b in ("pdflatex", "pdfinfo", "pdftoppm", "pdftotext") if not shutil.which(b)]


def _parse_log(log: str) -> tuple[list[str], list[str], list[str]]:
    errors, overfull, warnings = [], [], []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("! "):
            ctx = " ".join(lines[i + 1 : i + 3]).strip()
            errors.append(f"{line[2:].strip()} {ctx}".strip())
        elif line.startswith("Overfull \\hbox"):
            m = re.search(r"\(([\d.]+)pt too wide\).*?lines? ([\d-]+)", line)
            if m and float(m.group(1)) > 1.0:  # ignore sub-point noise
                snippet = lines[i + 1].strip() if i + 1 < len(lines) else ""
                overfull.append(f"{m.group(1)}pt too wide at tex line {m.group(2)}: {snippet[:80]}")
        elif "LaTeX Warning:" in line and "Label(s) may have changed" not in line:
            warnings.append(line.strip())
    return errors, overfull, warnings


def pdf_pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else 0


def compile_resume(tex_path: str | Path, *, dpi: int = 160, clean: bool = True) -> CompileResult:
    tex = Path(tex_path).resolve()
    if not tex.exists():
        return CompileResult(ok=False, tex=str(tex), errors=[f"{tex} does not exist"])
    missing = check_dependencies()
    if missing:
        return CompileResult(ok=False, tex=str(tex), errors=[f"missing binaries: {missing}"])

    workdir, base = tex.parent, tex.stem
    pdf, png = workdir / f"{base}.pdf", workdir / f"{base}.png"
    pdf.unlink(missing_ok=True)
    proc = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex.name],
        cwd=workdir,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    log_file = workdir / f"{base}.log"
    log = log_file.read_text(errors="replace") if log_file.exists() else proc.stdout
    errors, overfull, warnings = _parse_log(log)
    if clean:
        for suf in AUX_SUFFIXES:
            (workdir / f"{base}{suf}").unlink(missing_ok=True)

    result = CompileResult(
        ok=False,
        tex=str(tex),
        errors=errors,
        overfull_boxes=overfull,
        warnings=warnings,
        log_tail="\n".join(log.splitlines()[-25:]),
    )
    if proc.returncode != 0 or not pdf.exists():
        if not result.errors:
            result.errors = [f"pdflatex exited {proc.returncode}"]
        return result

    result.pdf = str(pdf)
    result.pages = pdf_pages(pdf)
    subprocess.run(
        ["pdftoppm", "-png", "-f", "1", "-singlefile", "-r", str(dpi), str(pdf), str(workdir / base)],
        capture_output=True,
        check=True,
    )
    result.png = str(png)
    result.ok = result.pages == 1
    return result
