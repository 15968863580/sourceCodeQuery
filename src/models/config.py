"""配置模型定义，基于 Pydantic Settings。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    """MCP Server 传输配置。"""

    transport: str = "stdio"  # stdio | sse
    port: int = 8080


class ProjectConfig(BaseModel):
    """单个 GitLab 项目配置。"""

    id: str
    name: str
    source_url: str  # GitLab 项目源码 URL，如 https://gitlab.example.com/group/project
    default_branch: str = "main"
    language: str = "java"

    def get_project_path(self) -> str:
        """从 source_url 中提取项目路径（group/project 形式）。

        Returns:
            项目路径字符串，如 "group/subgroup/project"
        """
        parsed = urlparse(self.source_url)
        path = parsed.path.strip("/")
        # 去掉可能的 .git 后缀
        if path.endswith(".git"):
            path = path[:-4]
        return path


class GitLabConfig(BaseModel):
    """GitLab 连接配置。"""

    url: str  # GitLab 实例地址，如 https://gitlab.example.com
    username: str
    password: str
    projects: list[ProjectConfig] = Field(default_factory=list)


class LLMConfig(BaseModel):
    """大模型配置。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"  # 主模型 ID，用于代码深度解读
    summary_model: str = "gpt-4o-mini"  # 摘要模型 ID，用于同步摘要生成
    temperature: float = 0.3
    max_reasoning_steps: int = 15
    max_tokens_per_read: int = 8000


class EmbeddingConfig(BaseModel):
    """Embedding 模型配置。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "text-embedding-3-small"


class IndexConfig(BaseModel):
    """代码索引配置。"""

    storage_path: str = "./data/index"
    chunk_size: int = 500
    chunk_overlap: int = 50


class SchedulerConfig(BaseModel):
    """调度器配置。"""

    sync_interval_cron: str = "*/30 * * * *"
    full_sync_on_start: bool = True


class ProjectMemoryConfig(BaseModel):
    """单个项目的记忆 namespace 配置。"""

    namespace: str


class MemoryConfig(BaseModel):
    """openviking 记忆服务配置。"""

    openviking_endpoint: str = "http://openviking.local:8000"
    projects: dict[str, ProjectMemoryConfig] = Field(default_factory=dict)
    global_namespace: str = "global"


class AppConfig(BaseModel):
    """顶层应用配置。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    gitlab: GitLabConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    data_dir: str = "./data"

    @field_validator("gitlab", mode="before")
    @classmethod
    def validate_gitlab(cls, v):
        if v is None:
            raise ValueError("gitlab 配置不能为空")
        return v

    @property
    def projects_by_id(self) -> dict[str, ProjectConfig]:
        """按 id 索引的项目字典。"""
        return {p.id: p for p in self.gitlab.projects}

    def get_project(self, project_id: str) -> Optional[ProjectConfig]:
        """根据 id 获取项目配置。"""
        return self.projects_by_id.get(project_id)

    def get_project_data_dir(self, project_id: str) -> Path:
        """获取项目数据目录路径。"""
        return Path(self.data_dir) / "projects" / project_id

    def get_project_memory_namespace(self, project_id: str) -> str:
        """获取项目的记忆 namespace。"""
        proj_mem = self.memory.projects.get(project_id)
        if proj_mem:
            return proj_mem.namespace
        return f"proj_{project_id}"


def _expand_env(value: str) -> str:
    """展开字符串中的 ${ENV_VAR} 环境变量。"""
    result = value
    # 多次替换以处理嵌套情况
    for _ in range(10):
        if "${" not in result:
            break
        start = result.find("${")
        end = result.find("}", start)
        if end == -1:
            break
        var_name = result[start + 2 : end]
        env_value = os.environ.get(var_name, "")
        result = result[:start] + env_value + result[end + 1 :]
    return result


def _expand_env_recursive(obj):
    """递归展开配置中所有字符串值的环境变量。"""
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_recursive(item) for item in obj]
    return obj


def load_config(config_path: str | Path = "config/config.yaml") -> AppConfig:
    """从 YAML 文件加载配置，自动展开环境变量。

    Args:
        config_path: 配置文件路径

    Returns:
        AppConfig 实例
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _expand_env_recursive(raw)
    return AppConfig(**raw)
