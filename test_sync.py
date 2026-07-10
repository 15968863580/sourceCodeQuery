"""测试脚本：初始化服务并拉取 GitLab 源码。

用法: python test_sync.py
"""

import asyncio
import json
import sys
from pathlib import Path

from loguru import logger

from src.core.gitlab_client import GitLabClient
from src.core.memory_service import MemoryService
from src.core.project_manager import ProjectManager
from src.core.scheduler import CodeSyncScheduler
from src.models.config import load_config

# 配置日志
logger.remove()
logger.add(
    sys.stderr,
    level="DEBUG",
    format="<green>{time:HH:mm:ss}</green> | "
    "<level>{level:<7}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>",
)


class MockUnderstandingEngine:
    """模拟理解引擎，全量同步时不调用 LLM，用空实现代替。"""

    async def generate_change_summary(self, changed_files, project_id):
        return f"变更摘要（mock）：共 {len(changed_files)} 个文件变更"

    async def understand(self, question, project_id):
        return {"explanation": "mock", "code_locations": []}


async def main():
    logger.info("=" * 60)
    logger.info("开始测试 GitLab 源码拉取")
    logger.info("=" * 60)

    # 1. 加载配置
    config = load_config("config/config.yaml")
    logger.info(
        f"配置加载完成: {len(config.gitlab.projects)} 个项目, "
        f"GitLab={config.gitlab.url}"
    )
    for p in config.gitlab.projects:
        logger.info(
            f"  项目: id={p.id}, name={p.name}, "
            f"branch={p.default_branch}, lang={p.language}"
        )

    # 2. 初始化核心模块
    logger.info("-" * 40)
    logger.info("初始化核心模块...")

    memory_service = MemoryService(config.memory)
    logger.info(f"记忆服务: endpoint={config.memory.openviking_endpoint}")

    gitlab_client = GitLabClient(config.gitlab)
    logger.info("GitLab 客户端: 认证成功")

    project_manager = ProjectManager(config, memory_service)
    logger.info(f"项目管理器: {len(project_manager.get_all_project_ids())} 个项目已加载")

    # 3. 创建调度器（使用 mock 引擎）
    scheduler = CodeSyncScheduler(
        config=config.scheduler,
        gitlab_client=gitlab_client,
        project_manager=project_manager,
        understanding_engine=MockUnderstandingEngine(),
        memory_service=memory_service,
    )
    logger.info("调度器初始化完成")

    # 4. 执行全量同步
    logger.info("=" * 40)
    logger.info("开始拉取源码（全量同步）...")
    logger.info("=" * 40)

    results = await scheduler.sync_all_projects()

    # 5. 输出结果
    logger.info("=" * 40)
    logger.info("源码拉取完成！结果汇总：")
    logger.info("=" * 40)
    for result in results:
        logger.info(json.dumps(result, ensure_ascii=False, indent=2))

    # 6. 验证：列出每个项目同步的文件
    logger.info("=" * 40)
    logger.info("验证：各项目代码存储目录")
    logger.info("=" * 40)
    for project_id in project_manager.get_all_project_ids():
        ctx = project_manager.get_project(project_id)
        if ctx is None:
            continue
        code_dir = Path(f"data/projects/{project_id}/code")
        if code_dir.exists():
            file_count = sum(1 for _ in code_dir.rglob("*") if _.is_file())
            total_size = sum(f.stat().st_size for f in code_dir.rglob("*") if f.is_file())
            logger.info(
                f"项目 {project_id}: {file_count} 个文件, "
                f"总大小 {total_size / 1024:.1f} KB"
            )
            # 列出前 10 个文件作为示例
            files = sorted(code_dir.rglob("*"))
            for f in files[:10]:
                if f.is_file():
                    rel = f.relative_to(code_dir)
                    logger.info(f"  {rel}")
            if len(files) > 10:
                logger.info(f"  ... 还有 {len(files) - 10} 个文件")
        else:
            logger.warning(f"项目 {project_id}: 代码目录不存在")

    logger.info("测试完成！")


if __name__ == "__main__":
    asyncio.run(main())
