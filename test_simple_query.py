"""简化测试：本地搜索 + LLM 分析（不依赖 function calling）。

用法: python test_simple_query.py
"""

import asyncio
import json
import sys
from pathlib import Path

from loguru import logger
from openai import AsyncOpenAI

from src.core.code_storage import CodeStorage
from src.core.scheduler import CodeSyncScheduler
from src.models.config import load_config

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>",
)

# 搜索关键词
KEYWORDS = ["外销", "权限", "客户"]
# 最多读取的文件数
MAX_FILES = 5
# 每个文件最多读取的字符数
MAX_CHARS_PER_FILE = 4000


async def main():
    logger.info("=" * 60)
    logger.info("简化查询测试（本地搜索 + LLM 分析）")
    logger.info("=" * 60)

    # 1. 加载配置
    config = load_config("config/config.yaml")
    project_id = "eip"

    # 2. 初始化代码存储
    storage = CodeStorage(base_data_dir=Path("data/projects"), project_id=project_id)
    all_files = storage.get_all_files()
    code_files = [f for f in all_files if CodeSyncScheduler._is_code_file(f)]
    logger.info(f"项目 {project_id}: 总文件 {len(all_files)}, 代码文件 {len(code_files)}")

    # 3. 本地关键词搜索
    logger.info("-" * 40)
    logger.info(f"关键词搜索: {KEYWORDS}")

    # 每个关键词找到的文件
    matched_files: dict[str, list[int]] = {}  # file_path -> [line numbers]
    file_scores: dict[str, int] = {}  # file_path -> match count

    for file_path in code_files:
        try:
            content = storage.read_file(file_path)
        except Exception:
            continue

        lines = content.splitlines()
        score = 0
        matching_lines: list[int] = []

        for i, line in enumerate(lines, 1):
            for kw in KEYWORDS:
                if kw.lower() in line.lower():
                    score += 1
                    matching_lines.append(i)
                    break  # 每行只算一次

        if score > 0:
            matched_files[file_path] = matching_lines
            file_scores[file_path] = score

    # 按匹配数排序，取前 MAX_FILES 个
    sorted_files = sorted(file_scores.items(), key=lambda x: -x[1])
    top_files = sorted_files[:MAX_FILES]

    logger.info(f"匹配到 {len(matched_files)} 个文件，取前 {len(top_files)} 个:")
    for f, score in top_files:
        logger.info(f"  {f} (匹配 {score} 处)")

    # 4. 读取匹配文件的内容
    code_context = ""
    for file_path, _ in top_files:
        try:
            content = storage.read_file(file_path)
            if len(content) > MAX_CHARS_PER_FILE:
                content = content[:MAX_CHARS_PER_FILE] + "\n... (文件过长，已截断)"
            code_context += f"\n{'='*60}\n文件: {file_path}\n{'='*60}\n{content}\n"
        except Exception as e:
            logger.warning(f"读取文件失败: {file_path}: {e}")

    logger.info(f"代码上下文总长度: {len(code_context)} 字符")

    # 5. 构建 LLM 请求
    system_prompt = (
        "你是一位资深代码分析专家。请根据以下源码文件，分析用户询问的功能逻辑。\n"
        "要求：\n"
        "1. 详细解读逻辑流程\n"
        "2. 给出关键代码的文件路径和行号\n"
        "3. 如果信息不足，明确说明需要查看哪些额外文件\n\n"
        "请用中文回答。"
    )

    user_prompt = (
        f"问题：外销合同的客户权限控制逻辑是怎么实现的？\n\n"
        f"以下是从项目中搜索到的相关源码文件：\n{code_context}"
    )

    logger.info("-" * 40)
    logger.info("调用 LLM 分析...")
    logger.info(f"model={config.llm.model}, base_url={config.llm.base_url}")

    # 6. 调用 LLM
    client = AsyncOpenAI(
        api_key=config.llm.api_key,
        base_url=config.llm.base_url,
    )

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=config.llm.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=3000,
            ),
            timeout=180,
        )

        answer = response.choices[0].message.content or "(无回答)"

        # 7. 输出结果
        print("\n" + "=" * 60)
        print("【LLM 分析结果】")
        print("=" * 60)
        print(answer)
        print("\n" + "=" * 60)
        print(f"消耗 token: prompt={response.usage.prompt_tokens}, "
              f"completion={response.usage.completion_tokens}, "
              f"total={response.usage.total_tokens}")
        print("=" * 60)
        logger.info("分析完成！")

    except asyncio.TimeoutError:
        logger.error("LLM 调用超时（120秒），请检查 API 连通性和模型可用性")
    except Exception as e:
        logger.error(f"LLM 调用失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
