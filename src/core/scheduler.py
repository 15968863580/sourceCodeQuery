"""定时调度器模块，定时从 GitLab 拉取最新源码，更新代码索引、调用图，并触发 LLM 生成变更摘要。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from src.core.gitlab_client import GitLabClient
from src.core.memory_service import MemoryService
from src.core.project_manager import ProjectContext, ProjectManager
from src.core.understanding_engine import UnderstandingEngine
from src.models.config import SchedulerConfig

# 二进制/图片等非文本文件扩展名，同步时排除
_BINARY_EXTS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".jar", ".war", ".class", ".so", ".dll",
    ".exe", ".bin", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".avi", ".mov",
}

# 排除的目录名，这些目录下的文件不处理
_EXCLUDED_DIRS: set[str] = {
    "node_modules", ".git", "__pycache__", ".idea", ".vscode",
    "target", "build", "dist",
}


class CodeSyncScheduler:
    """代码同步调度器。

    定时从 GitLab 拉取最新源码，更新代码索引、调用图，
    并触发 LLM 生成变更摘要，将摘要存入记忆服务。
    使用 APScheduler 的 AsyncIOScheduler 实现定时调度。
    """

    def __init__(
        self,
        config: SchedulerConfig,
        gitlab_client: GitLabClient,
        project_manager: ProjectManager,
        understanding_engine: UnderstandingEngine,
        memory_service: MemoryService,
    ):
        """初始化调度器。

        Args:
            config: 调度器配置
            gitlab_client: GitLab API 客户端
            project_manager: 项目管理器
            understanding_engine: 代码解读引擎
            memory_service: 记忆服务
        """
        self._config = config
        self._gitlab_client = gitlab_client
        self._project_manager = project_manager
        self._understanding_engine = understanding_engine
        self._memory_service = memory_service
        self._scheduler: AsyncIOScheduler | None = None
        logger.debug("初始化代码同步调度器")

    def start(self) -> None:
        """启动调度器，注册定时任务。

        如果 config.full_sync_on_start 为 True，启动后立即执行一次全量同步。
        """
        if self._scheduler is not None:
            logger.warning("调度器已在运行，无需重复启动")
            return

        self._scheduler = AsyncIOScheduler()
        trigger = CronTrigger.from_crontab(self._config.sync_interval_cron)
        self._scheduler.add_job(
            self._scheduled_sync,
            trigger=trigger,
            id="code_sync_all",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(f"调度器已启动，cron={self._config.sync_interval_cron}")

        # 启动后立即执行一次全量同步
        if self._config.full_sync_on_start:
            logger.info("full_sync_on_start=True，启动后立即执行同步")
            self._scheduler.add_job(
                self.sync_all_projects,
                trigger="date",
                id="code_sync_on_start",
                replace_existing=True,
            )

    def stop(self) -> None:
        """停止调度器。"""
        if self._scheduler is None:
            logger.debug("调度器未运行，无需停止")
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("调度器已停止")

    async def _scheduled_sync(self) -> None:
        """定时任务回调，同步所有项目。"""
        logger.info("定时同步任务触发")
        try:
            await self.sync_all_projects()
        except Exception as e:
            logger.exception(f"定时同步任务异常: {e}")

    async def sync_all_projects(self) -> list[dict]:
        """同步所有项目。

        同步过程中单个项目的错误不会中断其他项目的同步。

        Returns:
            各项目同步结果列表
        """
        project_ids = self._project_manager.get_all_project_ids()
        logger.info(f"开始同步所有项目，共 {len(project_ids)} 个")
        results: list[dict] = []
        for project_id in project_ids:
            try:
                result = await self.sync_project(project_id)
                results.append(result)
            except Exception as e:
                logger.exception(f"同步项目 {project_id} 失败: {e}")
                results.append(
                    {
                        "project_id": project_id,
                        "sync_type": "error",
                        "files_synced": 0,
                        "files_deleted": 0,
                        "new_sha": None,
                        "summary": f"同步失败: {e}",
                    }
                )
        logger.info(f"所有项目同步完成，共 {len(results)} 个结果")
        return results

    async def sync_project(self, project_id: str) -> dict:
        """同步单个项目。

        根据上次同步状态决定全量或增量同步：
        - 无上次同步记录 -> 全量同步
        - 上次 SHA 与当前最新 SHA 相同 -> 跳过
        - 否则 -> 增量同步

        Args:
            project_id: 项目 ID

        Returns:
            同步结果字典，包含 project_id、sync_type、files_synced、
            files_deleted、new_sha、summary
        """
        ctx = self._project_manager.get_project(project_id)
        if ctx is None:
            logger.warning(f"项目不存在: {project_id}")
            return {
                "project_id": project_id,
                "sync_type": "error",
                "files_synced": 0,
                "files_deleted": 0,
                "new_sha": None,
                "summary": "项目不存在",
            }

        project_path = ctx.config.get_project_path()
        ref = ctx.config.default_branch

        # 获取最新 commit SHA
        latest_sha = self._gitlab_client.get_last_commit_sha(
            project_path, ref
        )
        if latest_sha is None:
            logger.warning(
                f"无法获取最新 commit SHA，跳过同步: {project_id}"
            )
            return {
                "project_id": project_id,
                "sync_type": "skipped",
                "files_synced": 0,
                "files_deleted": 0,
                "new_sha": None,
                "summary": "无法获取最新 commit SHA",
            }

        # 获取上次同步状态
        sync_status = self._project_manager.get_sync_status(project_id)
        last_sha = sync_status.get("last_sync_sha")

        # 无上次同步记录，执行全量同步
        if last_sha is None:
            logger.info(f"项目 {project_id} 无上次同步记录，执行全量同步")
            return await self._full_sync(project_id, ctx)

        # SHA 未变化，跳过
        if last_sha == latest_sha:
            logger.info(
                f"项目 {project_id} 无代码变更 (sha={latest_sha})，跳过同步"
            )
            return {
                "project_id": project_id,
                "sync_type": "skipped",
                "files_synced": 0,
                "files_deleted": 0,
                "new_sha": latest_sha,
                "summary": "无代码变更",
            }

        # 执行增量同步
        logger.info(
            f"项目 {project_id} 检测到代码变更，"
            f"from={last_sha}, to={latest_sha}，执行增量同步"
        )
        return await self._incremental_sync(
            project_id, ctx, last_sha, latest_sha
        )

    async def _full_sync(
        self, project_id: str, ctx: ProjectContext
    ) -> dict:
        """全量同步：通过 git clone 拉取整个仓库，重建索引和调用图。

        使用 git clone 替代 GitLab API 逐文件拉取，速度快几个数量级。
        克隆后从本地文件系统读取文件内容进行索引。

        Args:
            project_id: 项目 ID
            ctx: 项目上下文

        Returns:
            同步结果字典
        """
        source_url = ctx.config.source_url
        ref = ctx.config.default_branch
        language = ctx.config.language

        logger.info(f"开始全量同步(git clone): {project_id}")

        # 1. 通过 git clone 拉取仓库到 CodeStorage 的代码目录
        clone_success = self._gitlab_client.clone_repository(
            source_url=source_url,
            target_dir=ctx.storage._base_path,
            branch=ref,
        )
        if not clone_success:
            return {
                "project_id": project_id,
                "sync_type": "error",
                "files_synced": 0,
                "files_deleted": 0,
                "new_sha": None,
                "summary": "git clone 失败",
            }

        # 2. 从本地存储列出所有文件
        all_files = ctx.storage.get_all_files()
        # 过滤出代码文件（排除二进制、.git 目录等）
        code_files = [f for f in all_files if self._is_code_file(f)]
        logger.info(
            f"全量同步 {project_id}: 本地文件 {len(all_files)} 个，"
            f"代码文件 {len(code_files)} 个"
        )

        # 3. 清空旧索引，确保干净重建
        ctx.index.clear()

        # 4. 逐个从本地读取文件内容并建立索引
        files_synced = 0
        for file_path in code_files:
            try:
                content = ctx.storage.read_file(file_path)
                ctx.index.index_file(file_path, content, language)
                files_synced += 1
                if files_synced % 50 == 0:
                    logger.info(
                        f"全量同步 {project_id}: 已索引 {files_synced}/{len(code_files)} 个文件"
                    )
            except Exception as e:
                logger.warning(
                    f"全量同步 {project_id}: 索引文件失败 {file_path}: {e}"
                )

        # 5. 构建调用图
        ctx.call_graph = None
        self._project_manager.ensure_call_graph(project_id)

        # 6. 获取最新 commit SHA，更新同步状态
        project_path = ctx.config.get_project_path()
        latest_sha = self._gitlab_client.get_last_commit_sha(
            project_path, ref
        )
        sync_time = datetime.now(timezone.utc).isoformat()
        if latest_sha is not None:
            self._project_manager.update_sync_status(
                project_id, latest_sha, sync_time
            )

        # 7. 生成同步摘要，存入记忆
        summary = (
            f"全量同步完成: 项目={project_id}, "
            f"同步文件数={files_synced}, "
            f"sync_time={sync_time}"
        )
        await self._store_sync_summary(project_id, summary)

        logger.info(
            f"全量同步完成: {project_id}, files_synced={files_synced}"
        )

        return {
            "project_id": project_id,
            "sync_type": "full",
            "files_synced": files_synced,
            "files_deleted": 0,
            "new_sha": latest_sha,
            "summary": summary,
        }

    async def _incremental_sync(
        self,
        project_id: str,
        ctx: ProjectContext,
        from_sha: str,
        to_sha: str,
    ) -> dict:
        """增量同步：只处理变更文件。

        Args:
            project_id: 项目 ID
            ctx: 项目上下文
            from_sha: 起始 commit SHA
            to_sha: 目标 commit SHA

        Returns:
            同步结果字典
        """
        # 如果 from_sha 和 to_sha 相同则跳过
        if from_sha == to_sha:
            logger.info(
                f"增量同步 {project_id}: from_sha==to_sha={from_sha}，跳过"
            )
            return {
                "project_id": project_id,
                "sync_type": "skipped",
                "files_synced": 0,
                "files_deleted": 0,
                "new_sha": to_sha,
                "summary": "无代码变更",
            }

        project_path = ctx.config.get_project_path()
        ref = ctx.config.default_branch
        language = ctx.config.language

        logger.info(
            f"开始增量同步: {project_id}, from={from_sha}, to={to_sha}"
        )

        # 1. 获取 from_sha 到 to_sha 之间的 diff
        diffs = self._gitlab_client.get_commit_diffs_between(
            project_path, from_sha, to_sha
        )

        files_synced = 0
        files_deleted = 0
        changed_files_for_summary: list[dict] = []

        # 2. 对每个变更文件进行处理
        for diff_item in diffs:
            new_path = diff_item.get("new_path", "")
            old_path = diff_item.get("old_path", "")
            is_deleted = diff_item.get("deleted_file", False)
            is_new = diff_item.get("new_file", False)
            diff_content = diff_item.get("diff", "")

            # 删除文件：从 CodeStorage 删除，从 CodeIndex 删除
            if is_deleted:
                # 删除时 new_path 可能为空，使用 old_path
                del_path = new_path or old_path
                if not self._is_code_file(del_path):
                    continue
                try:
                    if ctx.storage.file_exists(del_path):
                        ctx.storage.delete_file(del_path)
                    ctx.index.remove_file(del_path)
                    files_deleted += 1
                    logger.debug(
                        f"增量同步 {project_id}: 删除文件 {del_path}"
                    )
                except Exception as e:
                    logger.warning(
                        f"增量同步 {project_id}: 删除文件失败 {del_path}: {e}"
                    )
                # 记录变更用于生成摘要
                changed_files_for_summary.append(
                    {"path": del_path, "diff": diff_content}
                )
                continue

            # 新文件/修改文件：拉取最新内容，保存到 CodeStorage，更新 CodeIndex
            file_path = new_path or old_path
            if not self._is_code_file(file_path):
                continue
            try:
                content = self._gitlab_client.get_file_content(
                    project_path, file_path, ref=ref
                )
                ctx.storage.save_file(file_path, content)
                ctx.index.index_file(file_path, content, language)
                files_synced += 1
                logger.debug(
                    f"增量同步 {project_id}: 同步文件 {file_path} "
                    f"(new={is_new})"
                )
            except Exception as e:
                logger.warning(
                    f"增量同步 {project_id}: 同步文件失败 {file_path}: {e}"
                )

            # 记录变更用于生成摘要
            changed_files_for_summary.append(
                {"path": file_path, "diff": diff_content}
            )

        # 3. 重建调用图（因为调用关系可能变化）
        ctx.call_graph = None
        self._project_manager.ensure_call_graph(project_id)

        # 4. 更新同步状态
        sync_time = datetime.now(timezone.utc).isoformat()
        self._project_manager.update_sync_status(
            project_id, to_sha, sync_time
        )

        # 5. 调用 understanding_engine 生成变更摘要
        summary = await self._understanding_engine.generate_change_summary(
            changed_files_for_summary, project_id
        )

        # 6. 将摘要存入记忆
        await self._store_sync_summary(project_id, summary)

        logger.info(
            f"增量同步完成: {project_id}, "
            f"files_synced={files_synced}, files_deleted={files_deleted}"
        )

        return {
            "project_id": project_id,
            "sync_type": "incremental",
            "files_synced": files_synced,
            "files_deleted": files_deleted,
            "new_sha": to_sha,
            "summary": summary,
        }

    async def _store_sync_summary(
        self, project_id: str, summary: str
    ) -> None:
        """将同步摘要存入记忆服务。

        记忆类型为 sync_summary，标签包含项目 ID、sync 和当前 UTC 日期。

        Args:
            project_id: 项目 ID
            summary: 同步摘要文本
        """
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        key = f"sync_{date_str}_{now.strftime('%H%M%S')}"
        tags = [project_id, "sync", date_str]
        try:
            await self._memory_service.store(
                project_id,
                key,
                summary,
                memory_type="sync_summary",
                tags=tags,
            )
            logger.info(f"同步摘要已存入记忆: {project_id}, key={key}")
        except Exception as e:
            logger.warning(f"存储同步摘要失败: {project_id}: {e}")

    @staticmethod
    def _is_code_file(file_path: str) -> bool:
        """判断文件是否为需要同步的文本代码文件。

        排除二进制/图片等非文本文件，以及排除位于
        node_modules、.git、__pycache__ 等目录下的文件。

        Args:
            file_path: 文件路径

        Returns:
            是需要同步的代码文件返回 True，否则 False
        """
        if not file_path:
            return False

        # 检查路径中是否包含排除的目录
        path_parts = Path(file_path).parts
        for part in path_parts:
            if part in _EXCLUDED_DIRS:
                return False

        # 检查文件扩展名是否为二进制/图片等
        ext = Path(file_path).suffix.lower()
        if ext in _BINARY_EXTS:
            return False

        return True
