"""
Agent 后端统一入口。
"""
from __future__ import annotations

import os
from typing import Any

from src.claude_code import chat_sync as claude_chat_sync
from src.claude_code import get_default_model as get_claude_default_model

from .codex_cli import chat_sync as codex_chat_sync
from .codex_cli import get_default_model as get_codex_default_model


SUPPORTED_BACKENDS = ("claude", "codex")

BACKEND_ALIASES = {
    "claude": "claude",
    "claude-code": "claude",
    "claudecode": "claude",
    "codex": "codex",
    "codex-cli": "codex",
}

MODEL_ALIASES = {
    "claude": {
        "opus": "claude-opus-4-7",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    },
    "codex": {
        "codex": "gpt-5.3-codex",
        "spark": "gpt-5.3-codex-spark",
        "gpt52": "gpt-5.2-codex",
        "gpt51": "gpt-5.1-codex",
    },
}


def normalize_backend(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        raw = os.getenv("DEFAULT_AGENT_BACKEND", "claude").strip().lower()
    return BACKEND_ALIASES.get(raw, raw if raw in SUPPORTED_BACKENDS else "claude")


def resolve_model_alias(backend: str, value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    return MODEL_ALIASES.get(backend, {}).get(raw.lower(), raw)


def get_default_model(backend: str) -> str:
    if backend == "codex":
        return get_codex_default_model()
    return get_claude_default_model()


def chat_sync(
    backend: str,
    message: str,
    session_id: str = None,
    can_use_tool=None,
    cwd: str = None,
    model: str = None,
    sandbox: str = None,
    **kwargs: Any,
) -> tuple[str, str]:
    backend = normalize_backend(backend)
    if backend == "codex":
        return codex_chat_sync(
            message,
            session_id=session_id,
            cwd=cwd,
            model=model,
            sandbox=sandbox,
            **kwargs,
        )

    return claude_chat_sync(
        message,
        session_id=session_id,
        can_use_tool=can_use_tool,
        model=model,
        **kwargs,
    )
