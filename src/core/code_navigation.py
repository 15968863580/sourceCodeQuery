"""代码导航工具集，为 LLM Agent 提供代码浏览和追踪能力。

整合 CodeIndex（语义/关键词搜索）、CodeStorage（文件读取）、
CallGraph（调用链追踪）三个模块，提供统一的代码导航接口。
"""

from __future__ import annotations

from loguru import logger

from src.core.call_graph import CallGraph
from src.core.code_index import CodeIndex
from src.core.code_storage import CodeStorage


class CodeNavigation:
    """代码导航工具集，供 LLM Agent 在推理循环中自主调用。

    提供代码搜索、文件读取、调用链追踪、符号查询等能力，
    使 LLM 能够像工程师一样深入阅读和理解代码。
    """

    def __init__(
        self,
        storage: CodeStorage,
        index: CodeIndex,
        call_graph: CallGraph,
    ):
        """初始化代码导航工具集。

        Args:
            storage: 项目的代码存储实例
            index: 项目的代码索引实例
            call_graph: 项目的调用图实例
        """
        self._storage = storage
        self._index = index
        self._call_graph = call_graph

    def search_code(self, query: str, limit: int = 10) -> list[dict]:
        """语义搜索代码，快速定位起点。

        优先使用向量语义搜索（精确），失败时回退到本地关键词搜索（兜底）。

        Args:
            query: 搜索查询语句
            limit: 返回结果数量上限

        Returns:
            结果列表，每项包含 file_path、start_line、end_line、content、score
        """
        try:
            results = self._index.search(query, limit=limit)
            if results:
                logger.debug(
                    f"语义搜索 '{query}': 向量索引返回 {len(results)} 条结果"
                )
                return results
        except Exception as e:
            logger.warning(f"向量语义搜索失败，回退到本地关键词搜索: {e}")

        # 回退：从查询中提取关键词做本地搜索
        keywords = self._extract_keywords(query)
        all_results: list[dict] = []
        for kw in keywords:
            all_results.extend(
                self.search_keyword_local(kw, limit=limit)
            )
            if len(all_results) >= limit:
                break
        # 去重
        seen = set()
        deduped: list[dict] = []
        for r in all_results:
            if r["file_path"] not in seen:
                seen.add(r["file_path"])
                deduped.append(r)
        return deduped[:limit]

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        """从自然语言查询中提取关键词用于回退搜索。"""
        # 简单分词：按空格和中文字符边界分割
        import re

        tokens = re.findall(r"[\w]+", query)
        # 过滤掉太短的词
        return [t for t in tokens if len(t) >= 2]

    def search_keyword(self, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索代码。

        优先使用 ChromaDB 索引搜索（快），失败时回退到本地文件搜索（兜底）。

        Args:
            keyword: 关键词
            limit: 返回结果数量上限

        Returns:
            结果列表
        """
        try:
            results = self._index.search_keyword(keyword, limit=limit)
            if results:
                logger.debug(
                    f"关键词搜索 '{keyword}': ChromaDB 返回 {len(results)} 条结果"
                )
                return results
        except Exception as e:
            logger.warning(f"ChromaDB 关键词搜索失败，回退到本地搜索: {e}")

        # 回退：本地文件搜索
        return self.search_keyword_local(keyword, limit=limit)

    def search_keyword_local(
        self, keyword: str, limit: int = 10
    ) -> list[dict]:
        """本地关键词搜索（不依赖向量索引）。

        遍历本地代码文件，搜索包含关键词的文件和匹配行。

        Args:
            keyword: 搜索关键词
            limit: 返回结果数量上限

        Returns:
            结果列表，每项包含 file_path、matching_lines、content_snippet
        """
        results: list[dict] = []
        keyword_lower = keyword.lower()
        all_files = self._storage.get_all_files()

        for file_path in all_files:
            if not self._is_code_file(file_path):
                continue
            try:
                content = self._storage.read_file(file_path)
            except Exception:
                continue

            lines = content.splitlines()
            matching_lines: list[dict] = []
            for i, line in enumerate(lines, 1):
                if keyword_lower in line.lower():
                    matching_lines.append(
                        {
                            "line": i,
                            "content": line.strip()[:200],
                        }
                    )
                    if len(matching_lines) >= 5:
                        break

            if matching_lines:
                results.append(
                    {
                        "file_path": file_path,
                        "matching_lines": matching_lines,
                        "content_snippet": matching_lines[0]["content"],
                    }
                )
                if len(results) >= limit:
                    break

        logger.debug(
            f"本地关键词搜索 '{keyword}': 找到 {len(results)} 个文件"
        )
        return results

    @staticmethod
    def _is_code_file(file_path: str) -> bool:
        """判断是否为代码文件（排除 .git、二进制等）。"""
        from src.core.scheduler import CodeSyncScheduler

        return CodeSyncScheduler._is_code_file(file_path)

    def read_file(self, file_path: str) -> str:
        """读取完整文件内容。

        Args:
            file_path: 文件相对路径

        Returns:
            文件文本内容
        """
        content = self._storage.read_file(file_path)
        logger.debug(f"读取文件: {file_path}, 大小={len(content)}")
        return content

    def read_file_range(
        self, file_path: str, start_line: int, end_line: int
    ) -> str:
        """读取文件指定行范围（1-based），返回带行号的内容。

        Args:
            file_path: 文件相对路径
            start_line: 起始行号
            end_line: 结束行号

        Returns:
            带行号的内容字符串
        """
        content = self._storage.read_file_range(file_path, start_line, end_line)
        logger.debug(
            f"读取文件范围: {file_path}, "
            f"lines={start_line}-{end_line}"
        )
        return content

    def trace_callers(self, function_name: str) -> list[dict]:
        """查找谁调用了某函数（上游追踪）。

        Args:
            function_name: 函数名

        Returns:
            调用方列表，每项包含 function、file_path、line
        """
        callers = self._call_graph.get_callers(function_name)
        logger.debug(
            f"追踪调用方 '{function_name}': 找到 {len(callers)} 个"
        )
        return callers

    def trace_callees(self, function_name: str) -> list[dict]:
        """查找某函数调用了哪些函数（下游追踪）。

        Args:
            function_name: 函数名

        Returns:
            被调用方列表，每项包含 function、file_path、line
        """
        callees = self._call_graph.get_callees(function_name)
        logger.debug(
            f"追踪被调用方 '{function_name}': 找到 {len(callees)} 个"
        )
        return callees

    def get_file_tree(self, directory: str = "") -> dict:
        """获取目录结构，理解项目组织方式。

        Args:
            directory: 相对目录路径，空字符串表示根目录

        Returns:
            目录树字典
        """
        tree = self._storage.get_file_tree(directory)
        logger.debug(f"获取目录树: {directory or '(root)'}")
        return tree

    def get_symbols(self, file_path: str) -> list[dict]:
        """获取文件的符号列表，快速概览文件内容。

        Args:
            file_path: 文件路径

        Returns:
            符号列表，每项包含 name、type、start_line、end_line
        """
        symbols = self._call_graph.get_symbols(file_path)
        logger.debug(
            f"获取符号: {file_path}, 共 {len(symbols)} 个"
        )
        return symbols

    def get_function_location(self, function_name: str) -> list[dict]:
        """查找函数定义位置。

        Args:
            function_name: 函数名

        Returns:
            定义位置列表，每项包含 function、file_path、start_line、end_line
        """
        locations = self._call_graph.get_function_location(function_name)
        logger.debug(
            f"查找函数位置 '{function_name}': 找到 {len(locations)} 个"
        )
        return locations

    def get_all_functions(self) -> list[dict]:
        """获取项目中所有已解析的函数。

        Returns:
            函数列表，每项包含 name、file_path、start_line、end_line
        """
        funcs = self._call_graph.get_all_functions()
        logger.debug(f"获取所有函数: 共 {len(funcs)} 个")
        return funcs

    def get_class_hierarchy(self, class_name: str) -> dict:
        """获取类继承结构，理解面向对象设计。

        Args:
            class_name: 类名

        Returns:
            类信息字典，包含 class、file_path、parents、methods
        """
        hierarchy = self._call_graph.get_class_hierarchy(class_name)
        logger.debug(f"获取类继承结构 '{class_name}'")
        return hierarchy

    def file_exists(self, file_path: str) -> bool:
        """检查文件是否存在。

        Args:
            file_path: 文件路径

        Returns:
            存在返回 True
        """
        return self._storage.file_exists(file_path)

    def list_files(self, directory: str = "") -> list[str]:
        """列出指定目录下的所有文件。

        Args:
            directory: 相对目录路径

        Returns:
            文件路径列表
        """
        files = self._storage.list_files(directory)
        logger.debug(
            f"列出文件: {directory or '(root)'}, 共 {len(files)} 个"
        )
        return files

    def get_file_line_count(self, file_path: str) -> int:
        """获取文件行数。

        Args:
            file_path: 文件路径

        Returns:
            行数
        """
        return self._storage.get_file_line_count(file_path)
