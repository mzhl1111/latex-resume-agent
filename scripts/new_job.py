#!/usr/bin/env python3
from pathlib import Path
import argparse
import shutil

ROOT = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("slug", help="e.g. stripe-ml-infra")
args = parser.parse_args()

job = ROOT / "jobs" / args.slug
job.mkdir(parents=True, exist_ok=False)
(job / "jd.md").write_text("# Job Description\n\nPaste the JD text or URL notes here.\n")
(job / "analysis.md").write_text("# JD → Evidence Mapping\n\n")
shutil.copy(ROOT / "templates" / "resume_base.tex", job / "resume.tex")
print(job)
