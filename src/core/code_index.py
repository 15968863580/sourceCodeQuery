"""代码索引引擎，基于 ChromaDB 实现语义搜索和关键词搜索，按项目隔离。"""

from __future__ import annotations

from typing import Any

import chromadb
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings
from loguru import logger

from src.models.config import EmbeddingConfig, IndexConfig


class OpenAIEmbeddingFunction(EmbeddingFunction):
    """ChromaDB 自定义 Embedding 函数，使用 OpenAI API。

    当 OpenAI API 不可用时，降级使用 ChromaDB 默认的 sentence-transformers 模型。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._client = None
        self._fallback = None
        self._init_client()

    def _init_client(self) -> None:
        """初始化 OpenAI 客户端，失败则准备降级方案。"""
        if not self._api_key:
            logger.warning("OpenAI API key 为空，将使用 ChromaDB 默认 embedding")
            self._fallback = self._init_default_embedding()
            return
        try:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
            logger.info(f"使用 OpenAI embedding: model={self._model}")
        except Exception as e:
            logger.warning(f"初始化 OpenAI embedding 失败，降级使用默认: {e}")
            self._fallback = self._init_default_embedding()

    def _init_default_embedding(self) -> EmbeddingFunction | None:
        """初始化 ChromaDB 默认 embedding 函数作为降级方案。"""
        try:
            from chromadb.utils.embedding_functions import (
                DefaultEmbeddingFunction,
            )

            return DefaultEmbeddingFunction()
        except Exception as e:
            logger.error(f"初始化默认 embedding 也失败: {e}")
            return None

    def __call__(self, input: Documents) -> Embeddings:
        """将文本列表转换为向量列表。"""
        if self._fallback:
            return self._fallback(input)
        if self._client is None:
            raise RuntimeError("没有可用的 embedding 函数")

        # OpenAI API 单次请求限制，分批处理
        all_embeddings: list[list[float]] = []
        batch_size = 100
        for i in range(0, len(input), batch_size):
            batch = input[i : i + batch_size]
            # 过滤空字符串，避免 API 报错
            batch = [t if t.strip() else " " for t in batch]
            resp = self._client.embeddings.create(
                input=batch, model=self._model
            )
            all_embeddings.extend([d.embedding for d in resp.data])
        return all_embeddings

    def name(self) -> str:
        return "openai_embedding"


class CodeIndex:
    """代码索引引擎，按项目隔离。

    为每个项目创建独立的 ChromaDB collection，
    支持语义搜索和关键词搜索。
    """

    def __init__(
        self,
        index_config: IndexConfig,
        embedding_config: EmbeddingConfig,
        project_id: str,
    ):
        """初始化代码索引。

        Args:
            index_config: 索引配置
            embedding_config: Embedding 模型配置
            project_id: 项目 ID，用于创建独立的 collection
        """
        self._config = index_config
        self._project_id = project_id
        self._collection_name = f"code_{project_id}"

        # 初始化 embedding 函数
        self._embed_fn = OpenAIEmbeddingFunction(
            api_key=embedding_config.api_key,
            base_url=embedding_config.base_url,
            model=embedding_config.model,
        )

        # 初始化 ChromaDB 客户端（持久化模式）
        self._client = chromadb.PersistentClient(
            path=index_config.storage_path,
        )
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=self._embed_fn,
            metadata={"project_id": project_id},
        )
        logger.info(
            f"初始化代码索引: project={project_id}, "
            f"collection={self._collection_name}, "
            f"现有 {self._collection.count()} 个 chunk"
        )

    def _chunk_file(
        self, content: str, file_path: str, language: str
    ) -> list[dict[str, Any]]:
        """将文件内容按行数分块。

        分块策略：按 chunk_size 行分块，有 chunk_overlap 行重叠。
        尽量在空行处分块以保留代码完整性。

        Args:
            content: 文件文本内容
            file_path: 文件路径
            language: 编程语言

        Returns:
            chunk 列表，每个 chunk 包含 id、content、metadata
        """
        lines = content.split("\n")
        total_lines = len(lines)
        if total_lines == 0:
            return []

        chunk_size = self._config.chunk_size
        overlap = self._config.chunk_overlap
        chunks: list[dict[str, Any]] = []

        start = 0
        while start < total_lines:
            end = min(start + chunk_size, total_lines)

            # 尝试在空行处调整分块边界，保留代码完整性
            if end < total_lines and end - start >= chunk_size // 2:
                # 从预期 end 往前找最近的空行
                for adjust in range(end, max(start + chunk_size // 2, start), -1):
                    if lines[adjust - 1].strip() == "":
                        end = adjust
                        break

            chunk_lines = lines[start:end]
            chunk_content = "\n".join(chunk_lines)

            chunk_id = f"{file_path}:{start + 1}:{end}"
            chunks.append(
                {
                    "id": chunk_id,
                    "content": chunk_content,
                    "metadata": {
                        "file_path": file_path,
                        "start_line": start + 1,
                        "end_line": end,
                        "language": language,
                    },
                }
            )

            # 计算下一个 chunk 的起始位置
            next_start = end - overlap
            if next_start <= start:
                next_start = start + 1
            start = next_start

        return chunks

    def index_file(
        self, file_path: str, content: str, language: str = ""
    ) -> None:
        """将文件内容分块后建立索引。

        先删除该文件的旧索引，再添加新索引。

        Args:
            file_path: 文件路径
            content: 文件文本内容
            language: 编程语言
        """
        # 先删除该文件的旧索引
        self.remove_file(file_path)

        chunks = self._chunk_file(content, file_path, language)
        if not chunks:
            logger.debug(f"文件无内容可索引: {file_path}")
            return

        ids = [c["id"] for c in chunks]
        documents = [c["content"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]

        self._collection.add(
            ids=ids, documents=documents, metadatas=metadatas
        )
        logger.debug(
            f"索引文件: {file_path}, chunks={len(chunks)}, "
            f"language={language}"
        )

    def index_batch(
        self, files: list[tuple[str, str, str]]
    ) -> None:
        """批量索引文件。

        Args:
            files: [(file_path, content, language), ...]
        """
        for file_path, content, language in files:
            self.index_file(file_path, content, language)
        logger.info(f"批量索引完成: {len(files)} 个文件")

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """语义搜索。

        Args:
            query: 搜索查询语句
            limit: 返回结果数量上限

        Returns:
            结果列表，每项包含 file_path、start_line、end_line、content、distance
        """
        if self._collection.count() == 0:
            logger.debug("索引为空，跳过搜索")
            return []

        try:
            results = self._collection.query(
                query_texts=[query], n_results=limit
            )
        except Exception as e:
            logger.error(f"语义搜索失败: {e}")
            return []

        return self._format_results(results)

    def search_keyword(self, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索，基于 ChromaDB 的文档内容过滤。

        Args:
            keyword: 关键词
            limit: 返回结果数量上限

        Returns:
            结果列表
        """
        if self._collection.count() == 0:
            return []

        try:
            results = self._collection.query(
                query_texts=[keyword],
                n_results=limit,
                where_document={"$contains": keyword},
            )
        except Exception as e:
            logger.error(f"关键词搜索失败: {e}")
            return []

        return self._format_results(results)

    def _format_results(self, results: dict) -> list[dict]:
        """格式化 ChromaDB 查询结果为统一结构。"""
        formatted: list[dict] = []

        if not results or not results.get("ids"):
            return formatted

        ids_list = results.get("ids", [[]])
        documents_list = results.get("documents", [[]])
        metadatas_list = results.get("metadatas", [[]])
        distances_list = results.get("distances", [[]])

        # 取第一个查询的结果
        ids = ids_list[0] if ids_list else []
        documents = documents_list[0] if documents_list else []
        metadatas = metadatas_list[0] if metadatas_list else []
        distances = distances_list[0] if distances_list else []

        for i, chunk_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            dist = distances[i] if i < len(distances) else 0.0
            formatted.append(
                {
                    "file_path": meta.get("file_path", ""),
                    "start_line": meta.get("start_line", 0),
                    "end_line": meta.get("end_line", 0),
                    "content": doc,
                    "distance": dist,
                    "score": max(0.0, 1.0 - dist),
                    "language": meta.get("language", ""),
                }
            )

        return formatted

    def remove_file(self, file_path: str) -> None:
        """删除指定文件的所有索引。

        Args:
            file_path: 文件路径
        """
        try:
            self._collection.delete(
                where={"file_path": file_path}
            )
        except Exception as e:
            logger.debug(f"删除文件索引（可能不存在）: {file_path}, {e}")

    def clear(self) -> None:
        """清空当前项目的所有索引。"""
        try:
            self._client.delete_collection(self._collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                embedding_function=self._embed_fn,
                metadata={"project_id": self._project_id},
            )
            logger.info(f"已清空索引: project={self._project_id}")
        except Exception as e:
            logger.error(f"清空索引失败: {e}")

    def get_stats(self) -> dict:
        """返回索引统计信息。"""
        try:
            total_chunks = self._collection.count()
            # 获取所有不同的文件路径
            all_metas = self._collection.get(include=["metadatas"])
            file_paths = set()
            for meta in all_metas.get("metadatas", []):
                if meta and "file_path" in meta:
                    file_paths.add(meta["file_path"])
            return {
                "total_chunks": total_chunks,
                "total_files": len(file_paths),
                "files": sorted(file_paths),
            }
        except Exception as e:
            logger.error(f"获取索引统计失败: {e}")
            return {"total_chunks": 0, "total_files": 0, "files": []}
