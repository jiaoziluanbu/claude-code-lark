"""
canUseTool 交互式授权

流程：
1. Claude Code 请求使用某工具 → canUseTool 回调触发
2. 只读工具（AUTO_APPROVE_TOOLS）直接放行
3. session 已信任的工具直接放行
4. 否则发飞书消息请求授权，等待用户回复 y/trust/n
5. 用户回复后通过 threading.Event 通知回调

注意：canUseTool 在 Claude SDK 的线程中运行（chat_sync 创建的独立线程），
用户回复从主线程的 WebSocket handler 过来，因此用 threading.Event 做跨线程通信。
"""
import os
import uuid
import logging
import threading
from typing import Any

from .session_permissions import SessionPermissions

logger = logging.getLogger(__name__)


def _load_list_env(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key, "")
    if not raw.strip():
        return default
    return [t.strip() for t in raw.split(",") if t.strip()]


AUTO_APPROVE_TOOLS = _load_list_env("AUTO_APPROVE_TOOLS", ["Read", "Glob", "Grep"])
APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT", "120"))


def _summarize_input(tool_name: str, input_data: dict) -> str:
    """从工具输入中提取关键信息用于展示"""
    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        return f"命令：{cmd}"
    elif tool_name in ("Write", "Edit"):
        path = input_data.get("file_path", "")
        return f"文件：{path}"
    elif tool_name == "Read":
        return f"文件：{input_data.get('file_path', '')}"
    else:
        keys = list(input_data.keys())[:3]
        return f"参数：{', '.join(keys)}" if keys else "(无参数)"


class PendingApproval:
    """一个待处理的授权请求"""
    __slots__ = ("request_id", "event", "result", "message_id")

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.event = threading.Event()
        self.result: str | None = None  # "y" | "trust" | "n"
        self.message_id: str | None = None  # 授权消息的 message_id，用于匹配回复


class PermissionManager:
    """
    管理工具授权的全局单例。

    使用方式：
        manager = PermissionManager(send_fn)
        # 在 ConversationClient 中：
        callback = manager.make_callback(chat_id)
        # 在 WebSocket handler 中：
        manager.handle_approval_reply(parent_message_id, reply_text)
    """

    def __init__(self, send_message_fn):
        """
        Args:
            send_message_fn: 发送飞书消息的函数，签名 (chat_id, text) -> dict
                返回值需包含飞书 API 返回的 message_id
        """
        self._send_fn = send_message_fn
        self._session_perms = SessionPermissions()
        self._pending: dict[str, PendingApproval] = {}  # message_id -> PendingApproval
        self._lock = threading.Lock()

    @property
    def session_permissions(self) -> SessionPermissions:
        return self._session_perms

    def make_callback(self, chat_id: str):
        """为指定 chat_id 创建 canUseTool 回调闭包"""
        manager = self

        async def permission_callback(
            tool_name: str,
            input_data: dict[str, Any],
            context: Any,
        ):
            # 延迟导入避免循环依赖
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            # 1. 自动放行工具
            if tool_name in AUTO_APPROVE_TOOLS:
                return PermissionResultAllow()

            # 2. session 已信任
            if manager._session_perms.is_trusted(chat_id, tool_name):
                logger.info(f"[{chat_id[:8]}] {tool_name} 已信任，自动放行")
                return PermissionResultAllow()

            # 3. 发送授权请求
            request_id = uuid.uuid4().hex[:8]
            summary = _summarize_input(tool_name, input_data)

            approval_text = (
                f"🔐 工具授权请求 [{request_id}]\n"
                f"工具：{tool_name}\n"
                f"{summary}\n\n"
                f"回复本条消息：\n"
                f"  y — 允许一次\n"
                f"  trust — 本会话信任该工具\n"
                f"  n — 拒绝"
            )

            pending = PendingApproval(request_id)

            try:
                # 发送授权消息
                res = manager._send_fn(chat_id, approval_text)
                # 提取飞书返回的 message_id
                msg_id = None
                if isinstance(res, dict) and "data" in res:
                    msg_id = res["data"].get("message_id")
                if not msg_id:
                    logger.error(f"发送授权消息失败，无法获取 message_id: {res}")
                    return PermissionResultDeny(message="授权消息发送失败")

                pending.message_id = msg_id

                with manager._lock:
                    manager._pending[msg_id] = pending

                logger.info(f"[{chat_id[:8]}] 等待授权: {tool_name} [{request_id}] msg={msg_id}")

                # 4. 等待用户回复
                got_reply = pending.event.wait(timeout=APPROVAL_TIMEOUT)

                if not got_reply:
                    logger.info(f"[{chat_id[:8]}] 授权超时: {tool_name} [{request_id}]")
                    return PermissionResultDeny(message=f"授权超时（{APPROVAL_TIMEOUT}秒）")

                reply = pending.result
                if reply == "y":
                    logger.info(f"[{chat_id[:8]}] 授权通过（一次）: {tool_name}")
                    return PermissionResultAllow()
                elif reply == "trust":
                    manager._session_perms.trust_tool(chat_id, tool_name)
                    logger.info(f"[{chat_id[:8]}] 授权通过（信任会话）: {tool_name}")
                    return PermissionResultAllow()
                else:
                    logger.info(f"[{chat_id[:8]}] 授权拒绝: {tool_name}")
                    return PermissionResultDeny(message="用户拒绝了该操作")

            finally:
                with manager._lock:
                    if pending.message_id:
                        manager._pending.pop(pending.message_id, None)

        return permission_callback

    def handle_approval_reply(self, parent_message_id: str, reply_text: str) -> bool:
        """
        处理用户对授权消息的回复。

        Args:
            parent_message_id: 被回复的消息 ID（即授权请求消息的 ID）
            reply_text: 用户回复内容

        Returns:
            True 如果这是一条授权回复且已处理，False 如果不是
        """
        with self._lock:
            pending = self._pending.get(parent_message_id)

        if not pending:
            return False

        text = reply_text.strip().lower()
        if text in ("y", "yes", "ok", "允许"):
            pending.result = "y"
        elif text in ("trust", "信任"):
            pending.result = "trust"
        else:
            pending.result = "n"

        pending.event.set()
        logger.info(f"授权回复: {parent_message_id} -> {pending.result}")
        return True

    def has_pending(self, message_id: str) -> bool:
        """检查某个 message_id 是否有待处理的授权请求"""
        with self._lock:
            return message_id in self._pending
