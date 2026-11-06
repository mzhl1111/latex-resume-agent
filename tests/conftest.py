"""Tests always run against the fictional profile in examples/, never personal data."""

import os
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
os.environ["RESUME_PROFILE_DIR"] = str(_EXAMPLES / "profile")
os.environ["RESUME_TEMPLATES_DIR"] = str(_EXAMPLES / "templates")

# Traces from tests go to a throwaway dir, never the repo's traces/.
import tempfile  # noqa: E402

os.environ["RESUME_TRACES_DIR"] = tempfile.mkdtemp(prefix="resume-traces-")
os.environ.setdefault("RESUME_TRACER", "local")
