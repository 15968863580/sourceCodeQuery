"""文件读取 MCP 工具，提供项目文件内容读取能力。

这些工具函数供 server.py 注册为 MCP 工具，对外暴露给 MCP 客户端调用。
project_id 参数可选，未提供时使用 project_manager 的当前项目。
"""

from __future__ import annotations

from loguru import logger

from src.core.project_manager import ProjectManager


async def read_file(
    project_manager: ProjectManager,
    file_path: str,
    project_id: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """读取项目文件内容。

    如果指定了 start_line 和 end_line，只返回该行范围的内容（带行号）；
    否则返回完整文件内容。

    Args:
        project_manager: 项目管理器实例
        file_path: 文件相对路径（相对于项目代码存储根目录）
        project_id: 项目 ID，未提供时使用当前项目
        start_line: 起始行号（1-based，含），可选
        end_line: 结束行号（1-based，含），可选

    Returns:
        {"file_path": "...", "content": "...", "total_lines": N,
         "project_id": "..."}
    """
    # 解析目标 project_id：未提供时回退到当前项目
    if project_id is None:
        project_id = project_manager.get_current_project_id()
    if project_id is None:
        logger.warning("读取文件失败：未指定项目且未设置当前项目")
        return {
            "file_path": file_path,
            "content": "",
            "total_lines": 0,
            "project_id": "",
            "error": "no_project",
        }

    ctx = project_manager.get_project(project_id)
    if ctx is None:
        logger.warning(f"读取文件失败，项目不存在: {project_id}")
        return {
            "file_path": file_path,
            "content": "",
            "total_lines": 0,
            "project_id": project_id,
            "error": f"项目不存在: {project_id}",
        }

    storage = ctx.storage
    try:
        total_lines = storage.get_file_line_count(file_path)
        # 指定了行范围时按范围读取（带行号），否则读取完整文件
        if start_line is not None and end_line is not None:
            content = storage.read_file_range(file_path, start_line, end_line)
        else:
            content = storage.read_file(file_path)
        logger.info(
            f"读取文件 project={project_id}, path={file_path}, "
            f"行范围={start_line}-{end_line}, 总行数={total_lines}"
        )
        return {
            "file_path": file_path,
            "content": content,
            "total_lines": total_lines,
            "project_id": project_id,
        }
    except FileNotFoundError:
        logger.warning(f"文件不存在: {file_path}")
        return {
            "file_path": file_path,
            "content": "",
            "total_lines": 0,
            "project_id": project_id,
            "error": f"文件不存在: {file_path}",
        }
    except Exception as e:
        logger.error(f"读取文件失败 path={file_path}: {e}")
        return {
            "file_path": file_path,
            "content": "",
            "total_lines": 0,
            "project_id": project_id,
            "error": f"读取文件失败: {e}",
        }
