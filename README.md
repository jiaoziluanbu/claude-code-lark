# remote-feishu

通过飞书远程操作本机 Agent。当前支持 Claude Code 和 Codex 双后端，可在不同聊天里保留独立上下文、切换模型、指定工作目录，并把飞书变成一个轻量的远程开发入口。

## 核心特性

**多人协作，独立上下文**

- 支持多人同时使用，互不干扰
- 每个聊天窗口（私聊/群聊）拥有独立的对话上下文
- 上下文自动持久化，重启后可继续之前的对话

```
┌─────────────────────────────────────────────────────┐
│  飞书聊天窗口          Agent Session               │
├─────────────────────────────────────────────────────┤
│  张三 私聊  ────────►  claude/codex session_abc      │
│  李四 私聊  ────────►  claude/codex session_xyz      │
│  项目群聊   ────────►  claude/codex session_123      │
│  测试群聊   ────────►  claude/codex session_456      │
└─────────────────────────────────────────────────────┘
```

**远程开发能力**

- 文件读写、创建、删除
- 执行 Shell/Python 脚本
- 打开/关闭应用程序
- Git 操作、包管理等开发任务
- Claude Code / Codex 后端按聊天独立切换
- Codex 可按聊天设置工作目录和 sandbox


**相似之处：**
- 都是将 Agent 接入企业聊天工具
- 都支持多轮连续对话
- 都通过 session/thread 管理对话上下文

**本项目特色：**
- 通过 session/thread 管理对话上下文
- 支持 Claude Code 和 Codex，两套 session 分开保存
- **每个聊天窗口独立上下文**，群聊成员共享同一上下文
- 可用 `/backend`、`/model`、`/cwd`、`/sandbox` 在飞书里切换运行方式
- 后端/模型切换需要 `/switch apply` 二次确认，避免上下文突然断开
- 飞书长连接方式，无需公网域名
- 轻量级，Python 服务即可运行

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入飞书应用的 APP_ID 和 APP_SECRET
```

```env
APP_ID=cli_xxxxxx
APP_SECRET=xxxxxx
```

### 3. 安装 Claude Code / Codex

```bash
# macOS/Linux
curl -fsSL https://claude.ai/install.sh | bash

# Windows
npm install -g @anthropic-ai/claude-code

# 登录
claude login
```

如果要使用 Codex：

```bash
npm install -g @openai/codex
codex login
```

### 4. 飞书应用配置

1. 进入 [飞书开放平台](https://open.feishu.cn/)
2. 创建应用，获取 APP_ID 和 APP_SECRET
3. 事件订阅 → 选择"使用长连接接收事件"
4. 添加事件：`im.message.receive_v1`
5. 权限管理 → 添加 `im:message` 相关权限

### 5. 启动

```bash
# 前台运行
python -m src.main_websocket

# 后台运行
./start.sh

# 停止
./stop.sh

# 查看日志
tail -f log.log
```

## 使用示例

在飞书中 @机器人：

```
@机器人 帮我创建一个 hello.py 文件
@机器人 运行刚才的脚本
@机器人 打开网易云音乐
@机器人 当前目录有哪些文件
```

常用命令：

```text
/backend                  查看当前后端
/backend codex            准备切换到 Codex
/backend claude           准备切换到 Claude Code
/switch apply             确认待执行的后端/模型切换
/switch cancel            取消待执行的后端/模型切换
/model                    查看当前后端模型
/model codex              准备切到 Codex 默认别名 gpt-5.3-codex
/cwd                      查看 Codex 工作目录
/cwd PhotoCleaner         切到 ~/Documents/自制产品/PhotoCleaner
/sandbox                  查看 Codex 权限
/sandbox workspace-write  降级到 workspace-write
/sandbox danger           切回全权限远程开发
/reset                    重置当前后端会话
/reset all                重置 Claude 和 Codex 两边会话
```

## 项目结构

```
├── src/
│   ├── main_websocket.py      # 主程序（飞书长连接）
│   ├── claude_code/           # Claude Code 封装
│   │   ├── conversation.py    # 对话客户端
│   │   └── __init__.py
│   ├── agent_backends/        # Claude/Codex 后端统一入口
│   │   ├── codex_cli.py       # Codex CLI 后端
│   │   └── __init__.py
│   ├── feishu_utils/          # 飞书工具
│   │   └── feishu_utils.py
│   └── data_base_utils/       # 数据库
│       └── session_store.py   # 会话存储
├── data/
│   └── sessions.db            # SQLite 数据库
├── .env                       # 环境变量
├── start.sh / stop.sh         # 启停脚本
└── requirements.txt
```

## 技术栈

- Python 3.10+
- claude-agent-sdk（Claude Code Python SDK）
- lark-oapi（飞书 SDK）
- SQLite（会话持久化）

## 扩展其他 Agent

本项目采用模块化设计，可轻松扩展后端 Agent：

```
┌──────────────┐      ┌─────────────────┐      ┌────────────────────┐
│   飞书消息    │ ───► │  main_websocket │ ───► │   Agent 后端        │
│   (chat_id)  │      │   (路由/分发)    │      │                    │
└──────────────┘      └─────────────────┘      └────────────────────┘
                                                        │
                              ┌──────────────────────────┼──────────────────────────┐
                              ▼                          ▼                          ▼
                      ┌──────────────┐          ┌──────────────┐          ┌──────────────┐
                      │ Claude Code  │          │    Codex     │          │  自定义 Agent │
                      │              │          │              │          │              │
                      └──────────────┘          └──────────────┘          └──────────────┘
```

**扩展方式**

只需实现一个 `chat_sync(...)` 函数，并在 `src/agent_backends/__init__.py` 注册：

```python
# src/agent_backends/your_agent.py

def chat_sync(
    message: str,
    session_id: str = None,
    cwd: str = None,
    model: str = None,
    sandbox: str = None,
    **kwargs,
) -> tuple[str, str]:
    """
    Args:
        message: 用户消息
        session_id: 会话 ID（用于保持上下文）
        cwd: 工作目录
        model: 模型名
        sandbox: 权限策略
    
    Returns:
        (回复内容, 新的 session_id)
    """
    reply = your_agent.chat(message, session_id)
    return reply, session_id
```

**可扩展的 Agent 示例**

| Agent | 能力 | 适用场景 |
|-------|------|---------|
| Claude Code | 本地文件/命令操作 | 开发助手、自动化 |
| Codex | 本地远程开发、代码修改、测试 | 编程、重构、调试 |
| OpenAI Assistants | 对话 + 代码解释器 | 数据分析、问答 |
| LangChain Agent | 自定义工具链 | 复杂工作流 |
| Dify/Coze | 可视化编排 | 快速原型 |
| 本地 LLM | Ollama/vLLM | 私有部署 |
