"""小红书 bundle 组装 + 发布前校验（inspect）+ 三级审核接线。全部离线。"""

from __future__ import annotations

from pathlib import Path

import pytest

from generation import cover as cover_mod
from generation.imagegen import ImagegenNotEnabled
from generation.pipeline import XhsGenerationOptions, generate_xhs_bundle
from generation.xhs_note import PageSpec, XhsQualityError, XhsRevision
from publishers.base import ContentBundle, MediaAsset
from review.inspect import (
    MAX_XHS_BODY_CHARS,
    MAX_XHS_TAGS,
    MAX_XHS_TITLE_CHARS,
    XHS_IMAGE_RANGE,
    inspect,
    read_image_size,
)
from review.pipeline import ReviewOptions, review, review_text
from sourcing.base import RawTopic
from tests.p2_helpers import card_screenshotter, png_bytes, xhs_llm


@pytest.fixture
def offline_options(tmp_path) -> XhsGenerationOptions:
    written: list[tuple[Path, str]] = []
    return XhsGenerationOptions(
        media_root=tmp_path / "media",
        screenshotter=card_screenshotter(written),
    )


# ------------------------------------------------------------------ bundle


def test_generate_xhs_bundle_satisfies_frozen_contract(offline_options) -> None:
    outcome = generate_xhs_bundle(
        RawTopic(source="newsnow", title="租房收纳", url="https://x.test/1", score=0.8),
        None,
        llm=xhs_llm(),
        account_id="xhs-demo-01",
        options=offline_options,
    )
    bundle = outcome.bundle

    assert bundle.platform == "xhs"
    assert bundle.title and len(bundle.title) <= MAX_XHS_TITLE_CHARS
    # 正文 = 文案 + 话题行；标签在正文里也要出现（平台把话题算作正文）
    assert bundle.body_markdown.endswith("#免打孔")
    assert len(bundle.body_markdown) <= MAX_XHS_BODY_CHARS
    # 小红书不需要 body_html
    assert bundle.body_html is None
    # 媒体：封面 + 内页，第一张标了 cover
    assert len(bundle.media) == outcome.draft.page_count
    assert bundle.cover is not None and bundle.cover.cover is True
    assert all(asset.kind == "image" for asset in bundle.media)
    assert all(Path(asset.path).is_file() for asset in bundle.media)
    # platform_extra 三件套 + 生成侧留痕
    extra = bundle.platform_extra
    assert extra["tags"] == bundle.tags
    assert extra["schedule_at"] is None
    assert extra["is_original"] is True
    assert extra["theme"] == "editorial"
    assert extra["cover_headline"]
    assert len(extra["pages"]) == len(outcome.draft.pages)
    assert extra["selfcheck"]["verdict"] == "pass"
    assert extra["generated_by"].endswith("generate_xhs_bundle")


def test_bundle_roundtrips_through_json(offline_options) -> None:
    """入库走 bundle_json，必须能原样还原（契约冻结要求）。"""
    outcome = generate_xhs_bundle(
        "租房收纳", None, llm=xhs_llm(), account_id="xhs-demo-01", options=offline_options
    )
    restored = ContentBundle.model_validate(outcome.bundle.model_dump(mode="json"))
    assert restored == outcome.bundle


def test_cards_are_written_under_item_id(tmp_path) -> None:
    written: list[tuple[Path, str]] = []
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(),
        account_id="xhs-demo-01",
        content_id="itm_fixed",
        options=XhsGenerationOptions(
            media_root=tmp_path, screenshotter=card_screenshotter(written)
        ),
    )
    assert all(path.parent == tmp_path / "itm_fixed" for path in outcome.card_paths)
    assert read_image_size(outcome.card_paths[0]) == (1242, 1656)


def test_watermark_defaults_to_account_handle(tmp_path) -> None:
    from core.models import Account

    written: list[tuple[Path, str]] = []
    account = Account(id="xhs-demo-01", platform="xhs", name="一个人住的第4年", extra={})
    generate_xhs_bundle(
        "租房收纳",
        account,
        llm=xhs_llm(),
        options=XhsGenerationOptions(
            media_root=tmp_path, screenshotter=card_screenshotter(written)
        ),
    )
    assert "@一个人住的第4年" in written[0][1]


def test_pipeline_degrades_without_browser(tmp_path, monkeypatch) -> None:
    """没有 chromium 时不抛：内容仍入库进人工队列，由 inspect 报缺图。"""
    monkeypatch.setattr(cover_mod, "playwright_available", lambda: False)
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(media_root=tmp_path),
    )
    assert outcome.bundle.media == []
    assert any("未渲染卡片" in w for w in outcome.warnings)
    report = inspect(outcome.bundle)
    assert any(f.rule == "inspect.xhs.image.missing" for f in report.blocking)


def test_make_cards_false_skips_rendering(tmp_path) -> None:
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(media_root=tmp_path, make_cards=False),
    )
    assert outcome.card_paths == []
    assert any("跳过卡片渲染" in w for w in outcome.warnings)


def test_pipeline_requires_account_id() -> None:
    with pytest.raises(ValueError, match="account 或 account_id"):
        generate_xhs_bundle("x", None, llm=xhs_llm())


def test_schedule_at_flows_into_platform_extra(tmp_path) -> None:
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=xhs_llm(),
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(
            media_root=tmp_path, make_cards=False, schedule_at="2026-08-20T10:00:00+08:00"
        ),
    )
    assert outcome.bundle.platform_extra["schedule_at"] == "2026-08-20T10:00:00+08:00"


def test_quality_error_precedes_card_render_and_imagegen(tmp_path, monkeypatch) -> None:
    calls = {"render": 0, "imagegen": 0}

    def render_spy(*args, **kwargs):
        calls["render"] += 1
        return []

    class ImagegenSpy:
        def generate(self, *args, **kwargs):
            calls["imagegen"] += 1
            raise AssertionError("quality failure must precede imagegen")

    monkeypatch.setattr("generation.pipeline.render_cards", render_spy)
    with pytest.raises(XhsQualityError):
        generate_xhs_bundle(
            "租房收纳",
            None,
            llm=xhs_llm(
                verdict="revise",
                final_verdict="reject",
                final_blocking_issues=["仍不安全"],
                image_prompts=["safe prompt"],
            ),
            account_id="xhs-demo-01",
            options=XhsGenerationOptions(media_root=tmp_path, imagegen=ImagegenSpy()),
        )
    assert calls == {"render": 0, "imagegen": 0}


def test_pipeline_forwards_suggested_angle(tmp_path) -> None:
    marker = "PIPELINE_ANGLE_MARKER"
    llm = xhs_llm()
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=llm,
        account_id="xhs-demo-01",
        suggested_angle=marker,
        options=XhsGenerationOptions(media_root=tmp_path, make_cards=False),
    )
    assert outcome.bundle.platform_extra["suggested_angle"] == marker
    assert all(marker in call["prompt"] for call in llm.calls)


def test_canonical_publish_body_is_checked_and_copied_without_tail_loss(tmp_path) -> None:
    marker = "TAIL_77_MARKER"
    tags = ["TAGEND"]
    suffix = "\n\n#TAGEND"
    body = "正" * (MAX_XHS_BODY_CHARS - len(suffix) - len(marker)) + marker
    llm = xhs_llm(body=body, tags=tags)
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=llm,
        account_id="xhs-demo-01",
        options=XhsGenerationOptions(media_root=tmp_path, make_cards=False),
    )
    checked_prompt = next(
        call["prompt"] for call in llm.calls if call["purpose"] == "xhs.selfcheck"
    )
    assert marker in checked_prompt
    assert marker in outcome.bundle.body_markdown
    assert len(outcome.bundle.body_markdown) == MAX_XHS_BODY_CHARS
    assert outcome.draft.body == outcome.draft.body_with_tags()
    assert outcome.draft.body == outcome.bundle.body_markdown


def test_all_final_field_markers_are_checked_then_copied_without_change(tmp_path) -> None:
    class DisabledImagegen:
        def generate(self, *args, **kwargs):
            raise ImagegenNotEnabled("marker test does not render images")

    pages = [
        PageSpec(
            headline=f"页面E99-{index}",
            bullets=["要点F99-1", "要点F99-2"],
            footnote="脚注G99",
        )
        for index in range(3)
    ]
    revision = XhsRevision(
        title="标题T99",
        alt_titles=["备选A99"],
        body="正文B99",
        tags=["标签C99", "标签C98", "标签C97"],
        cover_headline="封面D99",
        pages=pages,
        image_prompts=["IMAGE_H99 safe isolated household still life"],
    )
    llm = xhs_llm(verdict="revise", revision=revision)
    outcome = generate_xhs_bundle(
        "租房收纳",
        None,
        llm=llm,
        account_id="xhs-demo-01",
        suggested_angle="ANGLE_Z99",
        options=XhsGenerationOptions(
            media_root=tmp_path,
            make_cards=False,
            illustrations=1,
            imagegen=DisabledImagegen(),
        ),
    )
    final_prompt = [call["prompt"] for call in llm.calls if call["purpose"] == "xhs.selfcheck"][-1]
    for marker in (
        "ANGLE_Z99",
        "标题T99",
        "备选A99",
        "正文B99",
        "标签C99",
        "封面D99",
        "页面E99",
        "要点F99",
        "脚注G99",
        "IMAGE_H99",
    ):
        assert marker in final_prompt

    draft = outcome.draft
    bundle = outcome.bundle
    extra = bundle.platform_extra
    assert bundle.title == draft.title == revision.title
    assert bundle.body_markdown == draft.body == draft.body_with_tags()
    assert bundle.tags == draft.tags == extra["tags"]
    assert extra["alt_titles"] == draft.alt_titles
    assert extra["pages"] == [page.model_dump() for page in draft.pages]
    assert extra["image_prompts"] == draft.image_prompts


# ------------------------------------------------------------------ inspect


def _xhs_bundle(
    tmp_path: Path,
    *,
    title: str = "租房不打孔，我多出一面墙",
    body: str | None = None,
    tags: list[str] | None = None,
    images: int = 4,
    size: tuple[int, int] = (1242, 1656),
) -> ContentBundle:
    media = []
    for index in range(images):
        path = tmp_path / f"card-{index:02d}.png"
        path.write_bytes(png_bytes(*size))
        media.append(MediaAsset(path=str(path), kind="image", cover=(index == 0)))
    return ContentBundle(
        id="itm_x",
        account_id="xhs-demo-01",
        platform="xhs",
        title=title,
        body_markdown=body if body is not None else "正文" * 60,
        media=media,
        tags=tags if tags is not None else ["租房", "小户型收纳", "独居"],
        platform_extra={"tags": tags or ["租房"], "schedule_at": None, "is_original": True},
    )


def test_inspect_passes_clean_xhs_bundle(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path))
    assert report.ok, report.findings
    assert report.metrics["xhs_tag_count"] == 3
    assert report.metrics["xhs_measured_images"] == 4


@pytest.mark.parametrize(
    ("selfcheck", "blocked"),
    [
        (None, False),
        ({"verdict": "pass", "blocking_issues": []}, False),
        ({"verdict": "revise", "blocking_issues": []}, True),
        ({"verdict": "pass", "blocking_issues": ["遗留阻断问题"]}, True),
    ],
)
def test_review_defensively_enforces_xhs_selfcheck(tmp_path, selfcheck, blocked) -> None:
    bundle = _xhs_bundle(tmp_path)
    if selfcheck is not None:
        bundle = bundle.model_copy(
            update={"platform_extra": {**bundle.platform_extra, "selfcheck": selfcheck}}
        )
    result = review(bundle, options=ReviewOptions(use_llm=False))
    quality_findings = [f for f in result.findings if f.rule == "generation.xhs.selfcheck.failed"]
    assert bool(quality_findings) is blocked
    assert result.passed is (not blocked)


def test_inspect_blocks_title_over_20_chars(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, title="标" * (MAX_XHS_TITLE_CHARS + 1)))
    hit = [f for f in report.blocking if f.rule == "inspect.title.too_long"]
    assert hit and str(MAX_XHS_TITLE_CHARS) in hit[0].suggestion


def test_inspect_allows_title_that_wechat_would_allow_but_xhs_rejects(tmp_path) -> None:
    """25 字标题在公众号（上限 32）合法，在小红书必须被挡下。"""
    title = "标" * 25
    assert inspect(_xhs_bundle(tmp_path, title=title)).ok is False


def test_inspect_blocks_body_over_1000_chars(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, body="字" * (MAX_XHS_BODY_CHARS + 1)))
    assert any(f.rule == "inspect.xhs.body.too_long" for f in report.blocking)


def test_inspect_blocks_zero_images(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, images=0))
    assert any(f.rule == "inspect.xhs.image.missing" for f in report.blocking)


def test_inspect_blocks_more_than_18_images(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, images=XHS_IMAGE_RANGE[1] + 1))
    assert any(f.rule == "inspect.xhs.image.too_many" for f in report.blocking)


def test_inspect_blocks_oversize_image(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, images=1, size=(5000, 5000)))
    assert any(f.rule == "inspect.xhs.image.oversize" for f in report.blocking)


def test_inspect_warns_on_bad_aspect_ratio(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, images=1, size=(1600, 600)))
    rules = [f.rule for f in report.findings]
    assert "inspect.xhs.image.aspect" in rules


def test_inspect_warns_on_tag_count_outside_range(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, tags=["租房"]))
    assert any(f.rule == "inspect.xhs.tags.count" for f in report.findings)
    assert report.ok, "标签偏少只是 warn，不该挡住发布"


def test_inspect_blocks_too_many_tags(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, tags=[f"标签{i}" for i in range(MAX_XHS_TAGS + 1)]))
    assert any(f.rule == "inspect.xhs.tags.too_many" for f in report.blocking)


def test_inspect_warns_on_malformed_and_duplicate_tags(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, tags=["#租房", "独 居", "独 居"]))
    rules = [f.rule for f in report.findings]
    assert "inspect.xhs.tags.malformed" in rules
    assert "inspect.xhs.tags.duplicate" in rules


def test_inspect_does_not_require_body_html_for_xhs(tmp_path) -> None:
    """body_html.missing 是公众号规则，小红书不该被它挡住。"""
    report = inspect(_xhs_bundle(tmp_path))
    assert not any(f.rule == "inspect.body_html.missing" for f in report.findings)


def test_inspect_allows_short_xhs_body(tmp_path) -> None:
    """图文笔记的信息在图上，120 字正文是正常的（公众号会被判 too_short）。"""
    report = inspect(_xhs_bundle(tmp_path, body="正文" * 60))
    assert not any(f.rule == "inspect.body.too_short" for f in report.findings)


def test_inspect_still_blocks_empty_xhs_body(tmp_path) -> None:
    report = inspect(_xhs_bundle(tmp_path, body="太短"))
    assert any(f.rule == "inspect.body.too_short" for f in report.blocking)


def test_inspect_warns_on_mixed_media(tmp_path) -> None:
    bundle = _xhs_bundle(tmp_path, images=2)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    bundle = bundle.model_copy(
        update={"media": [*bundle.media, MediaAsset(path=str(video), kind="video")]}
    )
    assert any(f.rule == "inspect.xhs.media.mixed" for f in inspect(bundle).findings)


def test_read_image_size_handles_unknown_format(tmp_path) -> None:
    junk = tmp_path / "x.png"
    junk.write_bytes(b"definitely not a png")
    assert read_image_size(junk) is None
    assert read_image_size(tmp_path / "missing.png") is None


# ------------------------------------------------------------------ review


def test_review_scans_tags_not_present_in_body(tmp_path) -> None:
    """人工改稿只动了标签输入框时，新标签也必须被送审。"""
    bundle = _xhs_bundle(tmp_path, tags=["租房", "赌博平台"])
    text = review_text(bundle)
    assert "#赌博平台" in text
    result = review(bundle, options=ReviewOptions(use_llm=False))
    assert not result.passed
    assert any("赌博" in f.excerpt for f in result.blocking)


def test_review_does_not_duplicate_tags_already_in_body(tmp_path) -> None:
    bundle = _xhs_bundle(tmp_path, body="正文" * 60 + "\n\n#租房 #独居", tags=["租房", "独居"])
    assert review_text(bundle).count("#租房") == 1


def test_wechat_tags_are_not_appended(tmp_path) -> None:
    """公众号的 tags 是内部检索关键词，不外显，扫它只会平白多出误报。"""
    bundle = ContentBundle(
        id="itm_w",
        account_id="wechat-demo-01",
        platform="wechat_mp",
        title="标题",
        body_markdown="正文",
        tags=["关键词"],
    )
    assert "#关键词" not in review_text(bundle)


def test_review_pipeline_runs_all_stages_for_xhs(tmp_path) -> None:
    result = review(_xhs_bundle(tmp_path), options=ReviewOptions(use_llm=False))
    assert "lexicon" in result.stages_run
    assert "precheck" in result.stages_run
    assert "inspect" in result.stages_run
    assert result.stages_skipped["llm_semantic"]


def test_commercial_rules_apply_to_xhs(tmp_path) -> None:
    """极限词是小红书被判违规最多的一类，带货语境必须扫到。"""
    bundle = _xhs_bundle(tmp_path, body="这是全网最低价，效果第一，" + "正文" * 50)
    plain = review(bundle, options=ReviewOptions(use_llm=False, commercial=False))
    commercial = review(bundle, options=ReviewOptions(use_llm=False, commercial=True))
    assert len(commercial.findings) >= len(plain.findings)
