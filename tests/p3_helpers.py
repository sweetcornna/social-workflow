"""P3（抖音视频）测试公用工具。

和 ``tests/p1_helpers.py`` / ``tests/p2_helpers.py`` 一样单独成模块，
避免多人同时改 ``conftest.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from generation.llm import ScriptedLLM
from generation.video_script import VideoScriptCopy, VideoSelfCheck
from tests.p2_helpers import png_bytes

#: 一段像样的口播稿：短句、有数字、有失败经历，够 200 字但不超 300
DEMO_SCRIPT = (
    "通勤一小时的人，一年亏掉十五万。\n\n"
    "我算过一笔账。单程六十分钟，一周五天，一年就是五百个小时。\n\n"
    "按三百块的时薪折，这是十五万。还没算到家之后什么都不想干的那两个小时。\n\n"
    "搬到离公司二十分钟的地方，房租一个月多两千，一年多花两万四，换回三百三十个小时。\n\n"
    "我问过身边七个通勤超过一小时的人，六个说的是合同没到期，或者懒得搬。\n\n"
    "只有一个人真的算过。\n\n"
    "今晚别估算，用手机自动记一次你的通勤时间。多数人会少算两成。\n\n"
    "你的通勤时间，值多少钱？"
)

#: 钩子必须原样是口播稿的第一句，否则 ``_ensure_hook_first`` 会把它再补一遍
DEMO_HOOK = "通勤一小时的人，一年亏掉十五万。"

DEMO_TERMS = [
    "crowded morning train",
    "empty subway platform",
    "office worker walking",
    "hands counting coins",
]

DEMO_HASHTAGS = ["通勤", "时间管理", "上班族", "租房"]


def douyin_llm(
    *,
    budget: Any = None,
    verdict: str = "pass",
    overall: int = 9,
    title: str = "通勤一小时，一年亏掉十五万",
    hook: str = DEMO_HOOK,
    script: str = DEMO_SCRIPT,
    search_terms: list[str] | None = None,
    cover_text: str = "通勤的隐形账单",
    hashtags: list[str] | None = None,
    blocking_issues: list[str] | None = None,
    rewritten_script: str | None = None,
    image_prompts: list[str] | None = None,
) -> ScriptedLLM:
    """喂饱抖音四步脚本链的假 LLM。

    ``parse`` 按类型取回复，所以两个结构化产物的顺序无所谓；
    ``complete`` 按顺序取：第 1 次是"切角度"，第 2 次（若触发）是"去 AI 味改写"。
    """
    return ScriptedLLM(
        budget=budget,
        replies=[
            "### 3 我要给的答案\n\n换房比换工作划算。",
            rewritten_script if rewritten_script is not None else script,
        ],
        parsed_replies=[
            VideoScriptCopy(
                title=title,
                hook=hook,
                script=script,
                search_terms=list(search_terms if search_terms is not None else DEMO_TERMS),
                cover_text=cover_text,
                hashtags=list(hashtags if hashtags is not None else DEMO_HASHTAGS),
                image_prompts=list(image_prompts or []),
            ),
            VideoSelfCheck(
                ai_flavor=overall,
                hook_strength=overall,
                specificity=overall,
                spoken_fit=overall,
                pacing=overall,
                term_fit=overall,
                compliance_risk=9,
                overall=overall,
                verdict=verdict,  # type: ignore[arg-type]
                blocking_issues=list(blocking_issues or []),
            ),
        ],
    )


def cover_screenshotter(
    written: list[tuple[Path, str]], *, width: int = 1080, height: int = 1920
) -> Any:
    """假截图器：记下 HTML 并写一张**尺寸真实**的 9:16 PNG。

    尺寸真实很重要——``review.inspect`` 会去量图，写 1×1 占位图等于没测。
    """

    def _shot(html: str, path: Path, w: int, h: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes(w or width, h or height))
        written.append((path, html))

    return _shot


# ------------------------------------------------------------------ MPT 打桩


#: 上游成功响应的外壳（``app/utils/utils.py:get_response`` + response_model）
def ok_envelope(data: Any) -> dict[str, Any]:
    return {"status": 200, "message": "success", "data": data}


def error_envelope(status: int, message: str) -> dict[str, Any]:
    """错误路径**绕过 response_model**，body 里没有 data 键。"""
    return {"status": status, "message": message}


def task_payload(
    task_id: str = "task-1",
    *,
    state: int = 4,
    progress: int = 30,
    videos: list[str] | None = None,
    combined_videos: list[str] | None = None,
    failed_stage: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """``GET /api/v1/tasks/{id}`` 的 ``data``。"""
    payload: dict[str, Any] = {
        "task_id": task_id,
        "state": state,
        "progress": progress,
        "videos": videos,
        "combined_videos": combined_videos,
    }
    if failed_stage is not None:
        payload["failed_stage"] = failed_stage
    if error is not None:
        payload["error"] = error
    return payload


__all__ = [
    "DEMO_HASHTAGS",
    "DEMO_HOOK",
    "DEMO_SCRIPT",
    "DEMO_TERMS",
    "cover_screenshotter",
    "douyin_llm",
    "error_envelope",
    "ok_envelope",
    "task_payload",
]
