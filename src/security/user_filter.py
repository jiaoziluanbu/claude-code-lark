"""
发送者白名单过滤
"""
import os
import logging

logger = logging.getLogger(__name__)

_allowed_user_ids: set[str] | None = None


def _load_allowed_users() -> set[str]:
    raw = os.getenv("ALLOWED_USER_IDS", "")
    if not raw.strip():
        return set()
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def is_user_allowed(open_id: str) -> bool:
    """检查发送者是否在白名单中。白名单为空时允许所有人（向后兼容）。"""
    global _allowed_user_ids
    if _allowed_user_ids is None:
        _allowed_user_ids = _load_allowed_users()
        if _allowed_user_ids:
            logger.info(f"白名单已加载: {len(_allowed_user_ids)} 个用户")
        else:
            logger.warning("ALLOWED_USER_IDS 未配置，允许所有用户")

    if not _allowed_user_ids:
        return True
    return open_id in _allowed_user_ids
