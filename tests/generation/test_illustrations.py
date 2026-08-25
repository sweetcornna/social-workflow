"""生图配图接入三条管线（P11）：媒体顺序、留痕、以及各条降级路径。

生图客户端一律注入假替身或 respx 打桩，**不出网**。这里关心的是"管线怎么用它"，
客户端本身的行为在 tests/generation/test_imagegen.py 里覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import respx

from core.budget import BudgetExhausted, BudgetGuard, CostKind
from core.config import reload_settings
from generation import imagegen
from generation.imagegen import (
    ASPECT_LANDSCAPE_3_2,
    ASPECT_PORTRAIT_3_4,
    AspectSpec,
    GeneratedImage,
    ImagegenClient,
    ImagegenNotEnabled,
    ImageUsage,
    apply_aspect,
)
from generation.pipeline import (
    GenerationOptions,
    XhsGenerationOptions,
    generate_wechat_bundle,
    generate_xhs_bundle,
)
from generation.video_pipeline import VideoGenerationOptions, generate_douyin_bundle
from review.inspect import inspect, read_image_size
from review.pipeline import ReviewOptions, review
from tests.generation.test_imagegen import png_bytes
from tests.p2_helpers import card_screenshotter, xhs_llm

PROMPTS = [
    "overhead flat lay of a linen tablecloth and ceramic mug, morning light, no text",
    "close up of a woven basket on a wooden shelf, soft shadows, no logo",
]


# --------------------------------------------------------------- 测试替身


@dataclass
class FakeImagegen:
    """假生图客户端：不出网，按脚本产出图或抛异常。

    ``size`` 刻意**不**决定落盘尺寸——真实网关就是这么不听话，管线必须靠
    read_image_size 量出来的值做决策，这个替身把那件事复现出来。
    """

    width: int = 1086
    height: int = 1448
    raise_exc: Exception | None = None
    #: 第几次调用开始抛（0 = 第一次就抛）
    raise_after: int = 0
    calls: list[dict] = field(default_factory=list)
    closed: bool = False

    def generate(
        self,
        prompt: str,
        out_path,
        *,
        aspect: AspectSpec | None = None,
        size: str | None = None,
        purpose: str = "",
        account_id: str = "",
        platform: str = "",
    ) -> GeneratedImage:
        # 复用客户端那一份 apply_aspect / size 归并，替身才不会和真货漂开
        prompt = apply_aspect(prompt, aspect)
        size = size or (aspect.size if aspect is not None else "")
        self.calls.append(
            {
                "prompt": prompt,
                "size": size,
                "aspect": aspect.key if aspect is not None else "",
                "purpose": purpose,
                "platform": platform,
            }
        )
        if self.raise_exc is not None and len(self.calls) > self.raise_after:
            raise self.raise_exc
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png_bytes(self.width, self.height))
        return GeneratedImage(
            path=target,
            requested_size=size,
            model="gpt-image-2",
            prompt=prompt,
            width=self.width,
            height=self.height,
            revised_prompt=f"revised::{prompt}",
            usage=ImageUsage(input_tokens=10, output_tokens=1500, total_tokens=1510),
            bytes_len=target.stat().st_size,
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def shots():
    return []


# ------------------------------------------------------------------ 小红书


def test_xhs_appends_illustrations_after_text_cards(tmp_path, shots) -> None:
    """配图排在文字卡之后，且**不抢封面**。"""
    fake = FakeImagegen()
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        content_id="itm_x",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=fake,
        ),
    )

    cards = outcome.draft.page_count
    assert len(outcome.card_paths) == cards
    assert len(outcome.illustration_paths) == 2
    media = outcome.bundle.media
    assert len(media) == cards + 2
    # 前 N 张是文字卡，后 2 张是配图
    assert [Path(m.path) for m in media[:cards]] == list(outcome.card_paths)
    assert [Path(m.path) for m in media[cards:]] == list(outcome.illustration_paths)
    # 封面仍是第一张文字卡：版式可控的东西才配当封面
    assert media[0].cover is True
    assert all(m.cover is False for m in media[1:])
    assert all(m.kind == "image" for m in media)
    assert all(Path(m.path).is_file() for m in media)


def test_xhs_records_prompts_and_measured_size_in_platform_extra(tmp_path, shots) -> None:
    """留痕：prompt、模型、请求尺寸、**实测**尺寸都要能回看。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=FakeImagegen(),
        ),
    )
    extra = outcome.bundle.platform_extra
    assert extra["image_prompts"] == PROMPTS
    entries = extra["illustrations"]
    assert len(entries) == 2
    first = entries[0]
    # 留痕记的是**真正发出去的那条**：带画幅指令，出了问题能原样复现
    assert first["prompt"] == ASPECT_PORTRAIT_3_4.directive + PROMPTS[0]
    assert first["revised_prompt"] == f"revised::{first['prompt']}"
    assert first["model"] == "gpt-image-2"
    assert first["requested_size"] == "1024x1536"
    # 请求 1024x1536 实返 1086x1448 —— 记的是量出来的那个
    assert first["actual_size"] == [1086, 1448]
    assert first["output_tokens"] == 1500
    assert Path(first["final_path"]).is_file()


def test_xhs_illustrations_pass_inspect(tmp_path, shots) -> None:
    """生图与文字卡同权走机器审核，且不该引入新的 block。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=FakeImagegen(),
        ),
    )
    report = inspect(outcome.bundle)
    assert report.ok, [f.rule for f in report.blocking]
    assert report.metrics["illustration_count"] == 2
    assert report.metrics["image_count"] == outcome.draft.page_count + 2


def test_xhs_off_ratio_illustration_is_center_cropped(tmp_path, shots) -> None:
    """比例不合规（9:16）时居中裁切到 3:4，否则 inspect 会 warn 说会被平台裁。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS[:1]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            # 1024x1536 = 0.667，落在 3:4~4:3 之外
            imagegen=FakeImagegen(width=1024, height=1536),
        ),
    )
    fitted = outcome.illustration_paths[0]
    assert fitted.name.endswith("-fit.png")
    assert read_image_size(fitted) == (1242, 1656)
    assert any("居中裁切" in w for w in outcome.warnings)
    assert inspect(outcome.bundle).ok


def test_xhs_in_ratio_illustration_skips_the_browser(tmp_path, shots) -> None:
    """比例本来就对就不重截一遍——省一次浏览器启动。"""
    before = len(shots)
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS[:1]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            imagegen=FakeImagegen(width=1086, height=1448),  # 正好 0.75
        ),
    )
    assert not outcome.illustration_paths[0].name.endswith("-fit.png")
    # 只有文字卡走了截图，配图没有
    assert len(shots) - before == outcome.draft.page_count


def test_xhs_square_illustration_is_cropped_even_though_the_platform_would_take_it(
    tmp_path, shots
) -> None:
    """1.000 落在平台容忍区间 (0.74, 1.34) 内，但我们自己的笔记是 3:4 竖版。

    平台"会不会裁"和"我们该不该配横图"是两个问题：前者是 review 侧的
    ``XHS_ASPECT_RANGE``，后者是生成侧的 ``XHS_ILLUSTRATION_ASPECT_RANGE``。
    实测裸提示词就会返回 1254×1254 这种方图，所以这条路必须真的裁。
    """
    from review.inspect import XHS_ASPECT_RANGE

    low, high = XHS_ASPECT_RANGE
    assert low <= 1.0 <= high, "前提自检：方图本来是平台容忍的，这条测试才有意义"

    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS[:1]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            # 1254x1254 = 1.000：模型没听画幅指令时的实测产物
            imagegen=FakeImagegen(width=1254, height=1254),
        ),
    )
    fitted = outcome.illustration_paths[0]
    assert fitted.name.endswith("-fit.png"), "方图被原样放进了竖版笔记"
    assert read_image_size(fitted) == (1242, 1656)
    assert any("居中裁切" in w for w in outcome.warnings)


def test_xhs_zero_illustrations_skips_imagegen_and_prompt(tmp_path, shots) -> None:
    """关掉配图时连"让模型写配图 prompt"都省掉，不白花 token。"""
    fake = FakeImagegen()
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=0,
            imagegen=fake,
        ),
    )
    assert fake.calls == []
    assert outcome.illustration_paths == []
    assert outcome.bundle.platform_extra["illustrations"] == []
    assert outcome.bundle.platform_extra["image_prompts"] == []
    # 链路照常跑完（trace 有内容），只是没走生图这一段
    assert outcome.draft.trace["body"]


# ------------------------------------------------------- 降级：一条都不能阻塞


def test_xhs_permission_error_degrades_to_text_cards_only(tmp_path, shots) -> None:
    """红线：权限没开 → 无配图继续 + warning，绝不阻塞出稿。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=FakeImagegen(raise_exc=ImagegenNotEnabled("分组没开图像生成")),
        ),
    )
    assert outcome.illustration_paths == []
    assert len(outcome.bundle.media) == outcome.draft.page_count
    assert any("分组没开图像生成" in w for w in outcome.warnings)
    # 稿子照常可发
    assert inspect(outcome.bundle).ok


def test_xhs_budget_exhausted_degrades_not_raises(session, tmp_path, shots) -> None:
    """红线：预算拒绝也只是"这条没有配图"，不能把整条链炸掉。"""
    guard = BudgetGuard(session, image_budget=1)
    guard.charge(CostKind.IMAGES, 1, meta={})
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=FakeImagegen(
                raise_exc=BudgetExhausted("images", 1, 0, 1),
            ),
        ),
        budget=guard,
    )
    assert outcome.illustration_paths == []
    assert any("预算" in w for w in outcome.warnings)
    assert inspect(outcome.bundle).ok


def test_xhs_partial_failure_keeps_the_first_image(tmp_path, shots) -> None:
    """第二张挂了，第一张仍然要留下——已经花掉的钱不能白花。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=FakeImagegen(raise_exc=ImagegenNotEnabled("挂了"), raise_after=1),
        ),
    )
    assert len(outcome.illustration_paths) == 1
    assert len(outcome.bundle.platform_extra["illustrations"]) == 1
    assert any("第 2 张" in w for w in outcome.warnings)


def test_xhs_model_gave_no_prompts(tmp_path, shots) -> None:
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=[]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=FakeImagegen(),
        ),
    )
    assert outcome.illustration_paths == []
    assert any("没给配图 prompt" in w for w in outcome.warnings)


def test_imagegen_client_is_closed_even_when_generation_fails(tmp_path, shots) -> None:
    """注入的客户端由调用方负责关；管线自己造的才由管线关。"""
    fake = FakeImagegen(raise_exc=ImagegenNotEnabled("x"))
    generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            imagegen=fake,
        ),
    )
    assert fake.closed is False


# ------------------------------------------------------------------ 公众号


def wechat_llm(*, image_prompts: list[str], cover_prompt: str = "") -> object:
    """喂饱公众号五步链的假 LLM（正文足够长，免得 inspect 报 body.too_short）。"""
    from generation.llm import ScriptedLLM
    from generation.wechat_article import ArticleMeta, SelfCheck

    body = "通勤这件事，按时薪算是一笔明账。" * 30
    return ScriptedLLM(
        replies=["## 大纲", body, body, body],
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
            ArticleMeta(
                title="通勤一小时一年亏十五万",
                digest="按时薪算的一笔通勤账。",
                cover_prompt=cover_prompt,
                cover_title="通勤的隐形账单",
                keywords=["通勤"],
                image_prompts=list(image_prompts),
            ),
        ],
    )


def test_wechat_hero_becomes_cover_background_at_exact_size(tmp_path) -> None:
    """题图：生图当底图，最终封面仍然是精确的 900×383 / 900×900。"""
    written: list[tuple[Path, str]] = []
    fake = FakeImagegen(width=1536, height=1024)
    outcome = generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=PROMPTS[:1]),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=fake,
            screenshotter=card_screenshotter(written),
        ),
    )
    assert outcome.hero_image is not None
    assert (outcome.hero_image.width, outcome.hero_image.height) == (1536, 1024)
    # size 照发（换一台认它的网关时还得靠它），真正起作用的是提示词里的横版指令
    assert fake.calls[0]["size"] == "1536x1024"
    assert fake.calls[0]["aspect"] == "landscape_3_2"
    assert fake.calls[0]["purpose"] == "wechat.hero"
    assert read_image_size(outcome.cover_paths["banner"]) == (900, 383)
    assert read_image_size(outcome.cover_paths["square"]) == (900, 900)
    # 底图内嵌进了模板，而不是外链
    banner_html = next(html for path, html in written if "banner" in str(path))
    assert "data:image/png;base64," in banner_html
    assert outcome.bundle.platform_extra["illustrations"][0]["role"] == "hero"


def test_wechat_falls_back_to_flat_cover(tmp_path) -> None:
    written: list[tuple[Path, str]] = []
    outcome = generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=PROMPTS[:1]),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=FakeImagegen(raise_exc=ImagegenNotEnabled("没开权限")),
            screenshotter=card_screenshotter(written),
        ),
    )
    assert outcome.hero_image is None
    # 封面照出，只是回到纯色版式
    assert read_image_size(outcome.cover_paths["banner"]) == (900, 383)
    banner_html = next(html for path, html in written if "banner" in str(path))
    assert "data:image/png;base64," not in banner_html
    assert any("纯色版式" in w for w in outcome.warnings)


def test_wechat_cover_prompt_is_used_when_model_skips_image_prompts(tmp_path) -> None:
    """老字段 ``cover_prompt`` 是有意义的回退：有总比没有强。"""
    fake = FakeImagegen(width=1536, height=1024)
    outcome = generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=[], cover_prompt="a quiet desk with a laptop, no text"),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=fake,
            screenshotter=card_screenshotter([]),
        ),
    )
    assert fake.calls[0]["prompt"] == (
        ASPECT_LANDSCAPE_3_2.directive + "a quiet desk with a laptop, no text"
    )
    assert outcome.hero_image is not None


# ------------------------------------------------------------------- 抖音


def test_douyin_cover_uses_generated_background_and_stays_1080x1920(tmp_path) -> None:
    from tests.p3_helpers import cover_screenshotter, douyin_llm

    written: list[tuple[Path, str]] = []
    fake = FakeImagegen(width=1086, height=1448)
    outcome = generate_douyin_bundle(
        "通勤耳机",
        None,
        llm=douyin_llm(image_prompts=PROMPTS[:1]),
        account_id="douyin-demo-01",
        options=VideoGenerationOptions(
            media_root=tmp_path,
            skip_render=True,
            illustrations=1,
            imagegen=fake,
            screenshotter=cover_screenshotter(written),
        ),
    )
    assert outcome.hero_image is not None
    assert fake.calls[0]["purpose"] == "douyin.cover"
    # inspect 的 9:16 校验不变：模板负责把底图裁成精确尺寸
    assert read_image_size(outcome.cover_paths["vertical"]) == (1080, 1920)
    assert outcome.bundle.platform_extra["illustrations"][0]["role"] == "cover"
    assert "data:image/png;base64," in written[0][1]


def test_douyin_degrades_without_blocking_the_render(tmp_path) -> None:
    from tests.p3_helpers import cover_screenshotter, douyin_llm

    outcome = generate_douyin_bundle(
        "通勤耳机",
        None,
        llm=douyin_llm(image_prompts=PROMPTS[:1]),
        account_id="douyin-demo-01",
        options=VideoGenerationOptions(
            media_root=tmp_path,
            skip_render=True,
            illustrations=1,
            imagegen=FakeImagegen(raise_exc=ImagegenNotEnabled("没开权限")),
            screenshotter=cover_screenshotter([]),
        ),
    )
    assert outcome.hero_image is None
    assert read_image_size(outcome.cover_paths["vertical"]) == (1080, 1920)
    assert outcome.video_path is not None  # 成片链路完全没受影响


# ------------------------------------------------------- 配图 prompt 也要送审


def test_image_prompts_go_through_the_lexicon(tmp_path, shots) -> None:
    """红线：prompt 本身踩了词库同样要被报出来，且标明来自配图 prompt。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=["a poster about 敏感词甲 on a wall"]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            imagegen=FakeImagegen(),
        ),
    )
    result = review(
        outcome.bundle,
        options=ReviewOptions(use_llm=False, lexicon_dir=str(_write_lexicon(tmp_path))),
    )
    assert "lexicon.image_prompts" in result.stages_run
    hits = [f for f in result.findings if f.extra.get("source") == "image_prompt"]
    assert hits, [f.rule for f in result.findings]
    assert "配图 prompt 命中词库" in hits[0].suggestion
    # 下标清掉了：它是相对 prompt 文本的，留着会让审核页在正文里高亮错位置
    assert hits[0].start is None


def test_risky_prompt_is_flagged_by_inspect(tmp_path, shots) -> None:
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=["a portrait of a real celebrity holding a brand logo"]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            imagegen=FakeImagegen(),
        ),
    )
    report = inspect(outcome.bundle)
    rules = [f.rule for f in report.findings]
    assert "inspect.illustration.prompt_risky" in rules
    # 只是 warn：配图 prompt 有风险要人看一眼，不该直接把稿子毙掉
    assert report.ok


def test_negated_constraints_are_not_false_positives(tmp_path, shots) -> None:
    """``no recognizable faces`` 是**约束**不是违规，不许报成噪音。"""
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=["a still life, no recognizable faces, no brand logos"]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            imagegen=FakeImagegen(),
        ),
    )
    report = inspect(outcome.bundle)
    assert "inspect.illustration.prompt_risky" not in [f.rule for f in report.findings]


def _write_lexicon(tmp_path: Path) -> Path:
    """造一个只有一个词的临时词库，避免依赖真实词库文件是否已下载。"""
    from review import lexicon as lexicon_mod

    root = tmp_path / "lex"
    vocab = root / lexicon_mod.LEXICON_SUBDIR
    vocab.mkdir(parents=True, exist_ok=True)
    (vocab / "政治类型.txt").write_text("敏感词甲\n", encoding="utf-8")
    lexicon_mod.clear_cache()
    return root


def test_wechat_hero_is_not_an_orphan_when_the_cover_shipped(tmp_path) -> None:
    """题图是**底图**，被烤进封面里，不该被报成"没挂进 media"。

    2026-08-24 之前 ``inspect.illustration.orphan`` 对每条 ``illustrations`` 记录都要求
    它自己出现在 media 里。小红书的配图确实如此（带 ``final_path``、是交付物），
    但公众号 / 抖音的题图只经 ``background=`` 进 render_cover_set，成品是渲出来的封面——
    于是每条公众号和抖音稿都稳定挂一条假 warn，autopilot 因此永远不自动批准。
    """
    written: list[tuple[Path, str]] = []
    outcome = generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=PROMPTS[:1]),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=FakeImagegen(),
            screenshotter=card_screenshotter(written),
        ),
    )
    bundle = outcome.bundle
    entries = (bundle.platform_extra or {})["illustrations"]
    # 前提自检：豁免挂在 role 上，管线真的得写 role，否则这条测试是空转的
    assert entries and entries[0].get("role") == "hero", entries
    assert any(asset.cover for asset in bundle.media), bundle.media

    rules = [f.rule for f in inspect(bundle).findings]
    assert "inspect.illustration.orphan" not in rules, rules
    assert "inspect.illustration.dropped" not in rules, rules


def test_wechat_hero_is_reported_when_no_cover_shipped(tmp_path) -> None:
    """封面没渲出来时题图就是白生成的——豁免必须在这里失效。"""
    written: list[tuple[Path, str]] = []
    outcome = generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=PROMPTS[:1]),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=FakeImagegen(),
            screenshotter=card_screenshotter(written),
        ),
    )
    # 缺 Playwright 时 render_cover_set 返回空 → media 里一张封面都没有。
    # inspect 只看 bundle，所以这样建模就是它眼中的降级态。
    degraded = outcome.bundle.model_copy(
        update={"media": [asset for asset in outcome.bundle.media if not asset.cover]}
    )
    findings = {f.rule: f for f in inspect(degraded).findings}
    assert "inspect.illustration.dropped" in findings, list(findings)
    assert findings["inspect.illustration.dropped"].level == "warn"


# ------------------------------------------- 目标画幅真的送到了网关（respx 打桩）
#
# 这一节刻意**不用** FakeImagegen：断言必须落在真正的 HTTP 请求体上，
# 否则"管线传了什么"和"网关收到什么"之间的那一段没人看着。


IMAGEGEN_URL = "https://imagegen.test/v1/images/generations"


@pytest.fixture
def live_imagegen(monkeypatch):
    """一个真的 ImagegenClient（请求由 respx 拦掉，不出网）。"""
    monkeypatch.setenv("SW_IMAGEGEN_ENABLED", "auto")
    reload_settings()
    imagegen.reset_availability()
    client = ImagegenClient()
    try:
        yield client
    finally:
        client.close()
        imagegen.reset_availability()


def _gateway_payload(width: int = 1086, height: int = 1448) -> dict:
    import base64

    return {
        "model": "gpt-image-2",
        "data": [{"b64_json": base64.b64encode(png_bytes(width, height)).decode("ascii")}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _prompts_on_the_wire(route) -> list[str]:
    return [
        httpx.Response(200, content=call.request.content).json()["prompt"] for call in route.calls
    ]


@respx.mock
def test_xhs_asks_the_gateway_for_a_3_4_portrait(tmp_path, shots, live_imagegen) -> None:
    """小红书内页配图：**每一条**送出去的 prompt 都要带 3:4 竖版指令。"""
    route = respx.post(IMAGEGEN_URL).mock(return_value=httpx.Response(200, json=_gateway_payload()))

    generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=2,
            imagegen=live_imagegen,
        ),
    )

    sent = _prompts_on_the_wire(route)
    assert len(sent) == 2
    for text, original in zip(sent, PROMPTS, strict=True):
        assert "3:4 aspect ratio (taller than wide)" in text, text
        assert text.endswith(original), "原 prompt 被指令挤掉了"


@respx.mock
def test_wechat_hero_asks_the_gateway_for_a_landscape(tmp_path, live_imagegen) -> None:
    """公众号题图：一张底图要喂 900×383 与 900×900，送出去的必须是横版指令。"""
    route = respx.post(IMAGEGEN_URL).mock(
        return_value=httpx.Response(200, json=_gateway_payload(1672, 941))
    )

    generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=PROMPTS[:1]),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=live_imagegen,
            screenshotter=card_screenshotter([]),
        ),
    )

    sent = _prompts_on_the_wire(route)
    assert len(sent) == 1
    assert "3:2 aspect ratio (wider than tall)" in sent[0], sent[0]
    assert "taller than wide" not in sent[0], "竖版指令跑到公众号题图上了"


@respx.mock
def test_xhs_and_wechat_do_not_ask_for_the_same_shape(tmp_path, shots, live_imagegen) -> None:
    """两个用途拿到的是**不同**的指令——一个常量套两处等于没修。"""
    route = respx.post(IMAGEGEN_URL).mock(return_value=httpx.Response(200, json=_gateway_payload()))

    generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(image_prompts=PROMPTS[:1]),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            screenshotter=card_screenshotter(shots),
            illustrations=1,
            imagegen=live_imagegen,
        ),
    )
    generate_wechat_bundle(
        "AI 编程助手",
        None,
        llm=wechat_llm(image_prompts=PROMPTS[:1]),
        account_id="wechat-demo-01",
        options=GenerationOptions(
            media_root=tmp_path,
            render_html=False,
            illustrations=1,
            imagegen=live_imagegen,
            screenshotter=card_screenshotter([]),
        ),
    )

    xhs_prompt, wechat_prompt = _prompts_on_the_wire(route)
    # 两条原 prompt 是同一句，差别只可能来自画幅指令
    assert xhs_prompt.endswith(PROMPTS[0]) and wechat_prompt.endswith(PROMPTS[0])
    assert xhs_prompt != wechat_prompt
    assert "taller than wide" in xhs_prompt
    assert "wider than tall" in wechat_prompt


@respx.mock
def test_douyin_cover_asks_the_gateway_for_a_9_16_vertical(tmp_path, live_imagegen) -> None:
    """抖音封面：模板要裁进 1080×1920（0.562），送出去的必须是 9:16 竖版指令。

    三个平台里抖音的形状最极端，不给指令拿回来的方图 / 横图居中裁进 9:16
    只剩三成多画面。这条和小红书那条一起，钉住"每个用途送的指令不一样"——
    只钉一个平台的话，把三档指令写成同一个常量也能全绿。
    """
    from tests.p3_helpers import cover_screenshotter, douyin_llm

    route = respx.post(IMAGEGEN_URL).mock(
        return_value=httpx.Response(200, json=_gateway_payload(941, 1672))
    )

    generate_douyin_bundle(
        "通勤耳机",
        None,
        llm=douyin_llm(image_prompts=PROMPTS[:1]),
        account_id="douyin-demo-01",
        options=VideoGenerationOptions(
            media_root=tmp_path,
            skip_render=True,
            illustrations=1,
            imagegen=live_imagegen,
            screenshotter=cover_screenshotter([]),
        ),
    )

    sent = _prompts_on_the_wire(route)
    assert len(sent) == 1
    assert "9:16 aspect ratio (much taller than wide)" in sent[0], sent[0]
    assert sent[0].endswith(PROMPTS[0]), "原 prompt 被指令挤掉了"
    # 3:4 是小红书那档；抖音拿错档也会"带着指令"，所以要显式排除
    assert "3:4 aspect ratio" not in sent[0], sent[0]
