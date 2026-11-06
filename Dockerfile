FROM python:3.12-slim

# TeX packages the template needs (titlesec, enumitem, fullpage, marvosym, babel-english)
# plus poppler for page counting / PNG rendering / text extraction.
RUN apt-get update && apt-get install -y --no-install-recommends \
      texlive-latex-base texlive-latex-recommended texlive-latex-extra \
      texlive-fonts-recommended texlive-lang-english poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY resume_agent ./resume_agent
RUN pip install --no-cache-dir ".[server]"

COPY AGENTS.md ./
COPY examples ./examples
COPY .claude/agents ./.claude/agents
# Personal data is NOT baked into the image: mount your profile/ and templates/
# at runtime (see docker-compose.yml) so the image is safe to push to a registry.

RUN useradd -m agent && mkdir -p /data/jobs /data/applications && chown -R agent /data
USER agent
ENV RESUME_AGENT_HOME=/app \
    RESUME_JOBS_DIR=/data/jobs \
    RESUME_APPLICATIONS_DIR=/data/applications \
    PYTHONUNBUFFERED=1
VOLUME ["/data/jobs", "/data/applications", "/app/profile", "/app/templates"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["python", "-m", "resume_agent", "serve", "--host", "0.0.0.0", "--port", "8080"]
