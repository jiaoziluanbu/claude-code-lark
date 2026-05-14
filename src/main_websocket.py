"""
飞书长连接方式接收消息

使用官方 SDK 的 WebSocket 长连接，无需公网域名
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
from dotenv import load_dotenv
load_dotenv()  # 必须在导入其他模块之前加载

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from queue import Queue

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from src.agent_backends import (
    SUPPORTED_BACKENDS,
    MODEL_ALIASES,
    chat_sync,
    get_default_model,
    normalize_backend,
    resolve_model_alias,
)
from src.feishu_utils.feishu_utils import send_message, reply_message, add_reaction, remove_reaction, download_message_resource
from src.data_base_utils import (
    get_agent_session,
    save_agent_session,
    delete_agent_session,
    get_chat_setting,
    save_chat_setting,
    delete_chat_setting,
)

from src.security import is_user_allowed, PermissionManager

# Claude API 图片超限错误特征
_IMAGE_LIMIT_ERROR = "exceeds the dimension limit"
# Claude Code 登录失效特征
_NOT_LOGGED_IN = "Not logged in"

_SERVICE_DIR = str(Path(__file__).parent.parent)


def _trigger_self_restart():
    """后台延迟 5 秒重启服务（先让当前回复发出去，再杀自己）"""
    def _do_restart():
        import time
        time.sleep(5)
        subprocess.Popen(
            ["bash", "-c", f"cd {_SERVICE_DIR} && ./stop.sh && sleep 1 && ./start.sh >> log.log 2>&1"],
            close_fds=True,
        )
    threading.Thread(target=_do_restart, daemon=True).start()

APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局 PermissionManager 实例
permission_manager = PermissionManager(send_message)

# 消息去重：防止同一条消息被处理两次
_processed_msg_ids: set[str] = set()
_msg_id_lock = threading.Lock()

# 正在处理中的 chat_id 队列（只保留未完成的）
_active_queues: dict[str, Queue] = {}
_queue_lock = threading.Lock()


DEFAULT_BACKEND = normalize_backend(os.getenv("DEFAULT_AGENT_BACKEND", "claude"))
DEFAULT_SANDBOX = os.getenv("CODEX_DEFAULT_SANDBOX", "danger-full-access")
DEFAULT_CWD = os.getenv(
    "CODEX_DEFAULT_CWD",
    str(Path.home() / "Documents" / "自制产品"),
)
WORKSPACE_ROOTS = [
    Path(p.strip()).expanduser()
    for p in os.getenv(
        "CODEX_WORKSPACE_ROOTS",
        f"{Path.home() / 'Documents' / '自制产品'},{Path.home()}",
    ).split(",")
    if p.strip()
] or [Path.home()]


def _get_backend(chat_id: str) -> str:
    return normalize_backend(get_chat_setting(chat_id, "backend") or DEFAULT_BACKEND)


def _model_setting_key(backend: str) -> str:
    return f"model:{backend}"


def _get_model(chat_id: str, backend: str) -> str:
    return get_chat_setting(chat_id, _model_setting_key(backend)) or get_default_model(backend)


def _get_sandbox(chat_id: str) -> str:
    return get_chat_setting(chat_id, "codex_sandbox") or DEFAULT_SANDBOX


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_roots_text() -> str:
    return "\n".join(f"  - {root}" for root in WORKSPACE_ROOTS)


def _resolve_cwd(raw: str | None) -> Path:
    value = (raw or DEFAULT_CWD).strip()
    candidate = Path(value).expanduser()

    if not candidate.is_absolute():
        existing = [root / candidate for root in WORKSPACE_ROOTS if (root / candidate).exists()]
        candidate = existing[0] if existing else WORKSPACE_ROOTS[0] / candidate

    resolved = candidate.resolve()
    roots = [root.resolve() for root in WORKSPACE_ROOTS]
    if not any(_is_inside(resolved, root) for root in roots):
        raise ValueError(f"工作目录必须位于允许的 workspace 内：\n{_workspace_roots_text()}")

    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"工作目录不是文件夹：{resolved}")

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _get_cwd(chat_id: str) -> Path:
    return _resolve_cwd(get_chat_setting(chat_id, "cwd"))


@dataclass
class PendingSwitch:
    kind: str
    current_backend: str
    target_backend: str
    current_model: str
    target_model: str | None = None


def _pending_switch_key() -> str:
    return "pending_switch"


def _load_pending_switch(chat_id: str) -> PendingSwitch | None:
    raw = get_chat_setting(chat_id, _pending_switch_key())
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return PendingSwitch(
            kind=str(data["kind"]),
            current_backend=str(data["current_backend"]),
            target_backend=str(data["target_backend"]),
            current_model=str(data["current_model"]),
            target_model=data.get("target_model"),
        )
    except Exception:
        delete_chat_setting(chat_id, _pending_switch_key())
        return None


def _save_pending_switch(chat_id: str, pending: PendingSwitch):
    save_chat_setting(
        chat_id,
        _pending_switch_key(),
        json.dumps(pending.__dict__, ensure_ascii=False),
    )


def _clear_pending_switch(chat_id: str):
    delete_chat_setting(chat_id, _pending_switch_key())


def _pending_switch_text(pending: PendingSwitch) -> str:
    if pending.kind == "backend":
        target = f"后端 {pending.target_backend}"
        current = f"当前后端 {pending.current_backend}"
    else:
        target = f"{pending.target_backend} 模型 {pending.target_model}"
        current = f"当前模型 {pending.current_model}"
    return (
        f"准备切换到 {target}。\n\n"
        f"注意：切换后不会继承 {current} 的原生会话上下文；"
        "旧会话会保留，之后切回来还能继续。\n\n"
        "输入 /switch apply 确认切换。\n"
        "输入 /switch cancel 继续使用当前设置。"
    )


def _apply_pending_switch(chat_id: str, pending: PendingSwitch) -> str:
    if pending.kind == "backend":
        save_chat_setting(chat_id, "backend", pending.target_backend)
        return f"已切换后端: {pending.current_backend} → {pending.target_backend}"

    target_model = pending.target_model or get_default_model(pending.target_backend)
    save_chat_setting(chat_id, _model_setting_key(pending.target_backend), target_model)
    delete_agent_session(chat_id, pending.target_backend)
    return (
        f"{pending.target_backend} 模型已切换: "
        f"{pending.current_model} → {target_model}\n"
        "已重置当前后端 session。"
    )


# ── 斜杠命令 ──

def _handle_command(chat_id: str, message_id: str, chat_type: str, command: str) -> bool:
    """
    处理 / 命令，返回 True 表示已处理。
    """
    parts = command.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    backend = _get_backend(chat_id)
    is_zhao = _is_zhao_chat(chat_id)

    if cmd == "/help":
        text = (
            "可用命令：\n"
            "/reset — 清除当前会话，重新开始\n"
            "/status — 查看当前会话状态\n"
            "/backend — 查看/切换后端（claude/codex）\n"
            "/backend <claude|codex> — 切换后端\n"
            "/switch apply — 确认待执行的后端/模型切换\n"
            "/switch cancel — 放弃待执行的后端/模型切换\n"
            "/cwd — 查看 Codex 工作目录\n"
            "/cwd <路径> — 切换 Codex 工作目录，可用相对路径\n"
            "/sandbox — 查看/切换 Codex 权限\n"
            "/sandbox <read-only|workspace-write|danger-full-access> — 切换权限\n"
            "/model — 查看/切换模型\n"
            "/model <别名或模型名> — 切换当前后端模型\n"
            "/trust — 查看已信任的工具列表\n"
            "/trust clear — 清除已信任的工具\n"
            "/help — 显示本帮助"
        )
    elif cmd == "/reset":
        if is_zhao:
            session_id = get_agent_session(chat_id, "claude")
            delete_agent_session(chat_id, "claude")
            text = "昭的对话 session 已重置，下次消息将重新开始。"
        elif arg.lower() == "all":
            for name in SUPPORTED_BACKENDS:
                delete_agent_session(chat_id, name)
            session_id = None
            text = "所有后端会话已重置，下次消息将开启新对话。"
        else:
            session_id = get_agent_session(chat_id, backend)
            delete_agent_session(chat_id, backend)
            text = f"{backend} 会话已重置，下次消息将开启新对话。"
        permission_manager.session_permissions.clear(chat_id)
        if session_id:
            text += f"\n（旧 session: {session_id[:8]}...）"
    elif cmd == "/status":
        trusted = permission_manager.session_permissions._store.get(chat_id, set())
        is_busy = chat_id in _active_queues
        pending = _load_pending_switch(chat_id)
        if is_zhao:
            session_id = get_agent_session(chat_id, "claude")
            text = (
                f"会话状态：\n"
                f"  路由: 昭（podcast-cohost）\n"
                f"  session: {session_id[:8] + '...' if session_id else '无（新会话）'}\n"
                f"  已信任工具: 不适用（昭不使用本机工具）\n"
                f"  处理中: {'是' if is_busy else '否'}"
            )
        else:
            session_id = get_agent_session(chat_id, backend)
            model = _get_model(chat_id, backend)
            cwd = _get_cwd(chat_id)
            sandbox = _get_sandbox(chat_id)
            text = (
                f"会话状态：\n"
                f"  后端: {backend}\n"
                f"  模型: {model}\n"
                f"  Codex cwd: {cwd}\n"
                f"  Codex sandbox: {sandbox}\n"
                f"  session: {session_id[:8] + '...' if session_id else '无（新会话）'}\n"
                f"  待确认切换: {'有' if pending else '无'}\n"
                f"  已信任工具: {', '.join(sorted(trusted)) if trusted else '无'}\n"
                f"  处理中: {'是' if is_busy else '否'}"
            )
    elif is_zhao and cmd in {"/backend", "/cwd", "/sandbox", "/model", "/trust"}:
        text = (
            "这个聊天已绑定到昭路由。\n"
            "远程开发命令（/backend、/cwd、/sandbox、/trust）不作用于昭；"
            "昭的模型由 podcast-cohost 的 model_registry 管理。"
        )
    elif cmd == "/switch":
        pending = _load_pending_switch(chat_id)
        action = arg.lower()
        if not pending:
            text = "当前没有待确认的后端/模型切换。"
        elif action in {"apply", "yes", "y", "确认"}:
            text = _apply_pending_switch(chat_id, pending)
            _clear_pending_switch(chat_id)
        elif action in {"cancel", "no", "n", "取消"}:
            _clear_pending_switch(chat_id)
            text = "已取消切换，继续使用当前后端和模型。"
        else:
            text = _pending_switch_text(pending)
    elif cmd == "/backend":
        if not arg:
            text = (
                f"当前后端: {backend}\n"
                f"可选: {', '.join(SUPPORTED_BACKENDS)}\n\n"
                "用法: /backend codex"
            )
        else:
            new_backend = normalize_backend(arg)
            if new_backend not in SUPPORTED_BACKENDS:
                text = f"未知后端: {arg}\n可选: {', '.join(SUPPORTED_BACKENDS)}"
            elif new_backend == backend:
                _clear_pending_switch(chat_id)
                text = f"当前已经是 {backend} 后端。"
            else:
                pending = PendingSwitch(
                    kind="backend",
                    current_backend=backend,
                    target_backend=new_backend,
                    current_model=_get_model(chat_id, backend),
                )
                _save_pending_switch(chat_id, pending)
                text = _pending_switch_text(pending)
    elif cmd == "/cwd":
        if not arg:
            text = (
                f"当前 Codex 工作目录:\n{_get_cwd(chat_id)}\n\n"
                f"允许的 workspace:\n{_workspace_roots_text()}\n\n"
                "用法: /cwd PhotoCleaner 或 /cwd ~/Documents/自制产品/PhotoCleaner"
            )
        else:
            try:
                cwd = _resolve_cwd(arg)
                save_chat_setting(chat_id, "cwd", str(cwd))
                delete_agent_session(chat_id, "codex")
                text = f"Codex 工作目录已切换:\n{cwd}\n\n已重置当前聊天的 Codex session。"
            except Exception as e:
                text = f"切换工作目录失败：{e}"
    elif cmd == "/sandbox":
        allowed = {"read-only", "workspace-write", "danger-full-access", "danger"}
        if not arg:
            text = (
                f"当前 Codex sandbox: {_get_sandbox(chat_id)}\n"
                "可选: read-only, workspace-write, danger-full-access\n\n"
                "用法: /sandbox danger-full-access"
            )
        else:
            value = "danger-full-access" if arg.lower() == "danger" else arg.lower()
            if value not in allowed:
                text = "未知 sandbox。可选: read-only, workspace-write, danger-full-access"
            else:
                save_chat_setting(chat_id, "codex_sandbox", value)
                delete_agent_session(chat_id, "codex")
                text = f"Codex sandbox 已切换为: {value}\n已重置当前聊天的 Codex session。"
    elif cmd == "/model":
        model = _get_model(chat_id, backend)
        current = model
        aliases = MODEL_ALIASES.get(backend, {})
        if not arg:
            model_list = "\n".join(f"  {k} → {v}" for k, v in aliases.items())
            text = f"当前后端: {backend}\n当前模型: {current}\n\n可选别名：\n{model_list}\n\n也可以直接输入完整模型名。"
        else:
            new_model = resolve_model_alias(backend, arg)
            if new_model == current:
                _clear_pending_switch(chat_id)
                text = f"当前 {backend} 已经在使用模型: {current}"
            else:
                pending = PendingSwitch(
                    kind="model",
                    current_backend=backend,
                    target_backend=backend,
                    current_model=current,
                    target_model=new_model,
                )
                _save_pending_switch(chat_id, pending)
                text = _pending_switch_text(pending)
    elif cmd == "/trust":
        if arg.lower() == "clear":
            permission_manager.session_permissions.clear(chat_id)
            text = "已清除所有信任的工具，后续操作将重新请求授权。"
        else:
            trusted = permission_manager.session_permissions._store.get(chat_id, set())
            if trusted:
                text = f"已信任的工具: {', '.join(sorted(trusted))}\n\n发送 /trust clear 可清除"
            else:
                text = "当前没有已信任的工具，所有非只读操作都会请求授权。"
    else:
        return False

    if chat_type == "group":
        reply_message(message_id, text)
    else:
        send_message(chat_id, text)
    return True


# ===== 昭（podcast-cohost）路由 =====
# 当 chat_id 在 ZHAO_CHAT_IDS 环境变量里（逗号分隔），走昭的 chat_sync
# 否则走默认 Claude Code
import os as _os
import sys as _sys
_ZHAO_CHAT_IDS = set(filter(None, _os.environ.get("ZHAO_CHAT_IDS", "").split(",")))
_ZHAO_PROJECT_PATH = _os.environ.get(
    "ZHAO_PROJECT_PATH",
    _os.path.expanduser("~/Documents/自制产品/podcast-cohost"),
)
_zhao_chat_sync = None
if _ZHAO_CHAT_IDS and _os.path.isdir(_ZHAO_PROJECT_PATH):
    if _ZHAO_PROJECT_PATH not in _sys.path:
        _sys.path.insert(0, _ZHAO_PROJECT_PATH)
    try:
        from engine.zhao_chat_sync import chat_sync as _zhao_chat_sync
        logger.info(f"[zhao] 已启用，监听 chat_ids: {_ZHAO_CHAT_IDS}")
    except Exception as _e:
        logger.warning(f"[zhao] 路由加载失败，回退到默认: {_e}")


def _is_zhao_chat(chat_id: str) -> bool:
    return _zhao_chat_sync is not None and chat_id in _ZHAO_CHAT_IDS


def chat_with_agent(chat_id: str, message: str) -> str:
    """
    调用当前 Agent 后端，基于 chat_id 保持对话连续性。
    若遇到图片超限错误，自动清除 session 并用新会话重试一次。

    若 chat_id 在 ZHAO_CHAT_IDS，走昭的 chat_sync（人格/情绪/记忆系统）。
    """
    is_zhao = _is_zhao_chat(chat_id)
    backend = "claude" if is_zhao else _get_backend(chat_id)
    model = _get_model(chat_id, backend)
    cwd = str(_get_cwd(chat_id))
    sandbox = _get_sandbox(chat_id)

    def _do_chat(session_id):
        if is_zhao:
            result = _zhao_chat_sync(message, session_id=session_id)
            if isinstance(result, tuple) and len(result) >= 2:
                return result[0], result[1]
            return str(result), session_id or ""
        can_use_tool = permission_manager.make_callback(chat_id)
        return chat_sync(
            backend,
            message,
            session_id=session_id,
            can_use_tool=can_use_tool,
            cwd=cwd,
            model=model,
            sandbox=sandbox,
        )

    session_id = get_agent_session(chat_id, backend)
    reply, new_session_id = _do_chat(session_id)

    # 登录失效：通知用户并触发自动重启
    if backend == "claude" and _NOT_LOGGED_IN in reply:
        logger.warning(f"[{chat_id[:8]}] 检测到登录失效，触发自动重启")
        delete_agent_session(chat_id, backend)
        _trigger_self_restart()
        return "⚠️ 检测到登录态失效，正在自动重启服务，约 10 秒后恢复，请稍后重发消息。"
    if backend == "codex" and ("not logged in" in reply.lower() or "codex login" in reply.lower()):
        delete_agent_session(chat_id, backend)
        return "⚠️ Codex 登录态可能失效。请在本机终端运行 `codex login` 后重试。"

    # 图片超限：清掉旧 session，用新会话重试一次
    if _IMAGE_LIMIT_ERROR in reply:
        logger.warning(f"[{chat_id[:8]}] 图片超限，清除 session 后重试")
        delete_agent_session(chat_id, backend)
        permission_manager.session_permissions.clear(chat_id)
        reply, new_session_id = _do_chat(None)
        # 把重试结果前加提示
        reply = f"⚠️ 会话已自动重置（图片超过 2000px 限制）\n\n{reply}"

    # 保存到 SQLite
    if new_session_id and new_session_id != session_id:
        save_agent_session(chat_id, backend, new_session_id)
        logger.info(f"会话映射: {backend}:{chat_id[:8]}... -> {new_session_id[:8]}...")

    return reply


def _process_chat_queue(chat_id: str, message_id: str, chat_type: str, queue: Queue):
    """
    处理指定 chat_id 的消息队列（FIFO）
    同一 chat_id 串行处理，不同 chat_id 可并行
    队列中的每条消息格式: (text, reaction_id)
    """
    while True:
        # 在锁内检查并获取消息，确保线程安全
        with _queue_lock:
            if queue.empty():
                # 队列空了，销毁并退出
                _active_queues.pop(chat_id, None)
                return
            text, reaction_id = queue.get_nowait()

        try:
            reply = chat_with_agent(chat_id, text)
            if chat_type == "group":
                reply_message(message_id, reply)
            else:
                send_message(chat_id, reply)
            logger.info(f"回复: {reply[:100]}...")
        except Exception as e:
            logger.error(f"处理失败 [{chat_id[:8]}...]: {e}")
        finally:
            # 移除 OnIt
            try:
                if reaction_id:
                    remove_reaction(message_id, reaction_id)
            except Exception:
                pass


def enqueue_message(chat_id: str, message_id: str, text: str, chat_type: str, reaction_id: str = None):
    """
    将消息加入队列，无队列则创建
    """
    with _queue_lock:
        if chat_id in _active_queues:
            # 已有队列，直接加入
            _active_queues[chat_id].put((text, reaction_id))
        else:
            # 创建新队列并启动 worker
            queue = Queue()
            queue.put((text, reaction_id))
            _active_queues[chat_id] = queue
            threading.Thread(
                target=_process_chat_queue,
                args=(chat_id, message_id, chat_type, queue),
                daemon=True
            ).start()


def handle_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """处理接收到的消息"""
    try:
        event = data.event
        message = event.message
        message_id = message.message_id
        sender_id = event.sender.sender_id.open_id

        # ── 消息去重 ──
        with _msg_id_lock:
            if message_id in _processed_msg_ids:
                logger.debug(f"重复消息，忽略: {message_id}")
                return
            _processed_msg_ids.add(message_id)
            # 防止内存泄漏，保留最近 1000 条
            if len(_processed_msg_ids) > 1000:
                _processed_msg_ids.clear()

        # ── 白名单检查 ──
        if not is_user_allowed(sender_id):
            logger.debug(f"非白名单用户，忽略: {sender_id}")
            return

        # 解析消息内容
        msg_type = message.message_type
        content = json.loads(message.content)
        text = ""

        if msg_type == "text":
            text = content.get("text", "")
        elif msg_type == "post":
            # 富文本消息：提取所有文本段落
            post = content.get("content", [])
            # post 结构: [[{"tag":"text","text":"..."},{"tag":"at",...}], [...]]
            lines = []
            for paragraph in post:
                parts = []
                for element in paragraph:
                    tag = element.get("tag", "")
                    if tag == "text":
                        parts.append(element.get("text", ""))
                    elif tag == "a":
                        parts.append(element.get("href", ""))
                lines.append("".join(parts))
            text = "\n".join(lines)
        elif msg_type == "image":
            # 图片消息：下载到本地，让 Claude 通过 Read 工具查看
            image_key = content.get("image_key", "")
            if image_key:
                file_path = download_message_resource(message_id, image_key, "image")
                if file_path:
                    text = f"用户发了一张图片，已保存到本地: {file_path}\n请用 Read 工具查看这张图片并描述/分析内容。"
                else:
                    text = "用户发了一张图片，但下载失败。"
            else:
                logger.info(f"图片消息缺少 image_key")
                return
        elif msg_type == "file":
            # 文件消息：下载到本地
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "未知文件")
            if file_key:
                file_path = download_message_resource(message_id, file_key, "file")
                if file_path:
                    text = f"用户发了一个文件「{file_name}」，已保存到本地: {file_path}\n请读取并处理这个文件。"
                else:
                    text = f"用户发了一个文件「{file_name}」，但下载失败。"
            else:
                logger.info(f"文件消息缺少 file_key")
                return
        else:
            logger.info(f"不支持的消息类型: {msg_type}")
            return

        # 去掉 @机器人
        if message.mentions:
            for mention in message.mentions:
                text = text.replace(f"@{mention.name}", "").strip()

        if not text:
            return

        # ── 授权回复路由 ──
        # 如果是回复消息且 parent_id 对应一个待处理的授权请求，走授权流程
        parent_id = getattr(message, "parent_id", None) or ""
        if parent_id and permission_manager.handle_approval_reply(parent_id, text):
            logger.info(f"授权回复已处理: {message_id} -> parent={parent_id}")
            return

        chat_id = message.chat_id
        logger.info(f"收到消息: {message_id} from={sender_id}: {text}")

        # ── 斜杠命令路由 ──
        if text.startswith("/"):
            if _handle_command(chat_id, message_id, message.chat_type, text):
                return

        # 👀 立刻标记正在处理
        reaction_id = None
        try:
            res = add_reaction(message_id, "OnIt")
            if isinstance(res, dict) and res.get("data"):
                reaction_id = res["data"].get("reaction_id")
        except Exception:
            pass

        # 加入队列，按 chat_id 串行处理
        enqueue_message(chat_id, message_id, text, message.chat_type, reaction_id)

    except Exception as e:
        logger.error(f"处理消息失败: {e}")


def main():
    # 创建客户端
    client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(handle_message)
            .build(),
        log_level=lark.LogLevel.INFO,
    )

    logger.info("启动飞书长连接（已启用安全层）...")
    client.start()


if __name__ == "__main__":
    main()
