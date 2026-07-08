# Code Query MCP

MCP 源码查询服务 —— 定时从局域网 GitLab 拉取最新源码，通过 LLM Agent 深度解读代码逻辑，支持多项目隔离和 openviking 记忆服务。

## 功能特性

- **定时源码同步**：自动从局域网 GitLab 拉取最新源码（全量/增量同步），更新代码索引和调用图
- **深度代码解读**：LLM Agent 自主搜索代码、阅读文件、追踪调用链，给出完整的逻辑解读和精确代码位置
- **多项目隔离**：代码存储、向量索引、调用图、记忆全链路按项目隔离
- **记忆能力**：通过 openviking 服务实现持久化记忆，存储项目架构、代码逻辑、问答历史等
- **MCP 协议**：标准 MCP 协议接口，可被 Claude Desktop、IDE 等客户端调用

## 架构概览

```
MCP Client (Claude/IDE)
    │ MCP Protocol (stdio/SSE)
    ▼
MCP Server ── 暴露 11 个工具
    │
    ▼
核心服务层
├── 代码深度解读引擎 (ReAct Agent 推理循环)
├── 代码导航工具集 (搜索/读取/追踪调用链)
├── 代码索引引擎 (ChromaDB 语义+关键词)
├── 调用图 (tree-sitter 多语言解析)
├── 项目管理器 (多项目上下文隔离)
├── 记忆服务 (openviking HTTP 集成)
├── GitLab 客户端 (OAuth2 账号密码认证)
└── 调度器 (APScheduler 定时同步)
```

## 快速开始

### 1. 安装依赖

```bash
pip install -e .
```

### 2. 配置

编辑 `config/config.yaml`：

```yaml
server:
  transport: stdio            # stdio | sse

gitlab:
  url: "https://gitlab.local.example.com"
  username: "${GITLAB_USERNAME}"    # 环境变量或直接填写
  password: "${GITLAB_PASSWORD}"
  projects:
    - id: "backend-service"          # 项目唯一标识
      name: "后端服务"
      source_url: "https://gitlab.local.example.com/team/backend-service"
      default_branch: "main"
      language: "java"

llm:
  base_url: "https://api.openai.com/v1"
  api_key: "${LLM_API_KEY}"
  model: "gpt-4o"                    # 主模型 ID（代码深度解读）
  summary_model: "gpt-4o-mini"      # 摘要模型 ID（同步摘要）

embedding:
  base_url: "https://api.openai.com/v1"
  api_key: "${LLM_API_KEY}"
  model: "text-embedding-3-small"

memory:
  openviking_endpoint: "http://openviking.local:8000"

scheduler:
  sync_interval_cron: "*/30 * * * *"  # 每 30 分钟同步
  full_sync_on_start: true            # 启动时全量同步
```

### 3. 设置环境变量

```powershell
$env:GITLAB_USERNAME = "your-username"
$env:GITLAB_PASSWORD = "your-password"
$env:LLM_API_KEY = "your-api-key"
```

### 4. 启动服务

```bash
# stdio 模式（供 MCP 客户端连接）
python -m src.server

# SSE 模式（HTTP 服务）
python -m src.server  # config.yaml 中设置 transport: sse
```

### 5. MCP 客户端配置

在 Claude Desktop 或其他 MCP 客户端的配置文件中添加：

```json
{
  "mcpServers": {
    "code-query": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "d:/desktop/SourceCodeQuery",
      "env": {
        "GITLAB_USERNAME": "your-username",
        "GITLAB_PASSWORD": "your-password",
        "LLM_API_KEY": "your-api-key"
      }
    }
  }
}
```

## MCP 工具列表

| 工具 | 说明 |
|------|------|
| `list_projects` | 列出所有已接入项目 |
| `set_project` | 设置当前操作项目 |
| `get_project_overview` | 获取项目架构概要 |
| `query_code_logic` | **深度解读源码逻辑**（LLM Agent 自主搜索+阅读+追踪调用链） |
| `locate_code` | 定位代码位置（关键词/语义） |
| `search_code` | 语义搜索代码 |
| `search_keyword` | 关键词搜索代码 |
| `read_file` | 读取文件内容（支持行范围） |
| `store_memory` | 存储记忆（按项目隔离） |
| `retrieve_memory` | 检索记忆（按项目隔离） |
| `get_sync_status` | 获取代码同步状态 |

## 项目结构

```
code-query-mcp/
├── config/
│   └── config.yaml                # 配置文件
├── src/
│   ├── server.py                  # MCP Server 入口
│   ├── models/
│   │   └── config.py              # 配置模型（Pydantic）
│   ├── core/
│   │   ├── gitlab_client.py       # GitLab API 客户端（OAuth2 认证）
│   │   ├── code_storage.py        # 按项目隔离的代码存储
│   │   ├── code_index.py          # ChromaDB 语义索引
│   │   ├── call_graph.py          # tree-sitter 调用图（支持 8 种语言）
│   │   ├── code_navigation.py     # 代码导航工具集（供 Agent 调用）
│   │   ├── understanding_engine.py# LLM Agent 推理循环（ReAct 模式）
│   │   ├── project_manager.py     # 多项目管理 + 上下文隔离
│   │   ├── memory_service.py      # openviking 记忆服务
│   │   └── scheduler.py           # 定时源码同步调度器
│   └── tools/
│       ├── project.py             # 项目管理工具
│       ├── query_logic.py         # 源码逻辑查询工具
│       ├── search.py              # 代码搜索工具
│       ├── read_file.py           # 文件读取工具
│       └── memory.py              # 记忆管理工具
├── data/                          # 运行时数据（自动创建）
│   └── projects/
│       └── {project_id}/
│           ├── code/              # 源码缓存
│           └── ...                # 索引等
├── pyproject.toml
└── README.md
```

## 核心工作流

### 源码定时同步

```
调度器触发（Cron）
  → GitLab API：获取增量 commit diff
  → 拉取变更文件 → 写入项目代码存储
  → 更新 ChromaDB 索引 + tree-sitter 调用图
  → LLM 生成变更逻辑摘要 → 存入 openviking（按项目 namespace 隔离）
```

### 用户查询源码逻辑

```
用户: query_code_logic(question="登录流程怎么实现?", project="backend-service")
  → 项目管理器：切换到 backend-service 上下文
  → 记忆服务：从 openviking 检索项目相关记忆
  → LLM Agent 推理循环（ReAct）:
      1. search_code("login") → 定位入口
      2. read_file("AuthController.java") → 阅读入口代码
      3. trace_callees("login") → 追踪调用链
      4. read_file("AuthService.java") → 阅读服务层
      5. retrieve_memory("auth") → 补充历史记忆
      6. 合成完整逻辑解读
  → 记忆服务：存储本次问答摘要
  → 返回: 逻辑解读 + 代码位置（文件+行号+说明）
```

## 支持的编程语言

调用图解析基于 tree-sitter，支持：

| 语言 | 函数定义 | 函数调用 | 类定义 |
|------|---------|---------|--------|
| Python | function_definition | call | class_definition |
| Java | method_declaration | method_invocation | class_declaration |
| JavaScript/TypeScript | function_declaration | call_expression | class_declaration |
| Go | function_declaration | call_expression | type_declaration |
| C/C++ | function_definition | call_expression | struct/class_specifier |
| Rust | function_item | call_expression | struct_item |

## 记忆隔离机制

```
openviking 记忆存储
├── namespace: "proj_backend"        ← 项目 A 的所有记忆
│   ├── project_memory               项目架构概要
│   ├── code_memory                  关键逻辑说明
│   ├── sync_summary                 同步变更摘要
│   └── qa_memory                    历史问答摘要
├── namespace: "proj_frontend"       ← 项目 B 的所有记忆
│   └── ...
└── namespace: "global"              ← 全局记忆（跨项目）
```

## 外部调用示例

### Python 客户端（MCP SDK）

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    # 通过 stdio 连接 MCP Server
    params = StdioServerParameters(
        command="python",
        args=["-m", "src.server"],
        cwd="d:/desktop/SourceCodeQuery",
        env={
            "GITLAB_USERNAME": "your-username",
            "GITLAB_PASSWORD": "your-password",
            "LLM_API_KEY": "your-api-key",
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. 列出所有项目
            result = await session.call_tool("list_projects", {})
            print("项目列表:", result.content[0].text)

            # 2. 设置当前项目
            await session.call_tool("set_project", {"project_id": "backend-service"})

            # 3. 深度解读源码逻辑
            result = await session.call_tool(
                "query_code_logic",
                {"question": "登录流程是怎么实现的？"},
            )
            print("逻辑解读:", result.content[0].text)

            # 4. 定位代码位置
            result = await session.call_tool(
                "locate_code",
                {"keyword": "login", "search_type": "keyword"},
            )
            print("代码位置:", result.content[0].text)

            # 5. 读取具体文件
            result = await session.call_tool(
                "read_file",
                {"file_path": "src/controller/AuthController.java", "start_line": 1, "end_line": 50},
            )
            print("文件内容:", result.content[0].text)

            # 6. 存储记忆
            await session.call_tool(
                "store_memory",
                {"key": "login_flow", "content": "登录使用JWT+Redis会话管理", "tags": ["auth", "login"]},
            )

            # 7. 检索记忆
            result = await session.call_tool(
                "retrieve_memory",
                {"query": "登录认证机制", "tags": ["auth"]},
            )
            print("相关记忆:", result.content[0].text)


asyncio.run(main())
```

### Node.js / TypeScript 客户端

```typescript
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({
  command: "python",
  args: ["-m", "src.server"],
  cwd: "d:/desktop/SourceCodeQuery",
  env: {
    GITLAB_USERNAME: "your-username",
    GITLAB_PASSWORD: "your-password",
    LLM_API_KEY: "your-api-key",
  },
});

const client = new Client(
  { name: "code-query-client", version: "1.0.0" },
  { capabilities: {} }
);

await client.connect(transport);

// 列出所有可用工具
const tools = await client.listTools();
console.log("可用工具:", tools.tools.map(t => t.name));

// 设置当前项目
await client.callTool({
  name: "set_project",
  arguments: { project_id: "backend-service" },
});

// 深度解读源码逻辑
const result = await client.callTool({
  name: "query_code_logic",
  arguments: { question: "用户认证流程是怎么实现的？" },
});
console.log("逻辑解读:", result.content[0].text);

await client.close();
```

### HTTP / SSE 模式调用

当配置 `transport: sse` 时，可通过 HTTP 调用：

```bash
# 启动 SSE 服务（config.yaml 中设置 transport: sse）
python -m src.server

# 使用 curl 测试
curl http://localhost:8080/sse
```

```python
# Python SSE 客户端
import httpx

async def sse_client():
    async with httpx.AsyncClient() as client:
        # 建立 SSE 连接
        async with client.stream("GET", "http://localhost:8080/sse") as response:
            async for line in aiter_lines():
                if line.startswith("data: "):
                    print(line[6:])
```

### 在 IDE 中集成（VS Code / Cursor）

在 IDE 的 MCP 配置文件中添加：

```json
{
  "mcpServers": {
    "code-query": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "d:/desktop/SourceCodeQuery",
      "env": {
        "GITLAB_USERNAME": "your-username",
        "GITLAB_PASSWORD": "your-password",
        "LLM_API_KEY": "your-api-key"
      }
    }
  }
}
```

配置后即可在 AI 对话中直接使用：
- "帮我看看 backend-service 项目的登录流程是怎么实现的"
- "搜索 frontend-app 项目中所有包含 auth 关键词的代码"
- "读取 backend-service 项目的 AuthService.java 文件前 100 行"

### 典型调用流程

```
1. list_projects()              → 查看有哪些项目可用
2. set_project("backend-service") → 设置当前操作项目
3. query_code_logic("登录流程")   → LLM Agent 深度解读
4. read_file("AuthController.java", 1, 50) → 查看具体代码
5. store_memory("login_flow", "使用JWT...") → 存储理解到记忆
6. retrieve_memory("认证机制")     → 下次查询时自动召回
```

## 技术栈

| 组件 | 技术 |
|------|------|
| MCP SDK | Python `mcp` 库 |
| GitLab 集成 | `python-gitlab`（OAuth2 密码授权） |
| LLM 调用 | `openai`（兼容 OpenAI API 的任意服务） |
| 代码解析 | `tree-sitter` + `tree-sitter-languages` |
| 向量索引 | `chromadb`（本地持久化） |
| 调度器 | `apscheduler`（AsyncIOScheduler） |
| 记忆服务 | `httpx`（异步 HTTP 调用 openviking） |
| 配置管理 | `pydantic` + `pyyaml` |
| 日志 | `loguru` |
