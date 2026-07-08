"""项目管理器模块，管理多个 GitLab 项目的上下文隔离。

为每个项目维护独立的 CodeStorage、CodeIndex、CallGraph 实例，
MemoryService 则在项目间共享，通过 project_id 参数实现隔离。
"""

from __future__ import annotations

from loguru import logger

from src.core.call_graph import CallGraph
from src.core.code_index import CodeIndex
from src.core.code_storage import CodeStorage
from src.core.memory_service import MemoryService
from src.models.config import AppConfig, ProjectConfig


class ProjectContext:
    """单个项目的上下文，包含该项目所有独立的资源实例。"""

    def __init__(
        self,
        config: ProjectConfig,
        storage: CodeStorage,
        index: CodeIndex,
        call_graph: CallGraph | None = None,
    ):
        """初始化项目上下文。

        Args:
            config: 项目配置
            storage: 项目独立的代码存储实例
            index: 项目独立的代码索引实例
            call_graph: 项目独立的调用图实例；延迟初始化时为 None，
                通常在代码同步完成后才构建
        """
        self.config = config
        self.storage = storage
        self.index = index
        self.call_graph = call_graph
        # 同步状态
        self.last_sync_sha: str | None = None
        self.last_sync_time: str | None = None


class ProjectManager:
    """多项目管理器，管理各项目的上下文隔离。"""

    def __init__(self, app_config: AppConfig, memory_service: MemoryService):
        """初始化项目管理器，为每个已配置项目创建独立的上下文。

        CodeStorage 与 CodeIndex 在此时即创建；CallGraph 延迟初始化，
        因为 build_from_storage 需要先有代码，通常在首次同步后才构建。

        Args:
            app_config: 应用配置
            memory_service: 共享的记忆服务实例（按 project_id 隔离）
        """
        self._app_config = app_config
        self._memory_service = memory_service
        self._contexts: dict[str, ProjectContext] = {}
        self._current_project_id: str | None = None

        # 为每个已配置项目创建独立的 ProjectContext
        for project_config in app_config.gitlab.projects:
            project_id = project_config.id
            try:
                # 项目数据目录为 data_dir/projects/<project_id>，
                # CodeStorage 会在 base_data_dir 上追加 <project_id>/code，
                # 故传入项目数据目录的父目录作为 base
                base_data_dir = app_config.get_project_data_dir(
                    project_id
                ).parent
                storage = CodeStorage(
                    base_data_dir=base_data_dir,
                    project_id=project_id,
                )
                index = CodeIndex(
                    index_config=app_config.index,
                    embedding_config=app_config.embedding,
                    project_id=project_id,
                )
                # CallGraph 延迟初始化，暂置为 None
                context = ProjectContext(
                    config=project_config,
                    storage=storage,
                    index=index,
                    call_graph=None,
                )
                self._contexts[project_id] = context
                logger.info(
                    f"已加载项目上下文: {project_id} "
                    f"({project_config.name})"
                )
            except Exception as e:
                logger.error(
                    f"初始化项目上下文失败: {project_id}, 错误: {e}"
                )

        logger.info(
            f"项目管理器初始化完成，共加载 {len(self._contexts)} 个项目"
        )

    def get_project(self, project_id: str) -> ProjectContext | None:
        """获取项目上下文。

        Args:
            project_id: 项目 ID

        Returns:
            项目上下文，若不存在返回 None
        """
        return self._contexts.get(project_id)

    def list_projects(self) -> list[ProjectConfig]:
        """列出所有已配置项目。

        Returns:
            项目配置列表，按配置文件中的顺序
        """
        return list(self._app_config.gitlab.projects)

    def get_all_project_ids(self) -> list[str]:
        """获取所有项目 ID 列表。

        Returns:
            项目 ID 列表，按配置文件中的顺序
        """
        return [p.id for p in self._app_config.gitlab.projects]

    def set_current_project(self, project_id: str) -> bool:
        """设置当前操作的项目。

        Args:
            project_id: 项目 ID

        Returns:
            设置成功返回 True，项目不存在或未加载返回 False
        """
        if project_id not in self._contexts:
            logger.warning(
                f"设置当前项目失败，项目不存在或未加载: {project_id}"
            )
            return False
        self._current_project_id = project_id
        logger.info(f"当前项目已切换为: {project_id}")
        return True

    def get_current_project(self) -> ProjectContext | None:
        """获取当前操作的项目上下文。

        Returns:
            当前项目上下文，未设置时返回 None
        """
        if self._current_project_id is None:
            return None
        return self._contexts.get(self._current_project_id)

    def get_current_project_id(self) -> str | None:
        """获取当前操作的项目 ID。

        Returns:
            当前项目 ID，未设置时返回 None
        """
        return self._current_project_id

    def update_sync_status(
        self, project_id: str, sha: str, sync_time: str
    ) -> None:
        """更新项目同步状态。

        Args:
            project_id: 项目 ID
            sha: 最新同步的 commit SHA
            sync_time: 同步时间字符串
        """
        ctx = self._contexts.get(project_id)
        if ctx is None:
            logger.warning(f"更新同步状态失败，项目不存在: {project_id}")
            return
        ctx.last_sync_sha = sha
        ctx.last_sync_time = sync_time
        logger.debug(
            f"更新同步状态: {project_id}, sha={sha}, time={sync_time}"
        )

    def get_sync_status(self, project_id: str) -> dict:
        """获取项目同步状态。

        Args:
            project_id: 项目 ID

        Returns:
            包含 last_sync_sha 和 last_sync_time 的字典；
            项目不存在时两者均为 None
        """
        ctx = self._contexts.get(project_id)
        if ctx is None:
            logger.warning(f"获取同步状态失败，项目不存在: {project_id}")
            return {"last_sync_sha": None, "last_sync_time": None}
        return {
            "last_sync_sha": ctx.last_sync_sha,
            "last_sync_time": ctx.last_sync_time,
        }

    def ensure_call_graph(self, project_id: str) -> CallGraph | None:
        """延迟初始化并构建项目的调用图。

        由于 build_from_storage 需要先有代码，CallGraph 在首次需要时
        （通常在代码同步完成后）才创建。若已构建则直接返回，不重复构建。

        Args:
            project_id: 项目 ID

        Returns:
            项目的调用图实例，项目不存在时返回 None
        """
        ctx = self._contexts.get(project_id)
        if ctx is None:
            logger.warning(f"构建调用图失败，项目不存在: {project_id}")
            return None
        if ctx.call_graph is not None:
            return ctx.call_graph
        logger.info(f"延迟初始化调用图: {project_id}")
        call_graph = CallGraph(project_id)
        call_graph.build_from_storage(ctx.storage)
        ctx.call_graph = call_graph
        return ctx.call_graph
