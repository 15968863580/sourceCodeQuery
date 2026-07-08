"""记忆管理 MCP 工具，提供记忆存储、检索和同步状态查询能力。

这些工具函数供 server.py 注册为 MCP 工具，对外暴露给 MCP 客户端调用。
记忆按项目 namespace 隔离，project_id 参数可选，未提供时使用当前项目。
"""

from __future__ import annotations

from loguru import logger

from src.core.memory_service import MemoryService
from src.core.project_manager import ProjectManager


async def store_memory(
    memory_service: MemoryService,
    project_manager: ProjectManager,
    key: str,
    content: str,
    project_id: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """存储记忆到当前项目的 namespace。

    Args:
        memory_service: 记忆服务实例
        project_manager: 项目管理器实例
        key: 记忆键
        content: 记忆内容
        project_id: 项目 ID，未提供时使用当前项目
        tags: 标签列表，用于后续过滤检索

    Returns:
        {"success": bool, "key": str, "project_id": str}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("存储记忆失败：未指定项目且未设置当前项目")
        return {
            "success": False,
            "key": key,
            "project_id": "",
            "error": "no_project",
        }

    result = await memory_service.store(
        project_id,
        key,
        content,
        memory_type="code_memory",
        tags=tags,
    )
    success = result.get("success", True)
    logger.info(
        f"存储记忆 project={project_id}, key={key}, success={success}"
    )
    return {
        "success": success,
        "key": key,
        "project_id": project_id,
    }


async def retrieve_memory(
    memory_service: MemoryService,
    project_manager: ProjectManager,
    query: str,
    project_id: str | None = None,
    tags: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """检索当前项目的相关记忆。

    Args:
        memory_service: 记忆服务实例
        project_manager: 项目管理器实例
        query: 检索查询语句
        project_id: 项目 ID，未提供时使用当前项目
        tags: 标签过滤
        limit: 返回结果数量上限，默认 5

    Returns:
        {"results": [...], "project_id": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("检索记忆失败：未指定项目且未设置当前项目")
        return {"results": [], "project_id": "", "error": "no_project"}

    results = await memory_service.retrieve(
        project_id, query, tags=tags, limit=limit
    )
    logger.info(
        f"检索记忆 project={project_id}, query={query[:50]}, "
        f"命中 {len(results)} 条"
    )
    return {"results": results, "project_id": project_id}


async def get_sync_status(
    project_manager: ProjectManager,
    project_id: str | None = None,
) -> dict:
    """获取指定项目的同步状态。

    Args:
        project_manager: 项目管理器实例
        project_id: 项目 ID，未提供时使用当前项目

    Returns:
        {"project_id": "...", "last_sync_sha": "...",
         "last_sync_time": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("获取同步状态失败：未指定项目且未设置当前项目")
        return {
            "project_id": "",
            "last_sync_sha": None,
            "last_sync_time": None,
            "error": "no_project",
        }

    status = project_manager.get_sync_status(project_id)
    logger.info(
        f"获取同步状态 project={project_id}, "
        f"sha={status.get('last_sync_sha')}"
    )
    return {
        "project_id": project_id,
        "last_sync_sha": status.get("last_sync_sha"),
        "last_sync_time": status.get("last_sync_time"),
    }
