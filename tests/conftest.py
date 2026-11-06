"""Tests always run against the fictional profile in examples/, never personal data."""

import os
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
os.environ["RESUME_PROFILE_DIR"] = str(_EXAMPLES / "profile")
os.environ["RESUME_TEMPLATES_DIR"] = str(_EXAMPLES / "templates")
