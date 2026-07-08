"""记忆服务模块，通过 HTTP 调用 openviking 服务实现记忆的读写，按项目 namespace 隔离。"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from src.models.config import MemoryConfig


class MemoryService:
    """记忆服务，封装 openviking 的 HTTP API 调用。

    所有方法在 openviking 不可用时优雅降级：记录警告日志但不抛出异常，
    store 类方法返回包含错误信息的字典，retrieve/list 类方法返回空列表。
    """

    # 默认请求超时时间（秒）
    DEFAULT_TIMEOUT: float = 30.0

    def __init__(self, config: MemoryConfig) -> None:
        """初始化记忆服务。

        Args:
            config: openviking 记忆服务配置
        """
        self._config = config
        # 去除 endpoint 末尾可能的斜杠，避免拼接出双斜杠
        self._endpoint = config.openviking_endpoint.rstrip("/")
        self._global_namespace = config.global_namespace

    def _get_namespace(self, project_id: str) -> str:
        """获取项目的记忆 namespace。

        优先从 config.projects[project_id].namespace 获取，
        如果未配置则回退到 f"proj_{project_id}"。

        Args:
            project_id: 项目 ID

        Returns:
            项目的记忆 namespace
        """
        proj_mem = self._config.projects.get(project_id)
        if proj_mem:
            return proj_mem.namespace
        return f"proj_{project_id}"

    async def store(
        self,
        project_id: str,
        key: str,
        content: str,
        memory_type: str = "code_memory",
        tags: list[str] | None = None,
    ) -> dict:
        """在指定项目的 namespace 下存储记忆。

        Args:
            project_id: 项目 ID
            key: 记忆键
            content: 记忆内容
            memory_type: 记忆类型，可选值:
                "project_memory" | "code_memory" | "sync_summary" | "qa_memory"
            tags: 标签列表，用于后续过滤检索

        Returns:
            openviking 的响应字典；服务不可用时返回错误信息字典
        """
        namespace = self._get_namespace(project_id)
        payload: dict[str, Any] = {
            "namespace": namespace,
            "key": key,
            "content": content,
            "type": memory_type,
            "tags": tags or [],
        }
        return await self._post("/api/v1/memory/store", payload)

    async def retrieve(
        self,
        project_id: str,
        query: str,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """从指定项目 namespace 检索相关记忆。

        Args:
            project_id: 项目 ID
            query: 检索查询语句
            tags: 标签过滤
            limit: 返回结果数量上限

        Returns:
            记忆列表，每项形如
            {"key": "...", "content": "...", "score": 0.85, "tags": [...], "type": "..."}；
            服务不可用时返回空列表
        """
        namespace = self._get_namespace(project_id)
        payload: dict[str, Any] = {
            "namespace": namespace,
            "query": query,
            "tags": tags or [],
            "limit": limit,
        }
        data = await self._post("/api/v1/memory/retrieve", payload)
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    async def store_global(
        self,
        key: str,
        content: str,
        tags: list[str] | None = None,
    ) -> dict:
        """存储全局记忆（跨项目共享）。

        使用 config.global_namespace 作为 namespace。
        全局记忆默认类型为 "project_memory"。

        Args:
            key: 记忆键
            content: 记忆内容
            tags: 标签列表

        Returns:
            openviking 的响应字典；服务不可用时返回错误信息字典
        """
        payload: dict[str, Any] = {
            "namespace": self._global_namespace,
            "key": key,
            "content": content,
            "type": "project_memory",
            "tags": tags or [],
        }
        return await self._post("/api/v1/memory/store", payload)

    async def retrieve_global(
        self,
        query: str,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """检索全局记忆。

        Args:
            query: 检索查询语句
            tags: 标签过滤
            limit: 返回结果数量上限

        Returns:
            记忆列表；服务不可用时返回空列表
        """
        payload: dict[str, Any] = {
            "namespace": self._global_namespace,
            "query": query,
            "tags": tags or [],
            "limit": limit,
        }
        data = await self._post("/api/v1/memory/retrieve", payload)
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    async def delete(self, project_id: str, key: str) -> bool:
        """删除指定项目中的某条记忆。

        Args:
            project_id: 项目 ID
            key: 记忆键

        Returns:
            删除成功返回 True，失败或服务不可用返回 False
        """
        namespace = self._get_namespace(project_id)
        url = f"{self._endpoint}/api/v1/memory/{namespace}/{key}"
        try:
            async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
                resp = await client.delete(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return bool(data.get("success", False))
                logger.warning(
                    "删除记忆失败 namespace={} key={} status={}",
                    namespace,
                    key,
                    resp.status_code,
                )
                return False
        except Exception as e:
            logger.warning(
                "openviking 服务不可用，删除记忆失败 namespace={} key={}: {}",
                namespace,
                key,
                e,
            )
            return False

    async def list_memories(
        self,
        project_id: str,
        memory_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """列出指定项目的记忆。

        Args:
            project_id: 项目 ID
            memory_type: 可选，按记忆类型过滤
            limit: 返回结果数量上限

        Returns:
            记忆列表；服务不可用时返回空列表
        """
        namespace = self._get_namespace(project_id)
        params: dict[str, Any] = {"namespace": namespace, "limit": limit}
        if memory_type is not None:
            params["type"] = memory_type
        url = f"{self._endpoint}/api/v1/memory/list"
        try:
            async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("results", [])
                logger.warning(
                    "列出记忆失败 namespace={} status={}",
                    namespace,
                    resp.status_code,
                )
                return []
        except Exception as e:
            logger.warning(
                "openviking 服务不可用，列出记忆失败 namespace={}: {}",
                namespace,
                e,
            )
            return []

    async def _post(self, path: str, payload: dict[str, Any]) -> dict:
        """发送 POST 请求到 openviking，统一处理超时与优雅降级。

        Args:
            path: API 路径，以 "/" 开头
            payload: 请求体

        Returns:
            响应 JSON 字典；服务不可用或出错时返回包含错误信息的字典
        """
        url = f"{self._endpoint}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    "openviking 请求失败 path={} status={} body={}",
                    path,
                    resp.status_code,
                    resp.text,
                )
                return {
                    "success": False,
                    "error": f"HTTP {resp.status_code}",
                    "key": payload.get("key", ""),
                }
        except Exception as e:
            logger.warning(
                "openviking 服务不可用 path={}: {}",
                path,
                e,
            )
            return {
                "success": False,
                "error": str(e),
                "key": payload.get("key", ""),
            }
