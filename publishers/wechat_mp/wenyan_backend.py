"""备选发布后端：``@wenyan-md/cli``（caol64/wenyan-cli，Apache-2.0）子进程直发草稿箱。

为什么保留这条路
----------------
- 它自带 markdown → 内联样式 HTML 的渲染、正文配图与封面自动上传，省掉我们自己拼
  ``media/uploadimg`` + ``material/add_material``；
- 它支持 **Client-Server 模式**（``--server``），可以把出网这一跳挪到有固定 IP 的机器上，
  是 40164（IP 白名单）在动态 IP 环境下的现成解法。

切换方式：``settings.WECHAT_BACKEND = "api" | "wenyan"``（环境变量 ``WECHAT_BACKEND``）。

安全约束
--------
凭据**只经环境变量**传给子进程（``WECHAT_APP_ID`` / ``WECHAT_APP_SECRET``，
上游 README 已核实），绝不出现在 argv 里——argv 对同机任何用户可见（``ps``）。
子进程的 stdout/stderr 在记日志前统一脱敏。

**未核实**：``wenyan publish`` 是否支持 ``--theme`` / ``--cover`` 等渲染类参数
（上游 README 只明确了 ``-f`` / ``--server`` / ``--api-key-file`` / ``--app-id``
与 ``--env-file``），因此本实现只使用这几个已核实的参数。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from publishers.base import (
    ContentBundle,
    PermanentError,
    PublishResult,
    RetryableError,
)
from publishers.wechat_mp.client import redact
from publishers.wechat_mp.publisher import WechatMpPublisher

logger = logging.getLogger("social_workflow.publishers.wechat_mp.wenyan")

Runner = Callable[[list[str], dict[str, str], float, Path], "subprocess.CompletedProcess[str]"]

_MEDIA_ID = re.compile(r"media[_-]?id[\"'\s:=]+([A-Za-z0-9_\-]{16,})", re.IGNORECASE)


def _default_runner(
    cmd: list[str], env: dict[str, str], timeout: float, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False
    )


class WenyanBackend:
    """``@wenyan-md/cli`` 的薄封装。"""

    def __init__(
        self,
        *,
        app_id: str = "",
        app_secret: str = "",
        node_bin: str = "npx",
        npm_spec: str = "@wenyan-md/cli",
        timeout: float = 120.0,
        server_url: str = "",
        api_key_file: str = "",
        dry_run: bool = False,
        runner: Runner | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.node_bin = node_bin
        self.npm_spec = npm_spec
        self.timeout = timeout
        self.server_url = server_url
        self.api_key_file = api_key_file
        self.dry_run = dry_run
        self._run = runner or _default_runner

    @classmethod
    def from_settings(cls, *, dry_run: bool = False, runner: Runner | None = None) -> WenyanBackend:
        from core.config import get_settings

        s = get_settings()
        return cls(
            app_id=s.wechat_app_id,
            app_secret=s.wechat_app_secret,
            node_bin=s.node_bin,
            npm_spec=s.wenyan_npm_spec,
            timeout=s.wenyan_timeout_seconds,
            server_url=s.wenyan_server_url,
            api_key_file=s.wenyan_api_key_file,
            dry_run=dry_run,
            runner=runner,
        )

    # -- 命令拼装 ---------------------------------------------------------

    def build_command(self, markdown_path: Path) -> list[str]:
        cmd = [self.node_bin]
        if Path(self.node_bin).name == "npx":
            cmd += ["-y", self.npm_spec]
        cmd += ["publish", "-f", str(markdown_path)]
        if self.app_id:
            # --app-id 只是选账号，不是密钥；AppSecret 走环境变量
            cmd += ["--app-id", self.app_id]
        if self.server_url:
            cmd += ["--server", self.server_url]
            if self.api_key_file:
                cmd += ["--api-key-file", self.api_key_file]
        return cmd

    def build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["WECHAT_APP_ID"] = self.app_id
        env["WECHAT_APP_SECRET"] = self.app_secret
        return env

    def available(self) -> bool:
        """node 运行器是否存在（不实际下载 npm 包）。"""
        return shutil.which(self.node_bin) is not None

    # -- 发布 -------------------------------------------------------------

    def publish_draft(self, markdown: str, *, title: str) -> dict[str, Any]:
        """把 markdown 写成临时文件并调用 ``wenyan publish`` 直发草稿箱。

        返回 ``{"media_id": ..., "found_media_id": bool, "stdout": ...}``。
        CLI 不保证在 stdout 打印 media_id，取不到时用内容哈希生成本地占位 id
        （``found_media_id=False``），此时对账只能靠 ``draft/batchget`` 标题匹配。
        """
        if not markdown.strip():
            raise PermanentError("wenyan 后端：markdown 正文为空", raw={"backend": "wenyan"})
        seed = hashlib.sha256(f"{title}\n{markdown}".encode()).hexdigest()[:16]
        if self.dry_run:
            logger.info("[dry_run] wenyan publish 跳过，title=%r", title)
            return {"media_id": f"dryrun-wenyan-{seed}", "found_media_id": False, "stdout": ""}
        if not self.app_id or not self.app_secret:
            raise PermanentError(
                "wenyan 后端：未配置 WECHAT_APP_ID / WECHAT_APP_SECRET", raw={"backend": "wenyan"}
            )
        if not self.available():
            raise PermanentError(
                f"wenyan 后端：找不到可执行文件 {self.node_bin!r}，请安装 Node.js",
                raw={"backend": "wenyan"},
            )

        with tempfile.TemporaryDirectory(prefix="wenyan-") as tmp:
            workdir = Path(tmp)
            md_path = workdir / "article.md"
            md_path.write_text(markdown, encoding="utf-8")
            cmd = self.build_command(md_path)
            logger.debug("wenyan -> %s", redact(" ".join(cmd), self.app_secret))
            try:
                proc = self._run(cmd, self.build_env(), self.timeout, workdir)
            except subprocess.TimeoutExpired as exc:
                raise RetryableError(
                    f"wenyan publish 超时（{self.timeout}s）", raw={"backend": "wenyan"}
                ) from exc
            except OSError as exc:
                raise PermanentError(
                    f"wenyan publish 无法启动：{exc}", raw={"backend": "wenyan"}
                ) from exc

        stdout = redact(proc.stdout or "", self.app_secret)
        stderr = redact(proc.stderr or "", self.app_secret)
        logger.debug(
            "wenyan <- rc=%s stdout=%s stderr=%s", proc.returncode, stdout[:1000], stderr[:1000]
        )
        if proc.returncode != 0:
            raise PermanentError(
                f"wenyan publish 失败 rc={proc.returncode}: {(stderr or stdout)[:400]}",
                raw={"backend": "wenyan", "returncode": proc.returncode},
            )
        match = _MEDIA_ID.search(stdout) or _MEDIA_ID.search(stderr)
        if match:
            return {"media_id": match.group(1), "found_media_id": True, "stdout": stdout}
        logger.warning("wenyan publish 成功但未能从输出解析 media_id，使用内容哈希占位")
        return {"media_id": f"wenyan-{seed}", "found_media_id": False, "stdout": stdout}


class WenyanWechatMpPublisher(WechatMpPublisher):
    """用 wenyan CLI 完成"渲染 + 传图 + 建草稿"的公众号发布器。

    与 API 后端的差异：

    - ``prepare`` **不**上传封面与正文图（wenyan 自己处理），只做长度校验；
    - ``publish`` 只到草稿箱，**从不** freepublish：wenyan 的 CLI 没有发布能力，
      而且双确认闸门的意义就是把"真正推送给读者"这一步留给人；
    - ``reconcile`` / ``health`` / ``fetch_metrics`` 仍复用官方 API 客户端。
    """

    platform: ClassVar[str] = "wechat_mp"

    def __init__(self, account_id: str, *, backend: WenyanBackend | None = None, **kwargs: Any):
        super().__init__(account_id, **kwargs)
        self.backend = backend or WenyanBackend.from_settings(dry_run=self.dry_run)

    def prepare(self, bundle: ContentBundle) -> ContentBundle:
        # 复用 API 后端的字段长度校验，但**不**上传封面与正文图（wenyan 自己处理）
        title, extra = self.normalise_fields(bundle)
        extra["backend"] = "wenyan"
        extra["wechat_prepared"] = True
        return bundle.model_copy(update={"title": title, "platform_extra": extra})

    def publish(self, bundle: ContentBundle) -> PublishResult:
        _, gate = self.gate(bundle)
        gate["backend"] = "wenyan"
        gate["note"] = "wenyan 后端只到草稿箱，freepublish 需人工在后台点发表"
        if self.dry_run:
            return PublishResult(ok=False, raw={"dry_run": True, "stage": "draft", "gate": gate})
        result = self.backend.publish_draft(bundle.body_markdown, title=bundle.title.strip())
        return PublishResult(
            ok=True,
            platform_post_id=str(result["media_id"]),
            url=None,
            raw={
                "stage": "draft",
                "gate": gate,
                "found_media_id": result["found_media_id"],
            },
            published_at=self._now(),
        )


__all__ = ["WenyanBackend", "WenyanWechatMpPublisher"]
