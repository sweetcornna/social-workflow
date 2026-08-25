"""公众号生成链：文案 SOP、wenyan 渲染、封面、bundle 组装。全部离线。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import prompts
from core.budget import BudgetGuard, CostKind
from core.dev_flow import DEMO_BODY, make_scripted_llm
from generation import cover as cover_mod
from generation import wechat_render
from generation.llm import ScriptedLLM
from generation.pipeline import GenerationOptions, generate_wechat_bundle
from generation.wechat_article import (
    ArticleMeta,
    SelfCheck,
    clean_body,
    generate_article,
    strip_code_fence,
    truncate,
)
from review.inspect import MAX_DIGEST_CHARS, MAX_TITLE_CHARS
from sourcing.base import RawTopic
from tests.p1_helpers import RecordingRunner, fake_screenshotter

# ------------------------------------------------------------------ 文本工具


def test_strip_code_fence() -> None:
    assert strip_code_fence("```markdown\n# hi\n```") == "# hi"
    assert strip_code_fence("no fence") == "no fence"


def test_clean_body_removes_h1_and_extra_blank_lines() -> None:
    body = clean_body("# 标题\n\n\n\n正文一\n\n正文二")
    assert "# 标题" not in body
    assert "\n\n\n" not in body
    assert "正文一" in body


def test_truncate_counts_characters() -> None:
    assert truncate("一二三四五", 3) == "一二…"
    assert truncate("短", 10) == "短"
    assert truncate("  多  空格  ", 10) == "多 空格"


# ------------------------------------------------------------------ 生成链


def _article_llm(*, verdict: str = "pass", overall: int = 9, budget=None) -> ScriptedLLM:
    return ScriptedLLM(
        budget=budget,
        replies=["## 大纲", DEMO_BODY, DEMO_BODY, DEMO_BODY],
        parsed_replies=[
            SelfCheck(
                ai_flavor=overall,
                specificity=overall,
                hook=overall,
                structure=overall,
                fact_risk=9,
                overall=overall,
                verdict=verdict,  # type: ignore[arg-type]
            ),
            ArticleMeta(
                title="通勤一小时一年亏十五万",
                alt_titles=["备选一", "备选二"],
                digest="按时薪算的一笔通勤账。",
                cover_prompt="empty subway platform, no text",
                cover_title="通勤的隐形账单",
                keywords=["通勤", "时间成本"],
            ),
        ],
    )


def test_generate_article_runs_five_steps() -> None:
    llm = _article_llm()
    draft = generate_article(llm, topic_title="通勤成本", persona="效率号")

    kinds = [c["kind"] for c in llm.calls]
    purposes = [c["purpose"] for c in llm.calls]
    # 大纲(complete) → 正文(long) → 润色(long) → 自评(parse) → meta(parse)
    assert kinds[:4] == ["complete", "complete_long", "complete_long", "parse"]
    assert "wechat.outline" in purposes
    assert "wechat.body" in purposes
    assert "wechat.polish" in purposes
    assert "wechat.selfcheck" in purposes
    assert "wechat.meta" in purposes
    # 自评 pass 且分数高 → 不触发第五步
    assert "wechat.dehumanize" not in purposes
    assert draft.rewritten is False
    assert draft.title == "通勤一小时一年亏十五万"
    assert "outline" in draft.trace and "polished" in draft.trace


def test_low_selfcheck_triggers_dehumanize_step() -> None:
    llm = _article_llm(verdict="revise", overall=5)
    draft = generate_article(llm, topic_title="通勤成本", persona="效率号")

    assert "wechat.dehumanize" in [c["purpose"] for c in llm.calls]
    assert draft.rewritten is True
    assert "dehumanized" in draft.trace


def test_force_rewrite_overrides_selfcheck() -> None:
    llm = _article_llm(verdict="pass", overall=10)
    draft = generate_article(llm, topic_title="x", persona="p", force_rewrite=True)
    assert draft.rewritten is True


def test_title_and_digest_are_hard_truncated() -> None:
    """模型超字数是常态，长度是平台硬限制，不能只靠 prompt 约束。"""
    llm = ScriptedLLM(
        replies=["outline", DEMO_BODY, DEMO_BODY],
        parsed_replies=[
            SelfCheck(
                ai_flavor=9,
                specificity=9,
                hook=9,
                structure=9,
                fact_risk=9,
                overall=9,
                verdict="pass",
            ),
            ArticleMeta(title="超长标题" * 20, digest="超长摘要" * 60),
        ],
    )
    draft = generate_article(llm, topic_title="x", persona="p")
    assert len(draft.title) <= MAX_TITLE_CHARS
    assert len(draft.digest) <= MAX_DIGEST_CHARS


def test_generation_charges_tokens(session) -> None:
    guard = BudgetGuard(session)
    llm = _article_llm(budget=guard)
    generate_article(llm, topic_title="x", persona="p")
    # 5 次调用 × (100 输入 + 100 输出)
    assert guard.used(CostKind.TOKENS) == 1000


def test_system_prompt_carries_persona() -> None:
    llm = _article_llm()
    generate_article(llm, topic_title="x", persona="我是一个测试人设标记")
    assert "我是一个测试人设标记" in llm.calls[0]["system"]


# ------------------------------------------------------------------ wenyan 渲染


def test_render_markdown_builds_expected_command() -> None:
    runner = RecordingRunner(stdout='<section style="color:#333">hi</section>')
    result = wechat_render.render_markdown("# hi", theme="lapis", runner=runner)

    command = runner.last_command
    assert command[1] == "-y", "npx 必须带 -y，否则首次执行会挂在交互确认上"
    assert command[2] == "@wenyan-md/cli"
    assert command[3] == "render"
    assert command[-2:] == ["--theme", "lapis"]
    # 传给 wenyan 的是临时文件路径，内容是我们的 Markdown
    assert command[4].endswith(".md")
    assert result.theme == "lapis"
    assert "<section" in result.html


def test_render_markdown_rejects_empty_input() -> None:
    with pytest.raises(wechat_render.RenderError, match="为空"):
        wechat_render.render_markdown("   ", runner=RecordingRunner())


def test_render_markdown_surfaces_subprocess_failure() -> None:
    runner = RecordingRunner(returncode=1, stderr="theme not found")
    with pytest.raises(wechat_render.RenderError, match="theme not found"):
        wechat_render.render_markdown("# hi", runner=runner)


def test_render_markdown_rejects_non_html_output() -> None:
    runner = RecordingRunner(stdout="just plain text")
    with pytest.raises(wechat_render.RenderError, match="不像 HTML"):
        wechat_render.render_markdown("# hi", runner=runner)


def test_missing_node_raises_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(wechat_render.shutil, "which", lambda _: None)
    with pytest.raises(wechat_render.NodeNotAvailable, match=r"Node\.js"):
        wechat_render.render_markdown("# hi")


@pytest.mark.node
def test_real_wenyan_render_smoke() -> None:
    """可选：装了 Node 才跑，真的调一次 npx。首次会下载包，给足超时。"""
    if not wechat_render.node_available():
        pytest.skip("本机没有 npx")
    try:
        result = wechat_render.render_markdown("# 标题\n\n正文一段。", theme="default", timeout=300)
    except (wechat_render.RenderError, subprocess.SubprocessError) as exc:
        pytest.skip(f"npx 不可用或无网络: {exc}")
    assert "<" in result.html
    assert "style" in result.html.lower()


# ------------------------------------------------------------------ 封面


def test_build_html_is_selfcontained_and_escaped() -> None:
    spec = cover_mod.CoverSpec(title='通勤 & "隐形" <账单>', kicker="HOT", footer="测试号")
    html = cover_mod.build_html(spec)

    assert "900" in html and "383" in html  # banner 尺寸
    # 文案必须转义，否则标题里的 < 会破坏结构
    assert "&amp;" in html and "&lt;账单&gt;" in html
    assert "<账单>" not in html
    # 不能有外链资源，否则截图时要联网
    assert "http://" not in html and "https://" not in html
    assert "__TITLE__" not in html  # 占位符全部替换掉


def test_cover_sizes() -> None:
    assert cover_mod.CoverSpec(title="x", size="banner").dimensions == (900, 383)
    assert cover_mod.CoverSpec(title="x", size="square").dimensions == (900, 900)


def test_palette_is_stable_for_same_title() -> None:
    a = cover_mod.CoverSpec(title="同一个标题").palette()
    b = cover_mod.CoverSpec(title="同一个标题").palette()
    assert a == b


def test_render_cover_returns_none_without_playwright(monkeypatch, tmp_path) -> None:
    """没装 Playwright 时跳过而不是抛异常——封面不该阻断生成链。"""
    monkeypatch.setattr(cover_mod, "playwright_available", lambda: False)
    result = cover_mod.render_cover(cover_mod.CoverSpec(title="x"), tmp_path / "cover.png")
    assert result is None


def test_render_cover_set_with_injected_screenshotter(tmp_path) -> None:
    written: list[tuple[Path, str]] = []
    produced = cover_mod.render_cover_set(
        "通勤的隐形账单",
        tmp_path,
        kicker="newsnow",
        screenshotter=fake_screenshotter(written),
    )
    assert set(produced) == {"banner", "square"}
    assert all(path.is_file() for path in produced.values())
    assert len(written) == 2
    assert "通勤的隐形账单" in written[0][1]


# ------------------------------------------------------------------ pipeline


def test_generate_wechat_bundle_satisfies_frozen_contract(tmp_path) -> None:
    written: list[tuple[Path, str]] = []
    runner = RecordingRunner(stdout='<section style="line-height:1.75">正文</section>')
    outcome = generate_wechat_bundle(
        RawTopic(source="newsnow", title="通勤成本", url="https://x.test/1", score=0.9),
        None,
        llm=_article_llm(),
        account_id="wechat-demo-01",
        author="测试作者",
        options=GenerationOptions(
            media_root=tmp_path,
            screenshotter=fake_screenshotter(written),
            render_runner=runner,
        ),
    )

    bundle = outcome.bundle
    assert bundle.platform == "wechat_mp"
    assert bundle.body_markdown
    assert bundle.body_html and "<section" in bundle.body_html
    # platform_extra 必须齐三件套
    for key in ("title", "digest", "author"):
        assert bundle.platform_extra[key], f"platform_extra.{key} 不能为空"
    assert bundle.platform_extra["author"] == "测试作者"
    # media 含封面，且第一张标了 cover
    assert bundle.media
    assert bundle.cover is not None
    assert bundle.cover.kind == "image"
    assert Path(bundle.cover.path).is_file()
    # 生成侧留痕
    assert bundle.platform_extra["selfcheck"]["verdict"] == "pass"
    assert bundle.tags == ["通勤", "时间成本"]


def test_pipeline_degrades_without_node(tmp_path, monkeypatch) -> None:
    """没有 Node 时不抛异常：内容仍入库进人工队列，由 inspect 报缺 body_html。"""
    monkeypatch.setattr(wechat_render.shutil, "which", lambda _: None)
    outcome = generate_wechat_bundle(
        "通勤成本",
        None,
        llm=_article_llm(),
        account_id="wechat-demo-01",
        options=GenerationOptions(media_root=tmp_path, make_cover=False),
    )
    assert outcome.bundle.body_html is None
    assert any("Node" in w for w in outcome.warnings)


def test_pipeline_requires_account_id() -> None:
    with pytest.raises(ValueError, match="account 或 account_id"):
        generate_wechat_bundle("x", None, llm=_article_llm())


def test_account_extra_persona_wins_over_file(tmp_path) -> None:
    from core.models import Account

    account = Account(
        id="wechat-demo-01",
        platform="wechat_mp",
        name="测试号",
        extra={"persona": "内联人设优先级更高"},
    )
    llm = _article_llm()
    generate_wechat_bundle(
        "x",
        account,
        llm=llm,
        options=GenerationOptions(media_root=tmp_path, make_cover=False, render_html=False),
    )
    assert "内联人设优先级更高" in llm.calls[0]["system"]


def test_demo_scripted_llm_drives_whole_chain(tmp_path) -> None:
    """core.dev_flow 的预置替身必须能喂饱整条生成链。"""
    outcome = generate_wechat_bundle(
        RawTopic(source="newsnow", title="通勤成本"),
        None,
        llm=make_scripted_llm(),
        account_id="wechat-demo-01",
        options=GenerationOptions(media_root=tmp_path, make_cover=False, render_html=False),
    )
    assert len(outcome.bundle.body_markdown) > 200


# ------------------------------------------------------------------ prompts


def test_all_wechat_prompts_render() -> None:
    """prompt 与调用方的变量必须对得上，缺变量要在测试里就炸出来。"""
    expected = {
        "wechat/system": {"persona"},
        "wechat/outline": {
            "topic_title",
            "topic_source",
            "topic_url",
            "topic_context",
            "target_words",
        },
        "wechat/body": {"outline", "target_words"},
        "wechat/polish": {"draft"},
        "wechat/selfcheck": {"draft"},
        "wechat/dehumanize": {"draft", "issues"},
        # P11 起 meta 那一步顺手产出配图 prompt，规范由 prompts/imagegen.md 注入
        "wechat/meta": {"body", "persona", "max_title", "max_digest", "image_rules"},
        # P4 起选题 prompt 多一个 insights（复盘 Agent 的历史结论回灌）
        "sourcing/select": {"today", "persona", "recent", "insights", "candidates", "max_picks"},
        "review/semantic": {"content", "hits"},
    }
    for name, variables in expected.items():
        assert prompts.variables_of(name) == variables, f"{name} 的变量集与调用方不一致"
        rendered = prompts.load(name, **dict.fromkeys(variables, "X"))
        assert "{{" not in rendered


def test_missing_prompt_variable_raises() -> None:
    with pytest.raises(prompts.PromptRenderError, match="缺少变量"):
        prompts.load("wechat/polish")


def test_prompt_path_traversal_blocked() -> None:
    with pytest.raises(prompts.PromptNotFound):
        prompts.prompt_path("../../etc/passwd")


def test_load_persona_falls_back_to_default() -> None:
    assert prompts.load_persona("does-not-exist", default="兜底") == "兜底"
    assert "效率与生活方式" in prompts.load_persona("wechat-demo-01")
