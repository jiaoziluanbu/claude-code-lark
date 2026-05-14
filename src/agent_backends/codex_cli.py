"""
Codex CLI 后端。

通过 `codex exec` 做非交互式远程开发，并用 Codex session id 恢复上下文。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_MODEL = os.getenv("CODEX_DEFAULT_MODEL", "gpt-5.3-codex")
DEFAULT_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "1800"))
DEFAULT_SANDBOX = os.getenv("CODEX_DEFAULT_SANDBOX", "danger-full-access")


def get_default_model() -> str:
    return DEFAULT_MODEL


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _find_session_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate

        session = value.get("session")
        if isinstance(session, dict):
            candidate = session.get("id")
            if isinstance(candidate, str) and candidate:
                return candidate

        for nested in value.values():
            candidate = _find_session_id(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _find_session_id(item)
            if candidate:
                return candidate
    return None


def _extract_assistant_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None

    for key in ("message", "item", "event", "data"):
        nested = value.get(key)
        text = _extract_assistant_text(nested)
        if text:
            return text

    role = value.get("role")
    if role == "assistant":
        content = value.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            if parts:
                return "\n".join(parts)

    text = value.get("text")
    if isinstance(text, str) and value.get("type") in {"assistant_message", "message"}:
        return text

    return None


def _parse_jsonl(stdout: str) -> tuple[str | None, str | None]:
    session_id = None
    assistant_text = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        session_id = _find_session_id(event) or session_id
        assistant_text = _extract_assistant_text(event) or assistant_text

    return assistant_text, session_id


def _build_command(
    message: str,
    session_id: str | None,
    cwd: str | None,
    model: str | None,
    sandbox: str | None,
    output_file: str,
) -> list[str]:
    if session_id:
        cmd = ["codex", "exec", "resume", "--json", "-o", output_file]
        if model:
            cmd.extend(["-m", model])
        if (sandbox or DEFAULT_SANDBOX) == "danger-full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.extend([session_id, message])
        return cmd

    cmd = ["codex", "exec", "--json", "-o", output_file, "--skip-git-repo-check"]
    if cwd:
        cmd.extend(["-C", cwd])
    if model:
        cmd.extend(["-m", model])

    effective_sandbox = sandbox or DEFAULT_SANDBOX
    if effective_sandbox == "danger-full-access":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["-s", effective_sandbox, "-a", "never"])

    cmd.append(message)
    return cmd


def chat_sync(
    message: str,
    session_id: str = None,
    cwd: str = None,
    model: str = None,
    sandbox: str = None,
    timeout: int = None,
    **_: Any,
) -> tuple[str, str]:
    """同步调用 Codex CLI。"""
    model = model or DEFAULT_MODEL
    timeout = timeout or DEFAULT_TIMEOUT

    with tempfile.TemporaryDirectory(prefix="remote-codex-") as tmpdir:
        output_file = str(Path(tmpdir) / "last-message.txt")
        cmd = _build_command(
            message=message,
            session_id=session_id,
            cwd=cwd,
            model=model,
            sandbox=sandbox,
            output_file=output_file,
        )
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd or None,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return "未找到 Codex CLI。请先在本机安装并确认 `codex --help` 可用。", session_id or ""
        except subprocess.TimeoutExpired:
            return f"Codex 执行超时（{timeout} 秒）。可以稍后重试，或调大 CODEX_TIMEOUT。", session_id or ""

        output_text = ""
        output_path = Path(output_file)
        if output_path.exists():
            output_text = output_path.read_text(encoding="utf-8", errors="replace").strip()

        json_text, parsed_session_id = _parse_jsonl(result.stdout)
        new_session_id = parsed_session_id or session_id or ""

        if result.returncode != 0:
            details = "\n".join(part for part in (_tail(result.stderr), _tail(result.stdout)) if part)
            if details:
                return f"Codex 执行失败：\n{details}", new_session_id
            return f"Codex 执行失败，退出码 {result.returncode}。", new_session_id

        reply = output_text or json_text or _tail(result.stdout)
        return reply.strip() or "Codex 已完成，但没有返回文本。", new_session_id
