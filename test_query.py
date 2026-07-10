"""测试 MCP 查询：查询 EIP 项目的源码逻辑。

用法: python test_query.py
"""

import asyncio
import json
import sys

from loguru import logger

from src.core.code_navigation import CodeNavigation
from src.core.memory_service import MemoryService
from src.core.project_manager import ProjectManager
from src.core.understanding_engine import UnderstandingEngine
from src.models.config import load_config

# 配置日志
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{name}</cyan> | <level>{message}</level>",
)


async def main():
    logger.info("=" * 60)
    logger.info("MCP 查询测试")
    logger.info("=" * 60)

    # 1. 加载配置
    config = load_config("config/config.yaml")
    logger.info(f"配置加载完成, LLM model={config.llm.model}")

    # 2. 初始化核心模块
    memory_service = MemoryService(config.memory)
    project_manager = ProjectManager(config, memory_service)

    # 确保使用 eip 项目
    project_id = "eip"
    ctx = project_manager.get_project(project_id)
    if ctx is None:
        logger.error(f"项目 {project_id} 不存在")
        return

    logger.info(f"项目上下文: {project_id}, storage={ctx.storage._base_path}")

    # 确保调用图已构建
    project_manager.ensure_call_graph(project_id)
    if ctx.call_graph is None:
        logger.warning("调用图构建失败，继续执行（调用链追踪不可用）")
    else:
        func_count = len(ctx.call_graph._functions)
        logger.info(f"调用图: {func_count} 个函数")

    # 3. 创建代码导航和解读引擎
    navigation = CodeNavigation(
        storage=ctx.storage,
        index=ctx.index,
        call_graph=ctx.call_graph,
    )
    engine = UnderstandingEngine(
        config=config.llm,
        navigation=navigation,
        memory_service=memory_service,
    )
    logger.info("解读引擎初始化完成")

    # 4. 执行查询
    question = "外销合同的客户权限控制逻辑"
    logger.info(f"查询问题: {question}")
    logger.info("-" * 60)

    result = await engine.understand(question, project_id)

    # 5. 输出结果
    logger.info("=" * 60)
    logger.info("查询结果：")
    logger.info("=" * 60)

    print("\n" + "=" * 60)
    print("【逻辑解读】")
    print("=" * 60)
    print(result.get("explanation", "(无)"))

    print("\n" + "=" * 60)
    print("【代码位置】")
    print("=" * 60)
    locations = result.get("code_locations", [])
    if locations:
        for i, loc in enumerate(locations, 1):
            print(f"\n  [{i}] 文件: {loc.get('file', 'unknown')}")
            if loc.get("lines"):
                print(f"      行号: {loc['lines']}")
            if loc.get("description"):
                print(f"      说明: {loc['description']}")
    else:
        print("  (无具体代码位置)")

    print("\n" + "=" * 60)
    print("【推理过程】")
    print("=" * 60)
    trace = result.get("reasoning_trace", [])
    for step in trace:
        print(f"\n  Step {step.get('step', '?')}:")
        print(f"    思考: {step.get('thought', '')[:200]}")
        action = step.get("action", "")
        if action:
            print(f"    动作: {action}")
        result_summary = step.get("result_summary", "")
        if result_summary:
            print(f"    结果: {result_summary[:200]}")

    print(f"\n  记忆存储: {'成功' if result.get('memory_stored') else '失败/跳过'}")
    print("\n" + "=" * 60)
    print("查询完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
