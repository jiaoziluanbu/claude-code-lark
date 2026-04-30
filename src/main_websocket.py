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
from queue import Queue

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from src.claude_code import chat_sync, set_default_model, get_default_model
from src.feishu_utils.feishu_utils import send_message, reply_message, add_reaction, remove_reaction, download_message_resource
from src.data_base_utils import get_session, save_session, delete_session

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


# ── 斜杠命令 ──

def _handle_command(chat_id: str, message_id: str, chat_type: str, command: str) -> bool:
    """
    处理 / 命令，返回 True 表示已处理。
    """
    parts = command.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    AVAILABLE_MODELS = {
        "opus": "claude-opus-4-7",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    }

    if cmd == "/help":
        model_list = "  ".join(AVAILABLE_MODELS.keys())
        text = (
            "可用命令：\n"
            "/reset — 清除当前会话，重新开始\n"
            "/status — 查看当前会话状态\n"
            "/model — 查看/切换模型\n"
            f"/model <{model_list}> — 切换模型\n"
            "/trust — 查看已信任的工具列表\n"
            "/trust clear — 清除已信任的工具\n"
            "/help — 显示本帮助"
        )
    elif cmd == "/reset":
        session_id = get_session(chat_id)
        delete_session(chat_id)
        permission_manager.session_permissions.clear(chat_id)
        text = "会话已重置，下次消息将开启新对话。"
        if session_id:
            text += f"\n（旧 session: {session_id[:8]}...）"
    elif cmd == "/status":
        session_id = get_session(chat_id)
        trusted = permission_manager.session_permissions._store.get(chat_id, set())
        is_busy = chat_id in _active_queues
        model = get_default_model()
        text = (
            f"会话状态：\n"
            f"  模型: {model}\n"
            f"  session: {session_id[:8] + '...' if session_id else '无（新会话）'}\n"
            f"  已信任工具: {', '.join(sorted(trusted)) if trusted else '无'}\n"
            f"  处理中: {'是' if is_busy else '否'}"
        )
    elif cmd == "/model":
        current = get_default_model()
        if not arg:
            model_list = "\n".join(f"  {k} → {v}" for k, v in AVAILABLE_MODELS.items())
            text = f"当前模型: {current}\n\n可选模型：\n{model_list}\n\n用法: /model opus"
        elif arg.lower() in AVAILABLE_MODELS:
            new_model = AVAILABLE_MODELS[arg.lower()]
            set_default_model(new_model)
            text = f"模型已切换: {current} → {new_model}\n（新会话生效，建议 /reset）"
        else:
            text = f"未知模型: {arg}\n可选: {', '.join(AVAILABLE_MODELS.keys())}"
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


def chat_with_claude(chat_id: str, message: str) -> str:
    """
    调用 Claude Code，基于 chat_id 保持对话连续性。
    若遇到图片超限错误，自动清除 session 并用新会话重试一次。
    """
    def _do_chat(session_id):
        can_use_tool = permission_manager.make_callback(chat_id)
        return chat_sync(message, session_id=session_id, can_use_tool=can_use_tool)

    session_id = get_session(chat_id)
    reply, new_session_id = _do_chat(session_id)

    # 登录失效：通知用户并触发自动重启
    if _NOT_LOGGED_IN in reply:
        logger.warning(f"[{chat_id[:8]}] 检测到登录失效，触发自动重启")
        delete_session(chat_id)
        _trigger_self_restart()
        return "⚠️ 检测到登录态失效，正在自动重启服务，约 10 秒后恢复，请稍后重发消息。"

    # 图片超限：清掉旧 session，用新会话重试一次
    if _IMAGE_LIMIT_ERROR in reply:
        logger.warning(f"[{chat_id[:8]}] 图片超限，清除 session 后重试")
        delete_session(chat_id)
        permission_manager.session_permissions.clear(chat_id)
        retry_msg = f"（上一个会话因图片过大已自动重置）\n{message}"
        reply, new_session_id = _do_chat(None)
        # 把重试结果前加提示
        reply = f"⚠️ 会话已自动重置（图片超过 2000px 限制）\n\n{reply}"

    # 保存到 SQLite
    if new_session_id != session_id:
        save_session(chat_id, new_session_id)
        logger.info(f"会话映射: {chat_id[:8]}... -> {new_session_id[:8]}...")

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
            reply = chat_with_claude(chat_id, text)
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
