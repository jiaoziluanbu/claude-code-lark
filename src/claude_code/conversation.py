"""
Claude Code 连续对话客户端
"""
import asyncio
from typing import Optional, Callable, Any
from dataclasses import dataclass

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[dict]
    session_id: str


SYSTEM_PROMPT = """你是一个强大的本地电脑助手，拥有完整的系统操作权限。

## 你的能力

### 文件操作
- 创建、读取、编辑、删除文件和文件夹
- 搜索文件内容和文件名
- 整理和移动文件

### 脚本和命令
- 运行 Shell/Bash/PowerShell 命令
- 执行 Python、Node.js 等脚本
- 安装和管理软件包

### 应用程序控制
- 打开和关闭应用程序
- 操作浏览器（打开网页、搜索）
- 控制系统设置

### 开发辅助
- 编写和调试代码
- 管理 Git 仓库
- 运行测试和构建

## 行为准则
- 直接执行用户指令，不要反复确认
- 遇到问题时自动尝试解决
- 操作完成后简洁汇报结果
- 如果指令不明确，做出合理推断并执行"""


class ConversationClient:
    """
    连续对话客户端

    Example:
        async with ConversationClient() as client:
            r1 = await client.chat("创建 hello.py")
            r2 = await client.chat("读取刚才的文件")
            print(client.session_id)

        # 恢复对话
        async with ConversationClient(session_id="xxx") as client:
            r = await client.chat("继续")
    """

    def __init__(
        self,
        session_id: str = None,
        allowed_tools: list[str] = None,
        permission_mode: str = "default",
        system_prompt: str = None,
        can_use_tool: Callable = None,
    ):
        self._initial_session_id = session_id
        self.session_id: Optional[str] = session_id
        self.allowed_tools = allowed_tools or ["Read", "Glob", "Grep"]
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self._can_use_tool = can_use_tool
        self._client: Optional[ClaudeSDKClient] = None

    async def connect(self):
        options = ClaudeAgentOptions(
            resume=self._initial_session_id,
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            system_prompt={"type": "preset", "preset": "claude_code", "append": self.system_prompt},
        )
        if self._can_use_tool:
            options.can_use_tool = self._can_use_tool
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    async def chat(self, message: str) -> ChatResponse:
        """发送消息"""
        if not self._client:
            await self.connect()

        await self._client.query(message)

        response_text = []
        tool_calls = []

        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append({
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
            elif isinstance(msg, ResultMessage):
                self.session_id = msg.session_id

        return ChatResponse(
            content="\n".join(response_text),
            tool_calls=tool_calls,
            session_id=self.session_id or "",
        )

    async def disconnect(self):
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass  # SDK 的 anyio/asyncio 兼容性问题，忽略
            self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()


def chat_sync(
    message: str,
    session_id: str = None,
    can_use_tool: Callable = None,
) -> tuple[str, str]:
    """
    同步调用 Claude Code（在独立线程中运行，避免事件循环冲突）

    Args:
        message: 用户消息
        session_id: 恢复之前的会话（可选）
        can_use_tool: canUseTool 回调（可选）

    Returns:
        (回复内容, session_id)
    """
    import concurrent.futures

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def _chat():
                async with ConversationClient(
                    session_id=session_id,
                    can_use_tool=can_use_tool,
                ) as client:
                    r = await client.chat(message)
                    return r.content, r.session_id
            return loop.run_until_complete(_chat())
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(_run_in_thread)
        return future.result()
