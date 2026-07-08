"""代码搜索 MCP 工具，提供语义搜索和关键词搜索能力。

这些工具函数供 server.py 注册为 MCP 工具，对外暴露给 MCP 客户端调用。
project_id 参数可选，未提供时使用 project_manager 的当前项目。
"""

from __future__ import annotations

from loguru import logger

from src.core.project_manager import ProjectManager


async def search_code(
    project_manager: ProjectManager,
    query: str,
    project_id: str | None = None,
    limit: int = 10,
) -> dict:
    """语义搜索代码。

    基于自然语言描述查找相关代码片段，利用向量相似度匹配。

    Args:
        project_manager: 项目管理器实例
        query: 搜索查询语句（自然语言描述）
        project_id: 项目 ID，未提供时使用当前项目
        limit: 返回结果数量上限，默认 10

    Returns:
        {"results": [...], "project_id": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("语义搜索失败：未指定项目且未设置当前项目")
        return {"results": [], "project_id": "", "error": "no_project"}

    ctx = project_manager.get_project(project_id)
    if ctx is None:
        logger.warning(f"语义搜索失败，项目不存在: {project_id}")
        return {
            "results": [],
            "project_id": project_id,
            "error": f"项目不存在: {project_id}",
        }

    results = ctx.index.search(query, limit=limit)
    logger.info(
        f"语义搜索 project={project_id}, query={query[:50]}, "
        f"命中 {len(results)} 条"
    )
    return {"results": results, "project_id": project_id}


async def search_keyword(
    project_manager: ProjectManager,
    keyword: str,
    project_id: str | None = None,
    limit: int = 10,
) -> dict:
    """关键词搜索代码。

    基于关键词精确匹配代码内容。

    Args:
        project_manager: 项目管理器实例
        keyword: 搜索关键词
        project_id: 项目 ID，未提供时使用当前项目
        limit: 返回结果数量上限，默认 10

    Returns:
        {"results": [...], "project_id": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("关键词搜索失败：未指定项目且未设置当前项目")
        return {"results": [], "project_id": "", "error": "no_project"}

    ctx = project_manager.get_project(project_id)
    if ctx is None:
        logger.warning(f"关键词搜索失败，项目不存在: {project_id}")
        return {
            "results": [],
            "project_id": project_id,
            "error": f"项目不存在: {project_id}",
        }

    results = ctx.index.search_keyword(keyword, limit=limit)
    logger.info(
        f"关键词搜索 project={project_id}, keyword={keyword}, "
        f"命中 {len(results)} 条"
    )
    return {"results": results, "project_id": project_id}
