"""源码逻辑查询 MCP 工具，提供深度解读和代码定位能力。

这些工具函数供 server.py 注册为 MCP 工具，对外暴露给 MCP 客户端调用。
project_id 参数可选，未提供时使用 project_manager 的当前项目。
"""

from __future__ import annotations

from loguru import logger

from src.core.project_manager import ProjectManager
from src.core.understanding_engine import UnderstandingEngine


async def query_code_logic(
    project_manager: ProjectManager,
    understanding_engine: UnderstandingEngine,
    question: str,
    project_id: str | None = None,
) -> dict:
    """深度解读指定项目的源码逻辑。

    调用理解引擎基于 ReAct 模式进行多轮工具调用的深度分析，
    返回完整的逻辑解读文本与涉及的代码位置。

    如果未指定 project_id，使用当前已设置的项目。

    Args:
        project_manager: 项目管理器实例
        understanding_engine: 代码理解引擎实例
        question: 用户的代码理解问题
        project_id: 项目 ID，未提供时使用当前项目

    Returns:
        {"explanation": "...", "code_locations": [...], "project_id": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("查询代码逻辑失败：未指定项目且未设置当前项目")
        return {
            "explanation": (
                "错误：未指定 project_id 且当前未设置任何项目，"
                "请先调用 set_project 设置当前项目。"
            ),
            "code_locations": [],
            "project_id": "",
            "error": "no_project",
        }

    logger.info(
        f"查询代码逻辑 project={project_id}, question={question[:80]}"
    )
    result = await understanding_engine.understand(question, project_id)
    return {
        "explanation": result.get("explanation", ""),
        "code_locations": result.get("code_locations", []),
        "project_id": project_id,
    }


async def locate_code(
    project_manager: ProjectManager,
    keyword: str,
    project_id: str | None = None,
    search_type: str = "keyword",
) -> dict:
    """定位具体实现代码位置。

    根据 search_type 选择关键词匹配或语义搜索来定位代码片段。

    Args:
        project_manager: 项目管理器实例
        keyword: 搜索关键词或查询语句
        project_id: 项目 ID，未提供时使用当前项目
        search_type: 搜索类型，"keyword"（关键词）或 "semantic"（语义）

    Returns:
        {"results": [{"file_path","start_line","end_line","content","score"}],
         "project_id": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("定位代码失败：未指定项目且未设置当前项目")
        return {
            "results": [],
            "project_id": "",
            "error": "no_project",
        }

    ctx = project_manager.get_project(project_id)
    if ctx is None:
        logger.warning(f"定位代码失败，项目不存在: {project_id}")
        return {
            "results": [],
            "project_id": project_id,
            "error": f"项目不存在: {project_id}",
        }

    # 根据搜索类型选择搜索方式
    if search_type == "semantic":
        results = ctx.index.search(keyword, limit=10)
    else:
        # 默认使用关键词搜索
        results = ctx.index.search_keyword(keyword, limit=10)

    logger.info(
        f"定位代码 project={project_id}, type={search_type}, "
        f"命中 {len(results)} 条"
    )
    return {
        "results": results,
        "project_id": project_id,
    }
