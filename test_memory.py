"""测试 openviking 记忆服务完整流程（存储→检索→删除）。"""
import asyncio
import sys

from loguru import logger

from src.core.memory_service import MemoryService
from src.models.config import load_config

logger.remove()
logger.add(sys.stderr, level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>")


async def main():
    config = load_config("config/config.yaml")
    svc = MemoryService(config.memory)

    logger.info("=== 1. 存储记忆 ===")
    result = await svc.store(
        project_id="eip",
        key="permission_control_analysis",
        content="EIP项目外销合同权限控制逻辑：基于@RequireDataFilter注解+AOP切面+MyBatis拦截器实现数据权限过滤。"
               "用户查询外销合同时，系统根据当前用户角色和部门动态追加SQL过滤条件。",
        memory_type="code_memory",
        tags=["eip", "permission", "export_contract"],
    )
    logger.info(f"存储结果: {result.get('status')}: {str(result)[:300]}")

    logger.info("\n=== 2. 检索记忆 ===")
    memories = await svc.retrieve(
        project_id="eip",
        query="外销合同权限控制",
        tags=["eip"],
        limit=5,
    )
    logger.info(f"检索到 {len(memories)} 条记忆:")
    for i, m in enumerate(memories, 1):
        logger.info(f"  [{i}] score={m.get('score', 0):.2f}")
        logger.info(f"      uri={m.get('uri', '')}")
        logger.info(f"      abstract={m.get('abstract', '')[:200]}")

    logger.info("\n=== 3. 存储全局记忆 ===")
    result = await svc.store_global(
        key="system_config",
        content="SourceCodeQuery MCP服务配置：LLM使用qwen3.7-plus，GitLab通过账号密码认证。",
        tags=["config", "global"],
    )
    logger.info(f"全局存储结果: {result.get('status')}")

    logger.info("\n=== 4. 检索全局记忆 ===")
    memories = await svc.retrieve_global(
        query="MCP服务配置",
        limit=3,
    )
    logger.info(f"全局检索到 {len(memories)} 条记忆:")
    for i, m in enumerate(memories, 1):
        logger.info(f"  [{i}] score={m.get('score', 0):.2f}")
        logger.info(f"      abstract={m.get('abstract', '')[:200]}")

    logger.info("\n=== 5. 列出项目记忆 ===")
    memories = await svc.list_memories("eip", limit=10)
    logger.info(f"EIP 项目记忆列表 ({len(memories)} 条):")
    for i, m in enumerate(memories, 1):
        logger.info(f"  [{i}] {m.get('uri', '')}")

    logger.info("\n=== 测试完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
