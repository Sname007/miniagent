# Mini Agent 5.0

一个基于 Python 的轻量级 AI Agent 桌面应用。
受SWE Agent仅靠单shell工具在agent横向测试中领先启发，仅保留一个有状态shell tool，并提高一定模块间耦合性精简而来。


## 项目结构

```
miniagent5.0/
├── main.py          # 应用入口，启动桌面窗口
├── agent.py         # Agent 核心逻辑，处理 AI 对话
├── server.py        # FastAPI 后端服务
├── config.py        # 配置文件
├── tools.py         # PowerShell 工具实现
├── sessions.py      # 会话管理
├── ui/              # 前端界面
│   ├── index.html
│   └── marked.min.js
├── history_data/    # 会话历史存储
└── requirements.txt # 依赖列表
```

## 安装依赖

```bash
pip install -r requirements.txt
```

依赖列表：

- `fastapi>=0.110.0`
- `uvicorn>=0.27.0`
- `openai>=2.0.0`
- `deepseek-tokenizer>=0.1.0`
- `pywebview>=5.0`

## 运行方式

### 方式一：桌面应用模式（推荐）

```bash
python main.py
```

### 方式二：Web 服务模式

```bash
python server.py
```

然后在浏览器中访问 `http://127.0.0.1:8999`

## 配置说明

编辑 `config.py` 文件：

```python
# API 配置
API_KEY = "your-api-key"
BASE_URL = "https://api.deepseek.com"
MODEL_NAME = "deepseek-flash"

# 推理努力级别：low/medium/high
REASONING_EFFORT = "low"

# Token 限制
MAX_TOKENS = 1000000
API_TIMEOUT = 600.0

# 服务配置
HOST = "127.0.0.1"
PORT = 8999
```

也可通过界面动态修改 API 配置。

## 工具说明

Agent 内置 `bash` 工具，用于执行 PowerShell 命令：

| 参数    | 类型    | 必需 | 说明                              |
| ------- | ------- | ---- | --------------------------------- |
| command | string  | 是   | 要执行的 PowerShell 命令          |
| timeout | integer | 否   | 超时时间（秒），默认 60，最大 600 |
| workdir | string  | 否   | 工作目录（绝对路径）              |

## API 端点

| 方法   | 路径                       | 说明                 |
| ------ | -------------------------- | -------------------- |
| GET    | `/api/config`              | 获取当前配置         |
| POST   | `/api/config`              | 更新配置             |
| POST   | `/api/test`                | 测试 API 连接        |
| GET    | `/api/sessions`            | 获取会话列表         |
| POST   | `/api/sessions`            | 创建新会话           |
| DELETE | `/api/sessions/{id}`       | 删除会话             |
| POST   | `/api/sessions/{id}/clear` | 清空会话             |
| GET    | `/api/history/{id}`        | 获取会话历史         |
| GET    | `/api/context/{id}`        | 获取上下文信息       |
| POST   | `/api/chat/{id}`           | 发送消息（SSE 流式） |
| POST   | `/api/chat/{id}/stop`      | 停止生成             |

## 技术栈

- **后端**：Python 3.10+、FastAPI、uvicorn（支持会话并发、工具异步执行）
- **前端**：HTML/CSS/JavaScript、pywebview
- **AI**：OpenAI Responses API（流式）
- **存储**：JSON 文件持久化

## 注意事项

1. 需要安装 PowerShell（pwsh 或 powershell.exe）
2. 命令中的路径必须使用绝对路径
3. 不支持需要交互输入的命令
4. 超时会导致 shell 重启，状态丢失
