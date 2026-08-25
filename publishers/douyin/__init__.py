"""抖音发布器（P3）。

抖音**没有面向个人/小团队的官方发布 API**，且会检测 headless 浏览器、发布时会触发
短信二次验证。因此本平台拆成两半：

- **core 侧**（本包默认导出）：``client`` + ``publisher``，只发本地 HTTP，不碰浏览器。
- **宿主机侧**：``service``（Patchright 有头浏览器常驻进程，一号一 profile），
  用 ``python -m publishers.douyin serve --port 8710`` 启动，**不进 Docker**。

模块
----
- :mod:`~publishers.douyin.client`：宿主机上传器的 httpx 薄封装 + state→异常映射
- :mod:`~publishers.douyin.publisher`：``DouyinPublisher``，实现 P0 冻结契约
- :mod:`~publishers.douyin.service`：宿主机上传器本体（**import 时需要 patchright**，
  故不在这里导出；core 永远不会 import 它）
- :mod:`~publishers.douyin.stub`：不联网的测试替身客户端

红线（docs/POLICY.md）：patchright 只用于"让真人自己的账号不被 headless 误杀"；
不做指纹伪装 / Cookie 池 / 多账号共用 profile；验证码只由真人处理，系统绝不识别。
流程参考 ``dreammis/social-auto-upload`` 的**行为**后自行实现，未复制其任何代码
（该仓库无 License，见 docs/THIRD_PARTY.md）。
"""

from publishers.douyin.client import DouyinServiceClient
from publishers.douyin.publisher import DouyinPublisher, is_placeholder_post_id

__all__ = [
    "DouyinPublisher",
    "DouyinServiceClient",
    "is_placeholder_post_id",
]
