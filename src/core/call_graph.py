"""调用图模块，基于 tree-sitter 解析源码构建项目内函数/方法的调用关系。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from src.core.code_storage import CodeStorage

# 文件扩展名到 tree-sitter 语言名称的映射
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
}

# 各语言的函数定义节点类型
_FUNCTION_DEF_TYPES: dict[str, set[str]] = {
    "python": {"function_definition"},
    "java": {"method_declaration", "constructor_declaration"},
    "javascript": {"function_declaration", "method_definition"},
    "typescript": {"function_declaration", "method_definition"},
    "go": {"function_declaration", "method_declaration"},
    "c": {"function_definition"},
    "cpp": {"function_definition"},
    "rust": {"function_item"},
}

# 各语言的函数调用节点类型
_CALL_TYPES: dict[str, set[str]] = {
    "python": {"call"},
    "java": {"method_invocation"},
    "javascript": {"call_expression"},
    "typescript": {"call_expression"},
    "go": {"call_expression"},
    "c": {"call_expression"},
    "cpp": {"call_expression"},
    "rust": {"call_expression"},
}

# 各语言的类定义节点类型
_CLASS_DEF_TYPES: dict[str, set[str]] = {
    "python": {"class_definition"},
    "java": {"class_declaration"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration"},
    "go": {"type_declaration"},
    "c": {"struct_specifier"},
    "cpp": {"class_specifier", "struct_specifier"},
    "rust": {"struct_item"},
}


def _get_parser(language: str):
    """获取 tree-sitter 解析器。"""
    from tree_sitter_languages import get_parser

    return get_parser(language)


def _node_name(node) -> str | None:
    """提取节点的名称标识符文本。

    尝试从节点的子节点中找到 name 或 function 字段。
    """
    # 尝试直接获取 name 子节点
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8") if child.text else None
        if child.type == "field_identifier":
            return child.text.decode("utf-8") if child.text else None
    # 尝试获取 named_children 中的第一个 identifier
    for child in node.named_children:
        if child.type == "identifier":
            return child.text.decode("utf-8") if child.text else None
    return None


def _extract_call_name(node) -> str | None:
    """从调用节点中提取被调用的函数名。"""
    # 对于 call_expression / method_invocation，函数名通常在 function 子节点
    func_node = node.child_by_field_name("function")
    if func_node is not None:
        # 可能是 identifier 或 member_expression（如 obj.method）
        if func_node.type == "identifier":
            return func_node.text.decode("utf-8") if func_node.text else None
        if func_node.type == "member_expression":
            # 提取属性名（最后一个 identifier）
            prop = func_node.child_by_field_name("property")
            if prop and prop.text:
                return prop.text.decode("utf-8")
        if func_node.type == "field_access":
            prop = func_node.child_by_field_name("field")
            if prop and prop.text:
                return prop.text.decode("utf-8")
        return func_node.text.decode("utf-8") if func_node.text else None

    # Python call 节点的函数名在第一个 identifier 子节点
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8") if child.text else None

    # Java method_invocation 的 name 字段
    name_node = node.child_by_field_name("name")
    if name_node and name_node.text:
        return name_node.text.decode("utf-8")

    return None


class CallGraph:
    """项目调用图，记录函数/方法间的调用关系。

    基于 tree-sitter 解析源码，提取函数定义、函数调用、类定义等信息，
    构建调用关系图，支持调用链追踪。
    """

    def __init__(self, project_id: str):
        """初始化调用图。

        Args:
            project_id: 项目 ID
        """
        self._project_id = project_id

        # 函数名 -> 定义位置列表
        self._functions: dict[str, list[dict]] = {}
        # 函数名 -> 调用方列表（谁调用了这个函数）
        self._callers: dict[str, list[dict]] = {}
        # 函数名 -> 被调用方列表（这个函数调用了哪些函数）
        self._callees: dict[str, list[dict]] = {}
        # 类名 -> 类信息
        self._classes: dict[str, dict] = {}
        # 文件路径 -> 符号列表
        self._file_symbols: dict[str, list[dict]] = {}
        # 文件路径 -> 语言
        self._file_languages: dict[str, str] = {}

        logger.debug(f"初始化调用图: {project_id}")

    def _get_language(self, file_path: str) -> str | None:
        """根据文件扩展名获取语言名称。"""
        from pathlib import Path

        ext = Path(file_path).suffix.lower()
        return _EXT_TO_LANG.get(ext)

    def build_from_storage(self, storage: CodeStorage) -> None:
        """从代码存储中扫描所有源码文件并构建调用图。

        Args:
            storage: 项目的代码存储实例
        """
        self.clear()
        all_files = storage.get_all_files()
        code_files = [
            f for f in all_files if self._get_language(f) is not None
        ]
        logger.info(
            f"开始构建调用图: project={self._project_id}, "
            f"总文件={len(all_files)}, 代码文件={len(code_files)}"
        )

        for file_path in code_files:
            language = self._get_language(file_path)
            if language is None:
                continue
            try:
                content = storage.read_file(file_path)
                self.build_from_file(file_path, content, language)
            except Exception as e:
                logger.warning(
                    f"解析文件失败，跳过: {file_path}, error={e}"
                )

        logger.info(
            f"调用图构建完成: project={self._project_id}, "
            f"函数数={len(self._functions)}, 类数={len(self._classes)}"
        )

    def build_from_file(
        self, file_path: str, content: str, language: str
    ) -> None:
        """解析单个文件，更新调用图。

        Args:
            file_path: 文件路径
            content: 文件文本内容
            language: 编程语言
        """
        if language not in _FUNCTION_DEF_TYPES:
            return

        self._file_languages[file_path] = language

        try:
            parser = _get_parser(language)
        except Exception as e:
            logger.warning(
                f"无法获取 {language} 解析器，跳过: {file_path}, {e}"
            )
            return

        try:
            tree = parser.parse(bytes(content, "utf-8"))
        except Exception as e:
            logger.warning(f"解析失败: {file_path}, {e}")
            return

        root = tree.root_node
        symbols: list[dict] = []

        # 提取函数定义
        func_def_types = _FUNCTION_DEF_TYPES.get(language, set())
        call_types = _CALL_TYPES.get(language, set())
        class_def_types = _CLASS_DEF_TYPES.get(language, set())

        # 遍历所有节点
        self._walk_and_extract(
            root,
            file_path,
            func_def_types,
            call_types,
            class_def_types,
            symbols,
        )

        self._file_symbols[file_path] = symbols

    def _walk_and_extract(
        self,
        node,
        file_path: str,
        func_def_types: set[str],
        call_types: set[str],
        class_def_types: set[str],
        symbols: list[dict],
        current_class: str | None = None,
    ) -> None:
        """递归遍历 AST 节点，提取函数定义、调用和类定义。"""
        # 检查是否是类定义
        if node.type in class_def_types:
            class_name = _node_name(node)
            if class_name:
                class_info: dict[str, Any] = {
                    "class": class_name,
                    "file_path": file_path,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "parents": [],
                    "methods": [],
                }
                # 尝试提取父类信息
                parent_args = node.child_by_field_name("superclass")
                if parent_args is None:
                    # Python/Multi-class inheritance: arguments 节点
                    for child in node.children:
                        if child.type == "argument_list":
                            for arg in child.named_children:
                                if arg.type == "identifier" and arg.text:
                                    class_info["parents"].append(
                                        arg.text.decode("utf-8")
                                    )
                elif parent_args.text:
                    class_info["parents"].append(
                        parent_args.text.decode("utf-8")
                    )

                self._classes[class_name] = class_info
                symbols.append(
                    {
                        "name": class_name,
                        "type": "class",
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
                # 递归处理类体内的方法
                for child in node.children:
                    self._walk_and_extract(
                        child,
                        file_path,
                        func_def_types,
                        call_types,
                        class_def_types,
                        symbols,
                        class_name,
                    )
                return

        # 检查是否是函数定义
        if node.type in func_def_types:
            func_name = _node_name(node)
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1

            if func_name:
                func_info = {
                    "function": func_name,
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "class": current_class,
                }
                self._functions.setdefault(func_name, []).append(func_info)
                symbols.append(
                    {
                        "name": func_name,
                        "type": "method" if current_class else "function",
                        "start_line": start_line,
                        "end_line": end_line,
                    }
                )
                if current_class:
                    cls = self._classes.get(current_class)
                    if cls:
                        cls["methods"].append(
                            {
                                "name": func_name,
                                "start_line": start_line,
                                "end_line": end_line,
                            }
                        )

                # 在函数体内查找调用
                # 函数体通常是最后一个 named child
                body = None
                for child in node.named_children:
                    if child.type in (
                        "block",
                        "suite",
                        "function_body",
                        "block_statement",
                    ):
                        body = child
                        break
                if body is None:
                    # 取最后一个 named child 作为 body
                    if node.named_children:
                        body = node.named_children[-1]

                if body is not None:
                    self._find_calls_in_node(
                        body, func_name, file_path, call_types
                    )

            # 继续递归处理嵌套定义
            for child in node.children:
                self._walk_and_extract(
                    child,
                    file_path,
                    func_def_types,
                    call_types,
                    class_def_types,
                    symbols,
                    current_class,
                )
            return

        # 递归处理子节点
        for child in node.children:
            self._walk_and_extract(
                child,
                file_path,
                func_def_types,
                call_types,
                class_def_types,
                symbols,
                current_class,
            )

    def _find_calls_in_node(
        self,
        node,
        caller_name: str,
        file_path: str,
        call_types: set[str],
    ) -> None:
        """在节点子树中查找所有函数调用。"""
        if node.type in call_types:
            called_name = _extract_call_name(node)
            call_line = node.start_point[0] + 1
            if called_name:
                # 记录调用关系
                self._callees.setdefault(caller_name, []).append(
                    {
                        "function": called_name,
                        "file_path": file_path,
                        "line": call_line,
                    }
                )
                self._callers.setdefault(called_name, []).append(
                    {
                        "function": caller_name,
                        "file_path": file_path,
                        "line": call_line,
                    }
                )

        for child in node.children:
            self._find_calls_in_node(
                child, caller_name, file_path, call_types
            )

    def get_callers(self, function_name: str) -> list[dict]:
        """查找谁调用了某函数。

        Args:
            function_name: 函数名

        Returns:
            调用方列表，每项包含 function、file_path、line
        """
        return self._callers.get(function_name, [])

    def get_callees(self, function_name: str) -> list[dict]:
        """查找某函数调用了哪些函数。

        Args:
            function_name: 函数名

        Returns:
            被调用方列表，每项包含 function、file_path、line
        """
        return self._callees.get(function_name, [])

    def get_function_location(self, function_name: str) -> list[dict]:
        """查找函数定义位置。

        Args:
            function_name: 函数名

        Returns:
            定义位置列表，每项包含 function、file_path、start_line、end_line
        """
        return self._functions.get(function_name, [])

    def get_all_functions(self) -> list[dict]:
        """获取所有已解析的函数。

        Returns:
            函数列表，每项包含 name、file_path、start_line、end_line
        """
        result = []
        for func_name, locations in self._functions.items():
            for loc in locations:
                result.append(
                    {
                        "name": func_name,
                        "file_path": loc["file_path"],
                        "start_line": loc["start_line"],
                        "end_line": loc["end_line"],
                    }
                )
        return result

    def get_class_hierarchy(self, class_name: str) -> dict:
        """获取类继承结构。

        Args:
            class_name: 类名

        Returns:
            类信息字典，包含 class、file_path、parents、methods；
            不存在时返回空字典
        """
        return self._classes.get(class_name, {})

    def get_symbols(self, file_path: str) -> list[dict]:
        """获取文件的符号列表。

        Args:
            file_path: 文件路径

        Returns:
            符号列表，每项包含 name、type、start_line、end_line
        """
        return self._file_symbols.get(file_path, [])

    def clear(self) -> None:
        """清空调用图。"""
        self._functions.clear()
        self._callers.clear()
        self._callees.clear()
        self._classes.clear()
        self._file_symbols.clear()
        self._file_languages.clear()
        logger.debug(f"已清空调用图: {self._project_id}")
