"""
Session 级工具授权缓存

每个 chat_id 维护一个已信任的工具集合。
用户选择 "trust" 后，该工具在当前 chat session 内自动放行。
服务重启后清空（内存存储）。
"""
import threading


class SessionPermissions:
    def __init__(self):
        self._store: dict[str, set[str]] = {}  # chat_id -> set of trusted tool names
        self._lock = threading.Lock()

    def is_trusted(self, chat_id: str, tool_name: str) -> bool:
        with self._lock:
            return tool_name in self._store.get(chat_id, set())

    def trust_tool(self, chat_id: str, tool_name: str):
        with self._lock:
            if chat_id not in self._store:
                self._store[chat_id] = set()
            self._store[chat_id].add(tool_name)

    def clear(self, chat_id: str):
        with self._lock:
            self._store.pop(chat_id, None)
