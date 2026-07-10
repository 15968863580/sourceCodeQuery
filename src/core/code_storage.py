"""按项目隔离的本地代码文件存储管理。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger


class CodeStorage:
    """按项目隔离的本地代码文件存储。

    以 base_data_dir / project_id / "code" 为存储根目录，
    提供文件读写、目录树构建、行范围读取等功能。
    所有文件路径均经过安全校验，防止目录穿越攻击。
    """

    def __init__(self, base_data_dir: Path, project_id: str):
        """初始化代码存储。

        Args:
            base_data_dir: 项目数据根目录
            project_id: 项目 ID，用于构建子目录
        """
        self._base_path = base_data_dir / project_id / "code"
        self._base_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"初始化代码存储: {self._base_path}")

    def _resolve_path(self, file_path: str) -> Path:
        """将相对路径解析为绝对路径，并防止目录穿越攻击。

        Args:
            file_path: 相对于存储根目录的路径

        Returns:
            解析后的绝对路径

        Raises:
            ValueError: 路径越界时抛出
        """
        base = self._base_path.resolve()
        # resolve() 会解析所有 ".." 和符号链接
        full_path = (self._base_path / file_path).resolve()
        if not full_path.is_relative_to(base):
            raise ValueError(f"路径越界，禁止目录穿越: {file_path}")
        return full_path

    def _to_relpath(self, abs_path: Path) -> str:
        """将绝对路径转换为相对于存储根目录的 POSIX 路径。

        Args:
            abs_path: 存储根目录内的绝对路径

        Returns:
            使用正斜杠的相对路径字符串
        """
        return abs_path.relative_to(self._base_path.resolve()).as_posix()

    def save_file(self, file_path: str, content: str) -> None:
        """保存文件，自动创建所需的目录结构。

        Args:
            file_path: 相对路径，如 "src/main/java/AuthController.java"
            content: 文件文本内容
        """
        full_path = self._resolve_path(file_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        logger.debug(f"保存文件: {file_path}, 大小={len(content)}")

    def read_file(self, file_path: str) -> str:
        """读取完整文件内容。

        自动尝试 UTF-8 和 GBK 两种编码，兼容中文项目。

        Args:
            file_path: 相对路径

        Returns:
            文件文本内容

        Raises:
            FileNotFoundError: 文件不存在时抛出
        """
        full_path = self._resolve_path(file_path)
        if not full_path.is_file():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        # 先尝试 UTF-8，失败则用 GBK
        try:
            content = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = full_path.read_text(encoding="gbk", errors="replace")
        logger.debug(f"读取文件: {file_path}, 大小={len(content)}")
        return content

    def read_file_range(
        self, file_path: str, start_line: int, end_line: int
    ) -> str:
        """读取文件指定行范围（1-based），返回带行号的内容。

        行号右对齐，格式如 "  1→content\\n  2→content"。

        Args:
            file_path: 相对路径
            start_line: 起始行号（1-based，含）
            end_line: 结束行号（1-based，含）

        Returns:
            带行号的内容字符串，若起始行超出文件范围则返回空字符串

        Raises:
            ValueError: 起始行小于 1 或大于结束行时抛出
            FileNotFoundError: 文件不存在时抛出
        """
        content = self.read_file(file_path)
        if not content:
            return ""

        lines = content.split("\n")
        # 去掉末尾因换行符产生的空行，使行号与编辑器一致
        if lines and lines[-1] == "" and content.endswith("\n"):
            lines = lines[:-1]

        total = len(lines)
        if start_line < 1:
            raise ValueError(f"起始行不能小于 1: {start_line}")
        if start_line > total:
            return ""
        if end_line > total:
            end_line = total
        if start_line > end_line:
            raise ValueError(
                f"起始行({start_line})不能大于结束行({end_line})"
            )

        selected = lines[start_line - 1 : end_line]
        width = len(str(end_line))
        result = []
        for i, line in enumerate(selected):
            line_num = start_line + i
            result.append(f"{line_num:>{width}}→{line}")
        return "\n".join(result)

    def file_exists(self, file_path: str) -> bool:
        """检查文件是否存在。

        Args:
            file_path: 相对路径

        Returns:
            文件存在返回 True，否则 False
        """
        full_path = self._resolve_path(file_path)
        return full_path.is_file()

    def list_files(self, directory: str = "") -> list[str]:
        """列出指定目录下的所有文件（相对路径，递归）。

        Args:
            directory: 相对目录路径，空字符串表示根目录

        Returns:
            文件相对路径列表，按路径排序
        """
        dir_path = self._resolve_path(directory)
        if not dir_path.is_dir():
            return []

        result = []
        for path in sorted(dir_path.rglob("*")):
            if path.is_file():
                result.append(self._to_relpath(path))
        logger.debug(f"列出目录 {directory or '(root)'} 文件: {len(result)} 个")
        return result

    def get_file_tree(self, directory: str = "") -> dict:
        """返回目录树结构。

        Args:
            directory: 相对目录路径，空字符串表示根目录

        Returns:
            树结构字典: {"name": "...", "type": "dir"/"file", "children": [...]}

        Raises:
            FileNotFoundError: 目录不存在时抛出
        """
        dir_path = self._resolve_path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"目录不存在: {directory}")

        def build_tree(path: Path) -> dict:
            """递归构建目录树节点。"""
            if path.is_file():
                return {"name": path.name, "type": "file", "children": []}
            # 目录优先，再按名称排序
            children = []
            for child in sorted(
                path.iterdir(), key=lambda p: (not p.is_dir(), p.name)
            ):
                children.append(build_tree(child))
            return {"name": path.name, "type": "dir", "children": children}

        return build_tree(dir_path)

    def delete_file(self, file_path: str) -> None:
        """删除文件。

        Args:
            file_path: 相对路径

        Raises:
            FileNotFoundError: 文件不存在时抛出
        """
        full_path = self._resolve_path(file_path)
        if not full_path.is_file():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        full_path.unlink()
        logger.debug(f"删除文件: {file_path}")

    def get_all_files(self) -> list[str]:
        """获取项目中所有文件的相对路径列表。

        Returns:
            所有文件的相对路径列表，按路径排序
        """
        return self.list_files("")

    def get_file_line_count(self, file_path: str) -> int:
        """获取文件行数。

        Args:
            file_path: 相对路径

        Returns:
            文件行数，空文件返回 0

        Raises:
            FileNotFoundError: 文件不存在时抛出
        """
        content = self.read_file(file_path)
        if not content:
            return 0
        # 以换行符计数，末尾换行符不额外计为一行
        return content.count("\n") + (0 if content.endswith("\n") else 1)
