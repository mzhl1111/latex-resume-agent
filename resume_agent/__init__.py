"""Shared core for the LaTeX resume agent.

Everything here is deterministic (no LLM calls) except ``llm_pipeline``,
so both front doors -- Claude Code (``.claude/``) and the MCP/A2A server
(``resume_agent.server``) -- use the same fetch / compile / proofread code.
"""

__version__ = "0.2.0"
