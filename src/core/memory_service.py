"""记忆服务模块，通过 HTTP 调用 openviking 服务实现记忆的读写，按项目 URI 路径隔离。

openviking API 概要:
  - 认证: x-api-key (完整密钥) + X-OpenViking-User (用户 ID)
  - 写入: POST /api/v1/content/write  {uri, content, mode:"create"|"replace"}
  - 读取: GET  /api/v1/content/read?uri=...
  - 搜索: POST /api/v1/search/search  {query, limit, target_uri?}
  - URI 格式: viking://user/{user}/memories/code_query/{project_id}/{key}.md
"""

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

    # URI 前缀模板（.md 后缀由 _build_uri 添加）
    URI_TEMPLATE = "viking://user/{user}/memories/code_query/{project_id}/{key}"

    def __init__(self, config: MemoryConfig) -> None:
        """初始化记忆服务。

        Args:
            config: openviking 记忆服务配置
        """
        self._config = config
        self._endpoint = config.openviking_endpoint.rstrip("/")
        self._global_namespace = config.global_namespace
        self._user = config.openviking_user or "default"
        # 构建认证请求头（openviking 使用 x-api-key + X-OpenViking-User）
        self._headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if config.openviking_api_key:
            self._headers["x-api-key"] = config.openviking_api_key
        if config.openviking_user:
            self._headers["X-OpenViking-User"] = config.openviking_user

    def _build_uri(self, project_id: str, key: str) -> str:
        """构建记忆的 viking:// URI。

        URI 路径格式: viking://user/{user}/memories/code_query/{project_id}/{key}.md
        不同项目的记忆存储在不同路径下，实现隔离。

        Args:
            project_id: 项目 ID
            key: 记忆键

        Returns:
            完整的 viking:// URI
        """
        # 清理 key 中的非法字符，用作文件名
        safe_key = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        # 去掉可能的 .md 后缀，统一添加
        if safe_key.endswith(".md"):
            safe_key = safe_key[:-3]
        safe_key += ".md"
        return self.URI_TEMPLATE.format(
            user=self._user,
            project_id=project_id,
            key=safe_key,
        )

    def _build_project_uri_prefix(self, project_id: str) -> str:
        """构建项目记忆的 URI 前缀，用于搜索时限定范围。

        Args:
            project_id: 项目 ID

        Returns:
            URI 前缀，如 viking://user/11277/memories/code_query/eip
        """
        return f"viking://user/{self._user}/memories/code_query/{project_id}"

    async def store(
        self,
        project_id: str,
        key: str,
        content: str,
        memory_type: str = "code_memory",
        tags: list[str] | None = None,
    ) -> dict:
        """在指定项目的路径下存储记忆。

        使用 openviking content/write API，mode="create" 创建新记忆，
        mode="replace" 更新已有记忆。

        Args:
            project_id: 项目 ID
            key: 记忆键（用作 URI 文件名）
            content: 记忆内容
            memory_type: 记忆类型（写入内容头部作为元信息）
            tags: 标签列表（写入内容头部作为元信息）

        Returns:
            openviking 的响应字典；服务不可用时返回错误信息字典
        """
        uri = self._build_uri(project_id, key)

        # 将 memory_type 和 tags 作为元信息写入内容头部
        meta_lines = [f"<!-- type: {memory_type} -->"]
        if tags:
            meta_lines.append(f"<!-- tags: {', '.join(tags)} -->")
        meta_lines.append(f"<!-- project: {project_id} -->")
        meta_lines.append("")
        full_content = "\n".join(meta_lines) + content

        # 先尝试 create 模式，如果文件已存在则用 replace
        for mode in ("create", "replace"):
            payload: dict[str, Any] = {
                "uri": uri,
                "content": full_content,
                "mode": mode,
            }
            data = await self._post("/api/v1/content/write", payload)
            if isinstance(data, dict) and data.get("status") == "ok":
                logger.debug(
                    "记忆写入成功 project={} key={} mode={}",
                    project_id,
                    key,
                    mode,
                )
                return data
            # 如果 create 失败因为文件已存在，自动切换到 replace
            if mode == "create" and data.get("error", "").endswith("already exists"):
                continue
            # 其他错误直接返回
            if mode == "create" and data.get("status") != "ok":
                # 可能是 404 或其他错误，尝试 replace
                continue

        return data  # type: ignore[possibly-undefined]

    async def retrieve(
        self,
        project_id: str,
        query: str,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """从指定项目路径下检索相关记忆。

        使用 openviking search/search API，通过 target_uri 限定搜索范围
        到当前项目的记忆路径。

        Args:
            project_id: 项目 ID
            query: 检索查询语句
            tags: 标签过滤（追加到查询语句中）
            limit: 返回结果数量上限

        Returns:
            记忆列表，每项形如
            {"uri": "...", "content": "...", "abstract": "...", "score": 0.85}；
            服务不可用时返回空列表
        """
        uri_prefix = self._build_project_uri_prefix(project_id)
        # 追加 tags 到查询
        full_query = query
        if tags:
            full_query = f"{query} {' '.join(tags)}"

        payload: dict[str, Any] = {
            "query": full_query,
            "limit": limit,
            "target_uri": uri_prefix,
        }
        data = await self._post("/api/v1/search/search", payload)
        if isinstance(data, dict) and data.get("status") == "ok":
            memories = data.get("result", {}).get("memories", [])
            # 读取每条记忆的完整内容
            results: list[dict] = []
            for mem in memories:
                item = {
                    "uri": mem.get("uri", ""),
                    "abstract": mem.get("abstract", ""),
                    "score": mem.get("score", 0.0),
                    "content": mem.get("abstract", ""),  # abstract 作为内容摘要
                }
                results.append(item)
            return results
        return []

    async def store_global(
        self,
        key: str,
        content: str,
        tags: list[str] | None = None,
    ) -> dict:
        """存储全局记忆（跨项目共享）。

        使用 global_namespace 作为项目路径。

        Args:
            key: 记忆键
            content: 记忆内容
            tags: 标签列表

        Returns:
            openviking 的响应字典；服务不可用时返回错误信息字典
        """
        return await self.store(
            project_id=self._global_namespace,
            key=key,
            content=content,
            memory_type="project_memory",
            tags=tags,
        )

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
        return await self.retrieve(
            project_id=self._global_namespace,
            query=query,
            tags=tags,
            limit=limit,
        )

    async def delete(self, project_id: str, key: str) -> bool:
        """删除指定项目中的某条记忆。

        Args:
            project_id: 项目 ID
            key: 记忆键

        Returns:
            删除成功返回 True，失败或服务不可用返回 False
        """
        uri = self._build_uri(project_id, key)
        url = f"{self._endpoint}/api/v1/content"
        try:
            async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
                resp = await client.delete(url, params={"uri": uri}, headers=self._headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("status") == "ok"
                logger.warning(
                    "删除记忆失败 project={} key={} status={}",
                    project_id,
                    key,
                    resp.status_code,
                )
                return False
        except Exception as e:
            logger.warning(
                "openviking 服务不可用，删除记忆失败 project={} key={}: {}",
                project_id,
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

        使用 search/search API 搜索项目路径下所有记忆。

        Args:
            project_id: 项目 ID
            memory_type: 可选，按记忆类型过滤
            limit: 返回结果数量上限

        Returns:
            记忆列表；服务不可用时返回空列表
        """
        uri_prefix = self._build_project_uri_prefix(project_id)
        query = memory_type or "all"
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "target_uri": uri_prefix,
        }
        data = await self._post("/api/v1/search/search", payload)
        if isinstance(data, dict) and data.get("status") == "ok":
            memories = data.get("result", {}).get("memories", [])
            return [
                {
                    "uri": m.get("uri", ""),
                    "abstract": m.get("abstract", ""),
                    "score": m.get("score", 0.0),
                }
                for m in memories
            ]
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
                resp = await client.post(url, json=payload, headers=self._headers)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    "openviking 请求失败 path={} status={} body={}",
                    path,
                    resp.status_code,
                    resp.text[:300],
                )
                return {
                    "status": "error",
                    "error": f"HTTP {resp.status_code}",
                    "details": resp.text[:300],
                }
        except Exception as e:
            logger.warning(
                "openviking 服务不可用 path={}: {}",
                path,
                e,
            )
            return {
                "status": "error",
                "error": str(e),
            }
