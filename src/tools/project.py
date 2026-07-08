"""项目管理 MCP 工具，提供项目列表、当前项目设置、项目架构概要查询能力。

这些工具函数供 server.py 注册为 MCP 工具，对外暴露给 MCP 客户端调用。
所有函数均为 async，返回包含操作结果与 project_id 的字典，便于上下文追踪。
"""

from __future__ import annotations

from loguru import logger

from src.core.memory_service import MemoryService
from src.core.project_manager import ProjectManager


async def list_projects(project_manager: ProjectManager) -> dict:
    """列出所有已接入项目。

    Args:
        project_manager: 项目管理器实例

    Returns:
        {"projects": [{"id","name","default_branch","language","last_sync_time"}]}
    """
    configs = project_manager.list_projects()
    projects: list[dict] = []
    for config in configs:
        sync_status = project_manager.get_sync_status(config.id)
        projects.append(
            {
                "id": config.id,
                "name": config.name,
                "default_branch": config.default_branch,
                "language": config.language,
                "last_sync_time": sync_status.get("last_sync_time"),
            }
        )
    logger.info(f"列出项目，共 {len(projects)} 个")
    return {"projects": projects}


async def set_project(
    project_manager: ProjectManager, project_id: str
) -> dict:
    """设置当前操作项目。

    Args:
        project_manager: 项目管理器实例
        project_id: 要设置为当前项目的项目 ID

    Returns:
        {"success": bool, "project_id": str, "project_name": str}
    """
    success = project_manager.set_current_project(project_id)
    if not success:
        logger.warning(f"设置当前项目失败: {project_id}")
        return {
            "success": False,
            "project_id": project_id,
            "project_name": "",
            "error": f"项目不存在或未加载: {project_id}",
        }

    ctx = project_manager.get_project(project_id)
    project_name = ctx.config.name if ctx else ""
    logger.info(f"已设置当前项目: {project_id} ({project_name})")
    return {
        "success": True,
        "project_id": project_id,
        "project_name": project_name,
    }


async def get_project_overview(
    project_manager: ProjectManager,
    memory_service: MemoryService,
    project_id: str,
) -> dict:
    """获取项目架构概要。

    从记忆服务中检索 project_memory 类型的记忆作为架构概要，
    并结合索引统计与同步状态返回完整的项目概要信息。

    Args:
        project_manager: 项目管理器实例
        memory_service: 记忆服务实例
        project_id: 项目 ID

    Returns:
        {"project_id", "project_name", "overview": "...",
         "file_count": N, "sync_status": {...}}
    """
    ctx = project_manager.get_project(project_id)
    if ctx is None:
        logger.warning(f"获取项目概要失败，项目不存在: {project_id}")
        return {
            "project_id": project_id,
            "project_name": "",
            "overview": "",
            "file_count": 0,
            "sync_status": {"last_sync_sha": None, "last_sync_time": None},
            "error": f"项目不存在: {project_id}",
        }

    # 从记忆中检索 project_memory 类型的记忆作为架构概要
    memories = await memory_service.list_memories(
        project_id, memory_type="project_memory", limit=1
    )
    overview = memories[0].get("content", "") if memories else ""

    # 通过索引统计获取已索引文件数
    stats = ctx.index.get_stats()
    file_count = stats.get("total_files", 0)

    sync_status = project_manager.get_sync_status(project_id)

    logger.info(
        f"获取项目概要: {project_id}, 文件数={file_count}, "
        f"有概要记忆={'是' if overview else '否'}"
    )
    return {
        "project_id": project_id,
        "project_name": ctx.config.name,
        "overview": overview,
        "file_count": file_count,
        "sync_status": sync_status,
    }
