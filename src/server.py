"""MCP Server 入口，整合所有模块，注册 MCP 工具，启动服务。

支持 stdio 和 SSE 两种传输模式，启动后自动运行定时代码同步调度器。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from loguru import logger

from src.core.code_navigation import CodeNavigation
from src.core.gitlab_client import GitLabClient
from src.core.memory_service import MemoryService
from src.core.project_manager import ProjectManager
from src.core.scheduler import CodeSyncScheduler
from src.core.understanding_engine import UnderstandingEngine
from src.models.config import LLMConfig, load_config
from src.tools.memory import (
    get_sync_status,
    retrieve_memory,
    store_memory,
)
from src.tools.project import get_project_overview, list_projects, set_project
from src.tools.query_logic import locate_code, query_code_logic
from src.tools.read_file import read_file
from src.tools.search import search_code, search_keyword

# MCP SDK
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    logger.error("未安装 mcp 库，请运行: pip install mcp")
    sys.exit(1)


class UnderstandingEngineManager:
    """理解引擎管理器，按项目动态创建和缓存 UnderstandingEngine 实例。

    每个项目有独立的 CodeNavigation（包含独立的 CodeStorage、CodeIndex、CallGraph），
    因此需要为每个项目创建独立的 UnderstandingEngine。
    在项目代码同步后，需要调用 invalidate() 使缓存失效以重建引擎。
    """

    def __init__(
        self,
        config: LLMConfig,
        project_manager: ProjectManager,
        memory_service: MemoryService,
    ):
        self._config = config
        self._project_manager = project_manager
        self._memory_service = memory_service
        self._engines: dict[str, UnderstandingEngine] = {}

    def _get_engine(self, project_id: str) -> UnderstandingEngine | None:
        """获取或创建指定项目的理解引擎。"""
        if project_id in self._engines:
            return self._engines[project_id]

        ctx = self._project_manager.get_project(project_id)
        if ctx is None:
            logger.error(f"项目不存在，无法创建理解引擎: {project_id}")
            return None

        # 确保调用图已构建
        self._project_manager.ensure_call_graph(project_id)
        ctx = self._project_manager.get_project(project_id)
        if ctx is None or ctx.call_graph is None:
            logger.error(f"调用图构建失败: {project_id}")
            return None

        navigation = CodeNavigation(
            storage=ctx.storage,
            index=ctx.index,
            call_graph=ctx.call_graph,
        )
        engine = UnderstandingEngine(
            config=self._config,
            navigation=navigation,
            memory_service=self._memory_service,
        )
        self._engines[project_id] = engine
        logger.info(f"已创建理解引擎: {project_id}")
        return engine

    def invalidate(self, project_id: str) -> None:
        """使指定项目的引擎缓存失效，下次访问时重建。"""
        if project_id in self._engines:
            del self._engines[project_id]
            logger.debug(f"已使引擎缓存失效: {project_id}")

    async def understand(self, question: str, project_id: str) -> dict:
        """深度解读源码逻辑。"""
        engine = self._get_engine(project_id)
        if engine is None:
            return {
                "explanation": f"错误：无法为项目 {project_id} 创建理解引擎",
                "code_locations": [],
            }
        return await engine.understand(question, project_id)

    async def generate_summary(
        self, file_path: str, content: str, project_id: str
    ) -> str:
        """为单个文件生成逻辑摘要。"""
        engine = self._get_engine(project_id)
        if engine is None:
            return ""
        return await engine.generate_summary(file_path, content, project_id)

    async def generate_change_summary(
        self, changed_files: list[dict], project_id: str
    ) -> str:
        """为一批变更文件生成变更摘要。"""
        engine = self._get_engine(project_id)
        if engine is None:
            return ""
        return await engine.generate_change_summary(changed_files, project_id)


def _build_tool_definitions() -> list[Tool]:
    """构建所有 MCP 工具定义。"""
    return [
        Tool(
            name="list_projects",
            description="列出所有已接入的 GitLab 项目，显示项目 ID、名称、分支和同步状态。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="set_project",
            description="设置当前操作的项目，后续工具调用将默认使用此项目上下文。",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID",
                    },
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="get_project_overview",
            description="获取项目架构概要，包括已索引文件数、同步状态和架构记忆。",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                },
            },
        ),
        Tool(
            name="query_code_logic",
            description=(
                "深度解读指定项目的源码逻辑。LLM Agent 会自主搜索代码、"
                "阅读文件、追踪调用链，给出完整的逻辑解读和涉及的代码位置。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "对源码逻辑的问题，如'登录流程是怎么实现的'",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                },
                "required": ["question"],
            },
        ),
        Tool(
            name="locate_code",
            description="定位具体实现代码位置，支持关键词匹配和语义搜索。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词或查询语句",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                    "search_type": {
                        "type": "string",
                        "enum": ["keyword", "semantic"],
                        "description": "搜索类型：keyword（关键词）或 semantic（语义）",
                        "default": "keyword",
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="search_code",
            description="语义搜索代码，基于自然语言描述查找相关代码片段。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询语句（自然语言描述）",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回结果数量上限",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_keyword",
            description="关键词搜索代码，精确匹配代码内容。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回结果数量上限",
                        "default": 10,
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="read_file",
            description=(
                "读取项目文件内容。可指定行范围读取部分内容（带行号）。"
                "用于查看具体代码实现。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件相对路径（相对于项目代码存储根目录）",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（1-based，含），可选",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（1-based，含），可选",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="store_memory",
            description="存储记忆到当前项目的 namespace，用于保存代码理解、设计决策等。",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "记忆键名",
                    },
                    "content": {
                        "type": "string",
                        "description": "记忆内容",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表，用于后续过滤检索",
                    },
                },
                "required": ["key", "content"],
            },
        ),
        Tool(
            name="retrieve_memory",
            description="检索当前项目的相关记忆，获取之前存储的代码理解、设计决策等。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索查询语句",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签过滤",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回结果数量上限",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_sync_status",
            description="获取指定项目的代码同步状态，包括最后同步的 commit SHA 和时间。",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "项目 ID，未提供时使用当前项目",
                    },
                },
            },
        ),
    ]


class CodeQueryMCPServer:
    """MCP 源码查询服务，整合所有模块。"""

    def __init__(self, config_path: str = "config/config.yaml"):
        """初始化服务，加载配置并创建所有模块实例。

        Args:
            config_path: 配置文件路径
        """
        logger.info("开始初始化 MCP 源码查询服务...")

        # 1. 加载配置
        self._config = load_config(config_path)
        logger.info(
            f"配置加载完成: {len(self._config.gitlab.projects)} 个项目, "
            f"LLM={self._config.llm.model}"
        )

        # 2. 初始化核心模块
        self._memory_service = MemoryService(self._config.memory)
        self._gitlab_client = GitLabClient(self._config.gitlab)
        self._project_manager = ProjectManager(
            self._config, self._memory_service
        )

        # 3. 创建理解引擎管理器
        self._engine_manager = UnderstandingEngineManager(
            config=self._config.llm,
            project_manager=self._project_manager,
            memory_service=self._memory_service,
        )

        # 4. 创建调度器
        self._scheduler = CodeSyncScheduler(
            config=self._config.scheduler,
            gitlab_client=self._gitlab_client,
            project_manager=self._project_manager,
            understanding_engine=self._engine_manager,
            memory_service=self._memory_service,
        )

        # 5. 创建 MCP Server
        self._server = Server("code-query-mcp")
        self._register_handlers()

        logger.info("MCP 源码查询服务初始化完成")

    def _register_handlers(self) -> None:
        """注册 MCP 工具处理 handler。"""

        @self._server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            """返回所有可用工具的定义。"""
            return _build_tool_definitions()

        @self._server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict[str, Any] | None
        ) -> list[TextContent]:
            """处理工具调用请求。"""
            if arguments is None:
                arguments = {}

            logger.info(f"收到工具调用: {name}, 参数: {arguments}")

            try:
                result = await self._dispatch_tool(name, arguments)
            except Exception as e:
                logger.error(f"工具调用异常: {name}, error={e}")
                result = {"error": str(e)}

            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, indent=2),
                )
            ]

    async def _dispatch_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict:
        """根据工具名分发到对应的处理函数。"""
        pm = self._project_manager
        ms = self._memory_service
        em = self._engine_manager

        if name == "list_projects":
            return await list_projects(pm)

        elif name == "set_project":
            project_id = arguments.get("project_id", "")
            return await set_project(pm, project_id)

        elif name == "get_project_overview":
            project_id = arguments.get("project_id")
            if project_id is None:
                project_id = pm.get_current_project_id()
            if project_id is None:
                return {"error": "未指定项目且未设置当前项目"}
            return await get_project_overview(pm, ms, project_id)

        elif name == "query_code_logic":
            question = arguments.get("question", "")
            project_id = arguments.get("project_id")
            return await query_code_logic(pm, em, question, project_id)

        elif name == "locate_code":
            keyword = arguments.get("keyword", "")
            project_id = arguments.get("project_id")
            search_type = arguments.get("search_type", "keyword")
            return await locate_code(pm, keyword, project_id, search_type)

        elif name == "search_code":
            query = arguments.get("query", "")
            project_id = arguments.get("project_id")
            limit = arguments.get("limit", 10)
            return await search_code(pm, query, project_id, limit)

        elif name == "search_keyword":
            keyword = arguments.get("keyword", "")
            project_id = arguments.get("project_id")
            limit = arguments.get("limit", 10)
            return await search_keyword(pm, keyword, project_id, limit)

        elif name == "read_file":
            file_path = arguments.get("file_path", "")
            project_id = arguments.get("project_id")
            start_line = arguments.get("start_line")
            end_line = arguments.get("end_line")
            return await read_file(
                pm, file_path, project_id, start_line, end_line
            )

        elif name == "store_memory":
            key = arguments.get("key", "")
            content = arguments.get("content", "")
            project_id = arguments.get("project_id")
            tags = arguments.get("tags")
            return await store_memory(ms, pm, key, content, project_id, tags)

        elif name == "retrieve_memory":
            query = arguments.get("query", "")
            project_id = arguments.get("project_id")
            tags = arguments.get("tags")
            limit = arguments.get("limit", 5)
            return await retrieve_memory(
                ms, pm, query, project_id, tags, limit
            )

        elif name == "get_sync_status":
            project_id = arguments.get("project_id")
            return await get_sync_status(pm, project_id)

        else:
            return {"error": f"未知工具: {name}"}

    async def run_stdio(self) -> None:
        """以 stdio 模式运行 MCP Server。"""
        logger.info("以 stdio 模式启动 MCP Server")

        # 启动调度器（在后台运行）
        try:
            self._scheduler.start()
            logger.info("代码同步调度器已启动")
        except Exception as e:
            logger.warning(f"调度器启动失败（不影响 MCP 服务）: {e}")

        # 运行 MCP Server
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )

    async def run_sse(self, port: int) -> None:
        """以 SSE 模式运行 MCP Server。"""
        logger.info(f"以 SSE 模式启动 MCP Server, port={port}")

        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await self._server.run(
                    read_stream,
                    write_stream,
                    self._server.create_initialization_options(),
                )

        app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

        # 启动调度器
        try:
            self._scheduler.start()
        except Exception as e:
            logger.warning(f"调度器启动失败: {e}")

        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=port)

    def run(self) -> None:
        """根据配置启动服务。"""
        if self._config.server.transport == "sse":
            asyncio.run(self.run_sse(self._config.server.port))
        else:
            asyncio.run(self.run_stdio())


def main():
    """程序入口。"""
    # 配置日志格式
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | "
        "<level>{level:<7}</level> | "
        "<cyan>{name}</cyan> - <level>{message}</level>",
    )

    # 支持命令行参数指定配置文件
    config_path = "config/config.yaml"
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--config="):
                config_path = arg.split("=", 1)[1]
            elif arg == "--sse":
                # SSE 模式标记，实际端口从配置读取
                pass

    server = CodeQueryMCPServer(config_path)
    server.run()


if __name__ == "__main__":
    main()
