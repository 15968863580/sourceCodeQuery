"""代码深度解读引擎，实现 ReAct (Reasoning + Acting) 模式的 LLM Agent 推理循环。

LLM 可自主调用代码导航工具来搜索、阅读、追踪代码，最终合成结构化的逻辑解读。
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

from src.core.code_navigation import CodeNavigation
from src.core.memory_service import MemoryService
from src.models.config import LLMConfig

# 单条工具结果发送给 LLM 时的最大字符数，防止上下文溢出
_MAX_TOOL_RESULT_CHARS: int = 12000
# 推理轨迹中单条结果的最大字符数，便于可读性
_TRACE_RESULT_CHARS: int = 500


class UnderstandingEngine:
    """代码深度解读引擎。

    基于 ReAct 模式构建 LLM Agent：LLM 在推理循环中自主决策调用哪些代码导航工具，
    逐步搜索定位、阅读理解、追踪调用链，最终合成完整的逻辑解读。

    核心能力：
        - understand(): 对用户问题进行多轮工具调用的深度分析
        - generate_summary(): 为单个文件生成逻辑摘要
        - generate_change_summary(): 为一批变更文件生成变更摘要
    """

    def __init__(
        self,
        config: LLMConfig,
        navigation: CodeNavigation,
        memory_service: MemoryService,
    ):
        """初始化解读引擎。

        Args:
            config: 大模型配置
            navigation: 代码导航工具集
            memory_service: 记忆服务
        """
        self._config = config
        self._navigation = navigation
        self._memory = memory_service
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        # 预构建工具定义，避免每次调用重复构建
        self._tools = self._build_tool_definitions()
        logger.debug(
            f"解读引擎初始化完成，model={config.model}, "
            f"summary_model={config.summary_model}, max_steps={config.max_reasoning_steps}"
        )

    # ------------------------------------------------------------------ #
    #  公开 API
    # ------------------------------------------------------------------ #

    async def understand(self, question: str, project_id: str) -> dict:
        """深度解读源码逻辑，核心 Agent 推理循环。

        使用 OpenAI function calling 让 LLM 自主调用代码导航工具，
        逐步搜索定位、阅读代码、追踪调用链，最终合成结构化解读。

        Args:
            question: 用户的代码理解问题
            project_id: 项目 ID

        Returns:
            解读结果字典，包含：
            - explanation: 完整的逻辑解读文本
            - code_locations: 涉及的代码位置列表
            - reasoning_trace: 推理过程轨迹
            - memory_stored: 是否成功存入记忆
        """
        logger.info(f"开始深度解读 project={project_id}, question={question[:80]}")

        # 1. 从记忆服务检索相关历史记忆
        memories = await self._retrieve_memories(project_id, question)

        # 2. 构建系统提示与初始消息
        system_prompt = self._build_system_prompt(project_id, memories)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        # 推理轨迹
        reasoning_trace: list[dict[str, Any]] = []
        max_steps = self._config.max_reasoning_steps

        # 3. ReAct 推理循环
        try:
            for step in range(1, max_steps + 1):
                response = await self._call_llm(messages, tools=self._tools)
                message = response.choices[0].message

                # 如果没有工具调用，说明 LLM 给出了最终回答
                if not message.tool_calls:
                    logger.info(f"LLM 在第 {step} 步给出最终回答")
                    explanation, locations = self._parse_final_answer(
                        message.content or ""
                    )
                    memory_stored = await self._store_qa_memory(
                        project_id, question, explanation
                    )
                    return {
                        "explanation": explanation,
                        "code_locations": locations,
                        "reasoning_trace": reasoning_trace,
                        "memory_stored": memory_stored,
                    }

                # 有工具调用：将 assistant 消息加入对话
                messages.append(message)

                # 执行所有工具调用（支持并行调用）
                thought = message.content or ""
                actions: list[str] = []
                results: list[str] = []
                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    action_desc = self._format_action(name, args)
                    logger.debug(f"步骤 {step} 工具调用: {action_desc}")

                    result_text = await self._execute_tool(name, args)
                    truncated_result = self._truncate(result_text, _MAX_TOOL_RESULT_CHARS)

                    # 将工具结果加入对话
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": truncated_result,
                        }
                    )

                    actions.append(action_desc)
                    results.append(self._truncate(result_text, _TRACE_RESULT_CHARS))

                # 记录推理轨迹
                reasoning_trace.append(
                    {
                        "step": step,
                        "thought": thought,
                        "action": "; ".join(actions),
                        "result": "; ".join(results),
                    }
                )

            # 4. 达到最大步数，强制要求 LLM 生成最终回答
            logger.warning(
                f"达到最大推理步数 {max_steps}，强制生成最终回答"
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "已达到最大推理步数。请立即基于你已收集的信息，"
                        "生成最终的逻辑解读，不要再调用任何工具。"
                    ),
                }
            )
            response = await self._call_llm(messages, tools=None)
            message = response.choices[0].message
            explanation, locations = self._parse_final_answer(
                message.content or ""
            )
            memory_stored = await self._store_qa_memory(
                project_id, question, explanation
            )
            return {
                "explanation": explanation,
                "code_locations": locations,
                "reasoning_trace": reasoning_trace,
                "memory_stored": memory_stored,
            }

        except Exception as e:
            logger.exception(f"解读引擎推理循环异常: {e}")
            return {
                "explanation": f"解读过程中发生错误: {e}",
                "code_locations": [],
                "reasoning_trace": reasoning_trace,
                "memory_stored": False,
            }

    async def generate_summary(
        self, file_path: str, content: str, project_id: str
    ) -> str:
        """为单个文件生成逻辑摘要（用于同步时批量生成）。

        使用 summary_model 进行单次 LLM 调用，不进入 Agent 循环。

        Args:
            file_path: 文件路径
            content: 文件内容
            project_id: 项目 ID

        Returns:
            文件逻辑摘要文本
        """
        logger.debug(f"生成文件摘要: {file_path}")

        # 大文件截断，防止超出上下文
        max_chars = self._config.max_tokens_per_read * 4
        truncated = self._truncate(content, max_chars)

        prompt = (
            "请为以下代码文件生成简洁的逻辑摘要，说明其主要职责、"
            "核心类/函数、关键逻辑流程。摘要应便于后续检索理解。\n\n"
            f"文件路径: {file_path}\n\n"
            f"代码内容:\n{truncated}"
        )

        messages = [
            {
                "role": "system",
                "content": "你是一位代码分析专家，擅长提炼代码的核心逻辑和职责。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._call_llm(
                messages, tools=None, model=self._config.summary_model  # type: ignore
            )
            summary = response.choices[0].message.content or ""
            logger.debug(f"文件摘要生成完成: {file_path}, 长度={len(summary)}")
            return summary
        except Exception as e:
            logger.error(f"生成文件摘要失败 {file_path}: {e}")
            return f"摘要生成失败: {e}"

    async def generate_change_summary(
        self, changed_files: list[dict], project_id: str
    ) -> str:
        """为一批变更文件生成变更摘要。

        Args:
            changed_files: 变更文件列表，每项包含 path 和 diff
            project_id: 项目 ID

        Returns:
            变更摘要文本
        """
        logger.info(
            f"生成变更摘要 project={project_id}, 文件数={len(changed_files)}"
        )

        if not changed_files:
            return "本次无文件变更。"

        # 拼接所有变更文件的 diff
        sections: list[str] = []
        for item in changed_files:
            path = item.get("path", "未知文件")
            diff = item.get("diff", "")
            # 单个 diff 截断，防止整体过长
            diff = self._truncate(diff, _MAX_TOOL_RESULT_CHARS)
            sections.append(f"---\n文件: {path}\n差异:\n{diff}")

        prompt = (
            "请分析以下代码变更，生成一份变更摘要，说明本次变更的主要内容和影响。"
            "请按变更类型分类总结（如新增功能、修复缺陷、重构等）。\n\n"
            f"变更文件:\n{chr(10).join(sections)}"
        )

        messages = [
            {
                "role": "system",
                "content": "你是一位代码分析专家，擅长总结代码变更的核心内容和影响。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._call_llm(
                messages, tools=None, model=self._config.model
            )
            summary = response.choices[0].message.content or ""
            logger.debug(f"变更摘要生成完成, 长度={len(summary)}")
            return summary
        except Exception as e:
            logger.error(f"生成变更摘要失败: {e}")
            return f"变更摘要生成失败: {e}"

    # ------------------------------------------------------------------ #
    #  工具定义与执行
    # ------------------------------------------------------------------ #

    def _build_tool_definitions(self) -> list[dict]:
        """构建 OpenAI function calling 工具定义列表。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": "语义搜索代码，根据自然语言描述查找相关代码片段",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索查询语句",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "返回结果数量上限",
                                "default": 10,
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_keyword",
                    "description": "关键词搜索代码，精确匹配关键词",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "搜索关键词",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "返回结果数量上限",
                                "default": 10,
                            },
                        },
                        "required": ["keyword"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "读取文件内容。可读取完整文件或指定行范围（通过 start_line/end_line）。"
                        "对于大文件，建议先不指定行范围触发概览，再按需读取行范围。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件相对路径",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "起始行号（1-based，可选）",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "结束行号（1-based，可选）",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "trace_callers",
                    "description": "追踪谁调用了某函数（上游调用方）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "function_name": {
                                "type": "string",
                                "description": "函数名",
                            },
                        },
                        "required": ["function_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "trace_callees",
                    "description": "追踪某函数调用了哪些函数（下游被调用方）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "function_name": {
                                "type": "string",
                                "description": "函数名",
                            },
                        },
                        "required": ["function_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_symbols",
                    "description": "获取文件的符号列表（类、函数等），快速概览文件结构",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件路径",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_file_tree",
                    "description": "获取目录结构树，了解项目组织方式",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "directory": {
                                "type": "string",
                                "description": "目录路径，空字符串表示根目录",
                                "default": "",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_function_location",
                    "description": "查找函数定义位置",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "function_name": {
                                "type": "string",
                                "description": "函数名",
                            },
                        },
                        "required": ["function_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_class_hierarchy",
                    "description": "获取类的继承结构和方法列表",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "class_name": {
                                "type": "string",
                                "description": "类名",
                            },
                        },
                        "required": ["class_name"],
                    },
                },
            },
        ]

    async def _execute_tool(self, name: str, args: dict) -> str:
        """执行 LLM 请求的工具调用，返回结果文本。

        工具执行失败时返回错误信息字符串（而非抛异常），
        让 LLM 能根据错误调整策略。

        Args:
            name: 工具名称
            args: 工具参数

        Returns:
            工具执行结果文本
        """
        try:
            if name == "search_code":
                result = self._navigation.search_code(
                    args.get("query", ""),
                    limit=args.get("limit", 10),
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "search_keyword":
                result = self._navigation.search_keyword(
                    args.get("keyword", ""),
                    limit=args.get("limit", 10),
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "read_file":
                return self._execute_read_file(args)

            if name == "trace_callers":
                result = self._navigation.trace_callers(
                    args.get("function_name", "")
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "trace_callees":
                result = self._navigation.trace_callees(
                    args.get("function_name", "")
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "get_symbols":
                result = self._navigation.get_symbols(
                    args.get("file_path", "")
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "get_file_tree":
                result = self._navigation.get_file_tree(
                    args.get("directory", "")
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "get_function_location":
                result = self._navigation.get_function_location(
                    args.get("function_name", "")
                )
                return json.dumps(result, ensure_ascii=False)

            if name == "get_class_hierarchy":
                result = self._navigation.get_class_hierarchy(
                    args.get("class_name", "")
                )
                return json.dumps(result, ensure_ascii=False)

            return f"未知工具: {name}"

        except Exception as e:
            logger.warning(f"工具执行失败 tool={name}: {e}")
            return f"工具执行出错 ({name}): {e}"

    def _execute_read_file(self, args: dict) -> str:
        """执行 read_file 工具，处理大文件分块读取逻辑。

        - 若指定了 start_line/end_line，则按行范围读取（带行号）
        - 否则读取完整文件；若文件过大则返回行数和符号概览，
          引导 LLM 使用行范围精确读取

        Args:
            args: 工具参数

        Returns:
            文件内容或概览信息
        """
        file_path = args.get("file_path", "")
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        # 指定了行范围，直接按范围读取
        if start_line is not None and end_line is not None:
            return self._navigation.read_file_range(
                file_path, int(start_line), int(end_line)
            )

        # 未指定行范围，读取完整文件
        content = self._navigation.read_file(file_path)

        # 大文件处理：超过阈值时返回概览，引导 LLM 分块读取
        if len(content) > self._config.max_tokens_per_read:
            line_count = self._navigation.get_file_line_count(file_path)
            symbols = self._navigation.get_symbols(file_path)
            overview = (
                f"文件较大（{line_count} 行，{len(content)} 字符），无法一次性返回完整内容。\n"
                f"请使用 read_file 工具并指定 start_line 和 end_line 参数来读取需要的部分。\n\n"
                f"文件行数: {line_count}\n"
                f"符号概览:\n{json.dumps(symbols, ensure_ascii=False, indent=2)}"
            )
            return overview

        return content

    # ------------------------------------------------------------------ #
    #  提示构建与结果解析
    # ------------------------------------------------------------------ #

    def _build_system_prompt(
        self, project_id: str, memories: list[dict]
    ) -> str:
        """构建系统提示，包含角色设定、分析策略、输出格式和相关记忆。

        Args:
            project_id: 项目 ID
            memories: 检索到的相关记忆列表

        Returns:
            系统提示文本
        """
        prompt = (
            "你是一位资深代码分析专家，擅长深入解读源码的逻辑和设计意图。\n"
            "你可以使用一系列代码导航工具来搜索和阅读代码，"
            "请像工程师一样逐步分析。\n\n"
            "## 分析策略\n"
            "1. 先用 search_code 或 search_keyword 搜索定位相关代码\n"
            "2. 用 read_file 阅读关键代码（大文件可先看概览再按行范围读取）\n"
            "3. 用 trace_callers / trace_callees 追踪调用链，理解数据流\n"
            "4. 用 get_symbols 快速概览文件结构\n"
            "5. 用 get_function_location / get_class_hierarchy 定位定义和继承关系\n\n"
            "请逐步分析，每一步先思考再行动。最终给出完整、清晰的逻辑解读，"
            "解释代码的工作原理、设计意图和关键流程。\n\n"
            "## 输出格式\n"
            "在最终回答中，请先写出完整的逻辑解读文本，"
            "然后在末尾用以下格式列出涉及的代码位置：\n\n"
            "<code_locations>\n"
            '[{"file_path": "路径", "lines": "起始-结束", "desc": "该代码片段的作用说明"}]\n'
            "</code_locations>\n\n"
            '其中 lines 用 "起始-结束" 格式表示行号范围（如 "10-30"）。'
            "如果没有明确的代码位置，可以输出空数组。\n"
        )

        # 追加相关记忆
        if memories:
            memory_lines = []
            for i, mem in enumerate(memories, 1):
                memory_lines.append(
                    f"{i}. [{mem.get('type', 'memory')}] {mem.get('content', '')}"
                )
            prompt += (
                f"\n## 相关历史记忆\n"
                f"以下是与本次问题相关的历史记忆，供参考：\n"
                + "\n".join(memory_lines)
                + "\n"
            )

        prompt += f"\n当前分析的项目 ID: {project_id}"
        return prompt

    def _parse_final_answer(
        self, answer: str
    ) -> tuple[str, list[dict]]:
        """从 LLM 最终回答中提取解读文本和代码位置。

        LLM 被要求在回答末尾用 <code_locations>...</code_locations> 标签
        包裹 JSON 数组。本方法提取该 JSON 并从解读文本中移除该标签块。

        Args:
            answer: LLM 的最终回答文本

        Returns:
            (explanation, code_locations) 元组
        """
        locations: list[dict] = []

        # 匹配 <code_locations>...</code_locations> 块
        pattern = re.compile(
            r"<code_locations>\s*(.*?)\s*</code_locations>",
            re.DOTALL,
        )
        match = pattern.search(answer)

        if match:
            json_str = match.group(1).strip()
            # 尝试解析 JSON
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, list):
                    locations = parsed
            except json.JSONDecodeError:
                # 尝试提取第一个 JSON 数组
                arr_match = re.search(r"\[.*\]", json_str, re.DOTALL)
                if arr_match:
                    try:
                        parsed = json.loads(arr_match.group(0))
                        if isinstance(parsed, list):
                            locations = parsed
                    except json.JSONDecodeError:
                        logger.warning("无法解析 code_locations JSON")
                else:
                    logger.warning("code_locations 中未找到 JSON 数组")

            # 从解读文本中移除标签块
            explanation = pattern.sub("", answer).strip()
        else:
            # 没有标签块，尝试从文本中提取 file_path 和行号
            explanation = answer.strip()
            locations = self._extract_locations_from_text(answer)

        return explanation, locations

    def _extract_locations_from_text(self, text: str) -> list[dict]:
        """从自然语言文本中启发式提取代码位置（后备方案）。

        匹配形如 "src/foo.py:10-30" 或 "src/foo.py:L10-L30" 的模式。

        Args:
            text: 回答文本

        Returns:
            代码位置列表
        """
        locations: list[dict] = []
        # 匹配 文件路径:数字-数字 的模式
        pattern = re.compile(
            r"([\w./\-]+\.\w+)\s*[:：]\s*(\d+)\s*[-–]\s*(\d+)"
        )
        for match in pattern.finditer(text):
            file_path = match.group(1)
            start = match.group(2)
            end = match.group(3)
            locations.append(
                {
                    "file_path": file_path,
                    "lines": f"{start}-{end}",
                    "desc": "",
                }
            )
        return locations

    # ------------------------------------------------------------------ #
    #  记忆相关
    # ------------------------------------------------------------------ #

    async def _retrieve_memories(
        self, project_id: str, question: str
    ) -> list[dict]:
        """从记忆服务检索与问题相关的历史记忆。

        Args:
            project_id: 项目 ID
            question: 用户问题

        Returns:
            记忆列表，服务不可用时返回空列表
        """
        try:
            memories = await self._memory.retrieve(
                project_id, question, limit=5
            )
            logger.debug(f"检索到 {len(memories)} 条相关记忆")
            return memories
        except Exception as e:
            logger.warning(f"检索记忆失败，将忽略记忆上下文: {e}")
            return []

    async def _store_qa_memory(
        self, project_id: str, question: str, explanation: str
    ) -> bool:
        """将问答摘要存入记忆服务，供后续检索复用。

        Args:
            project_id: 项目 ID
            question: 原始问题
            explanation: 解读文本

        Returns:
            是否存储成功
        """
        # 摘要内容：问题 + 解读前 500 字
        summary = explanation[:500]
        content = f"问题: {question}\n\n解读摘要: {summary}"
        key = f"qa_{self._simple_hash(question)}"
        tags = [project_id, "qa"] + self._extract_keywords(question)

        try:
            result = await self._memory.store(
                project_id,
                key,
                content,
                memory_type="qa_memory",
                tags=tags,
            )
            success = result.get("success", True)
            logger.info(f"问答记忆已存储 key={key}, success={success}")
            return bool(success)
        except Exception as e:
            logger.warning(f"存储问答记忆失败: {e}")
            return False

    @staticmethod
    def _simple_hash(text: str) -> str:
        """对文本生成简单的十六进制哈希，用作记忆 key。

        Args:
            text: 输入文本

        Returns:
            8 位十六进制哈希字符串
        """
        import hashlib

        return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
        """从文本中提取关键词，用作记忆标签。

        简单分词：按非字母数字汉字字符分割，过滤过短词。

        Args:
            text: 输入文本
            max_keywords: 最大关键词数

        Returns:
            关键词列表
        """
        words = re.findall(r"[\w\u4e00-\u9fff]+", text)
        keywords = [w for w in words if len(w) > 1]
        return keywords[:max_keywords]

    # ------------------------------------------------------------------ #
    #  辅助方法
    # ------------------------------------------------------------------ #

    async def _call_llm(
        self,
        messages: list,
        tools: list | None = None,
        model: str | None = None,
    ) -> Any:
        """调用 LLM chat completion API。

        Args:
            messages: 消息列表
            tools: 工具定义列表，None 表示不启用工具
            model: 模型名称，None 时使用 config.model

        Returns:
            OpenAI ChatCompletion 响应对象
        """
        use_model = model or self._config.model
        kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "temperature": self._config.temperature,
        }
        if tools:
            kwargs["tools"] = tools

        logger.debug(
            f"调用 LLM model={use_model}, "
            f"messages={len(messages)}, "
            f"tools={len(tools) if tools else 'none'}"
        )
        return await self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _format_action(name: str, args: dict) -> str:
        """将工具调用格式化为可读字符串，用于推理轨迹。

        Args:
            name: 工具名称
            args: 参数字典

        Returns:
            如 "search_code(query='user login')"
        """
        parts = [f"{k}={v!r}" for k, v in args.items()]
        return f"{name}({', '.join(parts)})"

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """截断文本到指定长度，超出时追加截断提示。

        Args:
            text: 原始文本
            limit: 最大字符数

        Returns:
            截断后的文本
        """
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[结果已截断，共 {len(text)} 字符]"
