"""GitLab API 客户端封装，基于 python-gitlab 库。

使用账号密码通过 OAuth2 密码授权获取访问令牌，
通过项目路径（从 source_url 提取）访问仓库资源。
支持 git clone 方式快速拉取整个仓库。
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import gitlab
import httpx
from gitlab.exceptions import GitlabError
from loguru import logger

from src.models.config import GitLabConfig


class GitLabClient:
    """GitLab API 客户端，封装常用仓库操作。

    通过 python-gitlab 库与局域网 GitLab 实例交互，
    使用账号密码进行 OAuth2 认证，通过项目路径访问仓库资源。
    """

    def __init__(self, config: GitLabConfig):
        """初始化 GitLab 客户端。

        使用账号密码通过 OAuth2 密码授权获取访问令牌。

        Args:
            config: GitLab 连接配置
        """
        self._config = config
        self._url = config.url.rstrip("/")

        # 通过 OAuth2 密码授权获取访问令牌
        token = self._authenticate(
            config.url, config.username, config.password
        )
        self._client = gitlab.Gitlab(
            url=config.url, oauth_token=token
        )
        self._oauth_token = token
        logger.debug(f"初始化 GitLab 客户端: url={config.url}")

    def _build_clone_url(self, source_url: str) -> str:
        """构建带认证信息的 git clone URL。

        将 OAuth2 token 注入到 URL 中，格式：
        http://oauth2:{token}@host/path.git

        Args:
            source_url: 原始源码 URL

        Returns:
            带认证信息的 clone URL
        """
        parsed = urllib.parse.urlparse(source_url)
        # 使用 oauth2 用户名 + token 认证
        auth_url = parsed._replace(
            netloc=f"oauth2:{self._oauth_token}@{parsed.netloc}"
        )
        return urllib.parse.urlunparse(auth_url)

    def clone_repository(
        self,
        source_url: str,
        target_dir: Path,
        branch: str = "main",
    ) -> bool:
        """通过 git clone 拉取整个仓库到本地目录。

        如果目标目录已存在且是 git 仓库，则执行 git pull 更新。
        如果目标目录存在但非 git 仓库，先删除再克隆。

        Args:
            source_url: GitLab 项目源码 URL
            target_dir: 本地目标目录
            branch: 要拉取的分支

        Returns:
            克隆/更新成功返回 True，失败返回 False
        """
        clone_url = self._build_clone_url(source_url)
        target_dir = Path(target_dir)

        # 如果目标目录已存在且是 git 仓库，执行 pull
        git_dir = target_dir / ".git"
        if git_dir.exists():
            logger.info(f"仓库已存在，执行 git pull: {target_dir}")
            try:
                # 切换到目标分支并拉取最新代码
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=str(target_dir),
                    capture_output=True,
                    timeout=120,
                )
                subprocess.run(
                    ["git", "checkout", branch],
                    cwd=str(target_dir),
                    capture_output=True,
                    timeout=30,
                )
                result = subprocess.run(
                    ["git", "pull", "origin", branch],
                    cwd=str(target_dir),
                    capture_output=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    logger.info(f"git pull 成功: {target_dir}")
                    return True
                else:
                    logger.error(
                        f"git pull 失败: {result.stderr.decode('utf-8', errors='replace')}"
                    )
                    return False
            except subprocess.TimeoutExpired:
                logger.error(f"git pull 超时: {target_dir}")
                return False
            except Exception as e:
                logger.error(f"git pull 异常: {e}")
                return False

        # 目标目录不存在或非 git 仓库，执行 clone
        if target_dir.exists():
            # 非空目录且不是 git 仓库，先清空
            shutil.rmtree(target_dir)

        target_dir.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"开始 git clone: {source_url} -> {target_dir}")
        try:
            result = subprocess.run(
                [
                    "git", "clone",
                    "--branch", branch,
                    "--depth", "1",  # 浅克隆，只取最新一次提交，加速拉取
                    clone_url,
                    str(target_dir),
                ],
                capture_output=True,
                timeout=300,
            )
            if result.returncode == 0:
                logger.info(f"git clone 成功: {target_dir}")
                return True
            else:
                stderr = result.stderr.decode("utf-8", errors="replace")
                logger.error(f"git clone 失败: {stderr}")
                # 浅克隆失败时尝试普通克隆
                logger.info("尝试普通克隆（非浅克隆）...")
                result = subprocess.run(
                    [
                        "git", "clone",
                        "--branch", branch,
                        clone_url,
                        str(target_dir),
                    ],
                    capture_output=True,
                    timeout=600,
                )
                if result.returncode == 0:
                    logger.info(f"普通克隆成功: {target_dir}")
                    return True
                else:
                    logger.error(
                        f"普通克隆也失败: {result.stderr.decode('utf-8', errors='replace')}"
                    )
                    return False
        except subprocess.TimeoutExpired:
            logger.error(f"git clone 超时: {target_dir}")
            return False
        except Exception as e:
            logger.error(f"git clone 异常: {e}")
            return False

    @staticmethod
    def _authenticate(url: str, username: str, password: str) -> str:
        """通过 OAuth2 密码授权获取访问令牌。

        Args:
            url: GitLab 实例地址
            username: 用户名
            password: 密码

        Returns:
            OAuth2 访问令牌

        Raises:
            RuntimeError: 认证失败时抛出
        """
        token_url = f"{url.rstrip('/')}/oauth/token"
        try:
            resp = httpx.post(
                token_url,
                data={
                    "grant_type": "password",
                    "username": username,
                    "password": password,
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                logger.error(
                    f"GitLab 认证失败: status={resp.status_code}, "
                    f"body={resp.text}"
                )
                raise RuntimeError(
                    f"GitLab 认证失败: HTTP {resp.status_code}"
                )
            token = resp.json().get("access_token")
            if not token:
                raise RuntimeError("GitLab 认证响应中缺少 access_token")
            logger.info("GitLab 认证成功")
            return token
        except httpx.HTTPError as e:
            logger.error(f"GitLab 认证网络错误: {e}")
            raise RuntimeError(f"GitLab 认证网络错误: {e}") from e

    @staticmethod
    def _encode_project_path(project_path: str) -> str:
        """将项目路径 URL 编码，用于 GitLab API 调用。

        Args:
            project_path: 项目路径，如 "group/subgroup/project"

        Returns:
            URL 编码后的路径，如 "group%2Fsubgroup%2Fproject"
        """
        return urllib.parse.quote(project_path, safe="")

    def list_projects(self) -> list[dict]:
        """列出所有已配置的项目信息。

        Returns:
            项目信息字典列表，每个字典包含 id、name、source_url、
            default_branch、language 字段
        """
        projects = [
            {
                "id": p.id,
                "name": p.name,
                "source_url": p.source_url,
                "default_branch": p.default_branch,
                "language": p.language,
            }
            for p in self._config.projects
        ]
        logger.info(f"列出已配置项目，共 {len(projects)} 个")
        return projects

    def _get_project(self, project_path: str):
        """获取项目对象（内部方法）。

        python-gitlab 内部会自动 URL 编码项目路径，无需手动编码。

        Args:
            project_path: 项目路径，如 "group/project"

        Returns:
            python-gitlab 的 Project 对象
        """
        return self._client.projects.get(project_path)

    def get_file_tree(
        self,
        project_path: str,
        ref: str = "main",
        path: str = "",
        recursive: bool = True,
    ) -> list[dict]:
        """获取仓库文件树。

        Args:
            project_path: 项目路径，如 "group/project"
            ref: 分支或标签名
            path: 仓库内子路径，空字符串表示根目录
            recursive: 是否递归获取

        Returns:
            文件树项列表，每项包含 path、type("blob"/"tree")、mode 字段
        """
        try:
            project = self._get_project(project_path)
            items = project.repository_tree(
                ref=ref, path=path, recursive=recursive, get_all=True
            )
            result = [
                {"path": item["path"], "type": item["type"], "mode": item["mode"]}
                for item in items
            ]
            logger.info(
                f"获取文件树: project={project_path}, ref={ref}, "
                f"path={path}, 共 {len(result)} 项"
            )
            return result
        except GitlabError as e:
            logger.error(
                f"获取文件树失败: project={project_path}, ref={ref}, "
                f"path={path}, error={e}"
            )
            raise RuntimeError(f"获取文件树失败: {e}") from e

    def get_file_content(
        self, project_path: str, file_path: str, ref: str = "main"
    ) -> str:
        """获取文件内容。

        Args:
            project_path: 项目路径
            file_path: 文件在仓库中的路径
            ref: 分支或标签名

        Returns:
            文件文本内容
        """
        try:
            project = self._get_project(project_path)
            f = project.files.get(file_path, ref=ref)
            # GitLab API 返回的文件内容通常是 base64 编码，需要解码
            if getattr(f, "encoding", None) == "base64":
                content = base64.b64decode(f.content).decode("utf-8")
            else:
                content = f.content
            logger.info(
                f"获取文件内容: project={project_path}, path={file_path}, "
                f"ref={ref}, 大小={len(content)}"
            )
            return content
        except GitlabError as e:
            logger.error(
                f"获取文件内容失败: project={project_path}, "
                f"path={file_path}, ref={ref}, error={e}"
            )
            raise RuntimeError(f"获取文件内容失败: {e}") from e

    def get_commits(
        self,
        project_path: str,
        ref: str = "main",
        since: str | None = None,
        per_page: int = 100,
    ) -> list[dict]:
        """获取提交列表。

        Args:
            project_path: 项目路径
            ref: 分支或标签名
            since: ISO 格式时间字符串，仅返回此时间之后的提交
            per_page: 每页数量

        Returns:
            提交信息字典列表
        """
        try:
            project = self._get_project(project_path)
            kwargs: dict = {"ref_name": ref, "get_all": True, "per_page": per_page}
            if since:
                kwargs["since"] = since
            commits = project.commits.list(**kwargs)
            result = [c.asdict() for c in commits]
            logger.info(
                f"获取提交列表: project={project_path}, ref={ref}, "
                f"since={since}, 共 {len(result)} 条"
            )
            return result
        except GitlabError as e:
            logger.error(
                f"获取提交列表失败: project={project_path}, ref={ref}, "
                f"since={since}, error={e}"
            )
            raise RuntimeError(f"获取提交列表失败: {e}") from e

    def get_commit_diff(
        self, project_path: str, commit_sha: str
    ) -> list[dict]:
        """获取某次提交的 diff。

        Args:
            project_path: 项目路径
            commit_sha: 提交 SHA

        Returns:
            diff 字典列表
        """
        try:
            project = self._get_project(project_path)
            commit = project.commits.get(commit_sha)
            diffs = commit.diff()
            result = [
                {
                    "old_path": d["old_path"],
                    "new_path": d["new_path"],
                    "new_file": d["new_file"],
                    "deleted_file": d["deleted_file"],
                    "diff": d["diff"],
                }
                for d in diffs
            ]
            logger.info(
                f"获取提交 diff: project={project_path}, "
                f"sha={commit_sha}, 共 {len(result)} 个文件"
            )
            return result
        except GitlabError as e:
            logger.error(
                f"获取提交 diff 失败: project={project_path}, "
                f"sha={commit_sha}, error={e}"
            )
            raise RuntimeError(f"获取提交 diff 失败: {e}") from e

    def get_last_commit_sha(
        self, project_path: str, ref: str = "main"
    ) -> str | None:
        """获取指定分支最新 commit SHA。

        Args:
            project_path: 项目路径
            ref: 分支名

        Returns:
            最新 commit SHA 字符串，若分支无提交则返回 None
        """
        try:
            project = self._get_project(project_path)
            branch = project.branches.get(ref)
            commit = branch.commit
            if not commit:
                logger.warning(
                    f"分支无提交记录: project={project_path}, ref={ref}"
                )
                return None
            sha = commit["id"]
            logger.info(
                f"获取最新 commit SHA: project={project_path}, "
                f"ref={ref}, sha={sha}"
            )
            return sha
        except GitlabError as e:
            logger.error(
                f"获取最新 commit SHA 失败: project={project_path}, "
                f"ref={ref}, error={e}"
            )
            raise RuntimeError(f"获取最新 commit SHA 失败: {e}") from e

    def get_commit_diffs_between(
        self, project_path: str, from_sha: str, to_sha: str
    ) -> list[dict]:
        """获取两个 commit 之间的所有变更文件及 diff。

        Args:
            project_path: 项目路径
            from_sha: 起始 commit SHA
            to_sha: 目标 commit SHA

        Returns:
            diff 字典列表
        """
        try:
            project = self._get_project(project_path)
            comparison = project.repository_compare(from_sha, to_sha)
            if isinstance(comparison, dict):
                diffs = comparison.get("diffs", [])
            else:
                diffs = getattr(comparison, "diffs", [])
            result = [
                {
                    "old_path": d["old_path"],
                    "new_path": d["new_path"],
                    "new_file": d["new_file"],
                    "deleted_file": d["deleted_file"],
                    "diff": d["diff"],
                }
                for d in diffs
            ]
            logger.info(
                f"获取 commit 间 diff: project={project_path}, "
                f"from={from_sha}, to={to_sha}, 共 {len(result)} 个文件"
            )
            return result
        except GitlabError as e:
            logger.error(
                f"获取 commit 间 diff 失败: project={project_path}, "
                f"from={from_sha}, to={to_sha}, error={e}"
            )
            raise RuntimeError(f"获取 commit 间 diff 失败: {e}") from e
