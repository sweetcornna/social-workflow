"""三级审核管线 + 发布前校验。全部离线。"""

from __future__ import annotations

import json

import pytest

from core.state_machine import ContentStatus
from generation.llm import ScriptedLLM
from publishers.base import ContentBundle, MediaAsset
from review import lexicon as lexicon_mod
from review import precheck
from review.base import Finding, ReviewResult, excerpt_around, sort_findings
from review.inspect import (
    MAX_DIGEST_CHARS,
    MAX_TITLE_CHARS,
    MIN_BODY_CHARS,
    inspect,
    iter_image_urls,
)
from review.llm_semantic import (
    ExtraRisk,
    HitJudgement,
    SemanticReview,
    SemanticSkipped,
    apply,
    judge,
)
from review.pipeline import MACHINE_REVIEW_ACTION, ReviewOptions, review, review_item
from review.vendor.yuwen_precheck import LICENSE_PATH, TERMS_PATH
from tests.conftest import make_account

LONG_BODY = "这是一段用于测试的正文。" * 40


def make_wechat_bundle(
    *,
    title: str = "一个正常的测试标题",
    body: str = LONG_BODY,
    body_html: str | None = "<section>正文</section>",
    digest: str = "一句正常的摘要。",
    author: str = "测试作者",
    media: list[MediaAsset] | None = None,
    extra: dict | None = None,
) -> ContentBundle:
    return ContentBundle(
        id="itm_test",
        account_id="wechat-demo-01",
        platform="wechat_mp",
        title=title,
        body_markdown=body,
        body_html=body_html,
        media=media if media is not None else [],
        platform_extra={"title": title, "digest": digest, "author": author, **(extra or {})},
    )


# ------------------------------------------------------------------ base


def test_excerpt_around_adds_ellipsis() -> None:
    text = "abcdefghij" * 10
    snippet = excerpt_around(text, 50, 55, radius=5)
    assert snippet.startswith("…") and snippet.endswith("…")


def test_sort_findings_by_level_then_position() -> None:
    findings = [
        Finding(level="info", rule="c", start=1),
        Finding(level="block", rule="a", start=9),
        Finding(level="warn", rule="b", start=2),
    ]
    assert [f.rule for f in sort_findings(findings)] == ["a", "b", "c"]


def test_review_result_summary_is_human_readable() -> None:
    result = ReviewResult(
        passed=False,
        findings=[
            Finding(level="block", rule="lexicon.政治类型", excerpt="片段", suggestion="删掉")
        ],
        stages_skipped={"llm_semantic": "无 key"},
    )
    summary = result.summary()
    assert "未通过" in summary
    assert "lexicon.政治类型" in summary
    assert "[skip] llm_semantic" in summary


# ------------------------------------------------------------------ 词库


def test_lexicon_falls_back_when_not_installed(tmp_path) -> None:
    """没下载词库时退化到内置兜底表，不能抛异常。"""
    lex = lexicon_mod.load_lexicon(tmp_path)
    assert lex.is_fallback is True
    assert lex.word_count > 0
    findings = lexicon_mod.scan("有人在网上出售枪支", lex)
    assert any(f.rule == "lexicon.涉枪涉爆" and f.level == "block" for f in findings)
    # 必须留一条 info 说明词库缺失，否则会误以为"审核过了"
    assert any(f.rule == "lexicon.not_installed" for f in findings)


def test_lexicon_loads_real_files(tmp_path) -> None:
    vocab = tmp_path / lexicon_mod.LEXICON_SUBDIR
    vocab.mkdir(parents=True)
    (vocab / "政治类型.txt").write_text("敏感词甲\n敏感词乙\n\n敏感词甲\n单\n", encoding="utf-8")
    (vocab / "广告类型.txt").write_text("全网最低\n", encoding="utf-8")
    # 域名清单必须被排除，否则会把正文里的 com/cn 当敏感词
    (vocab / "非法网址.txt").write_text("evil.example.com\n", encoding="utf-8")

    lex = lexicon_mod.load_lexicon(tmp_path)
    assert lex.is_fallback is False
    assert set(lex.categories) == {"政治类型", "广告类型"}
    # 单字词被跳过（误伤率太高）
    assert lex.categories["政治类型"] == 3  # 甲/乙/甲（重复词由自动机吸收）

    findings = lexicon_mod.scan("这里出现了敏感词甲，还有全网最低价", lex)
    rules = {f.rule for f in findings}
    assert rules == {"lexicon.政治类型", "lexicon.广告类型"}
    assert next(f for f in findings if f.rule == "lexicon.政治类型").level == "block"
    assert next(f for f in findings if f.rule == "lexicon.广告类型").level == "warn"


def test_lexicon_defeats_zero_width_evasion(tmp_path) -> None:
    """往词里插零宽空格是最常见的规避手法，命中位置还要能回指原文。"""
    vocab = tmp_path / lexicon_mod.LEXICON_SUBDIR
    vocab.mkdir(parents=True)
    (vocab / "暴恐词库.txt").write_text("制造炸弹\n", encoding="utf-8")
    lex = lexicon_mod.load_lexicon(tmp_path)

    text = "教你制​造​炸弹的方法"
    findings = lexicon_mod.scan(text, lex)
    assert len(findings) == 1
    hit = findings[0]
    assert hit.rule == "lexicon.暴恐词库"
    # start/end 必须是原文下标（含零宽字符），否则给人看的 excerpt 会错位
    assert text[hit.start : hit.end] == "制​造​炸弹"


def test_aho_corasick_finds_overlapping_words() -> None:
    automaton = lexicon_mod.AhoCorasick()
    for word in ("abc", "bcd", "cde"):
        automaton.add(word, "test")
    matches = {(s, e, w) for s, e, w, _ in automaton.iter_matches("abcde")}
    assert matches == {(0, 3, "abc"), (1, 4, "bcd"), (2, 5, "cde")}


def test_lexicon_scan_reports_each_word_once(tmp_path) -> None:
    vocab = tmp_path / lexicon_mod.LEXICON_SUBDIR
    vocab.mkdir(parents=True)
    (vocab / "广告类型.txt").write_text("全网最低\n", encoding="utf-8")
    lex = lexicon_mod.load_lexicon(tmp_path)
    findings = lexicon_mod.scan("全网最低 全网最低 全网最低", lex)
    assert len(findings) == 1


# ------------------------------------------------------------------ precheck


def test_vendored_precheck_data_present_with_license() -> None:
    """vendored 资源必须带 LICENSE，否则 License 台账对不上。"""
    assert TERMS_PATH.is_file()
    assert LICENSE_PATH.is_file()
    assert "MIT License" in LICENSE_PATH.read_text(encoding="utf-8")
    data = json.loads(TERMS_PATH.read_text(encoding="utf-8"))
    assert data["terms"] and data["debunked_myths"]


def test_precheck_rules_load() -> None:
    rules = precheck.get_rules()
    assert len(rules) >= 40
    assert all(term.pattern is not None for term in rules.terms)


def test_precheck_blocks_illegal_content() -> None:
    findings = precheck.scan("有人问代办假证靠不靠谱")
    assert any(f.level == "block" and "general-community-safety" in f.rule for f in findings)


def test_precheck_commercial_rules_are_opt_in() -> None:
    text = "本店全网最低价，欢迎选购"
    assert not [f for f in precheck.scan(text) if "commercial" in f.rule]
    commercial = [f for f in precheck.scan(text, commercial=True) if "commercial" in f.rule]
    assert commercial


def test_precheck_industry_rules_are_opt_in() -> None:
    text = "这款产品包治百病"
    plain = {f.rule for f in precheck.scan(text)}
    medical = {f.rule for f in precheck.scan(text, industries={"medical"})}
    assert medical - plain, "行业规则应该只在声明该行业时命中"


def test_precheck_debunked_myth_is_info_not_violation() -> None:
    """命中"谐音规避"说明作者做了没必要的替换，是提示不是违规。"""
    findings = precheck.scan("教你怎么赚米")
    myth = [f for f in findings if f.rule == "precheck.debunked_myth"]
    assert myth and myth[0].level == "info"
    assert "钱" in myth[0].suggestion


def test_precheck_missing_data_raises_actionable_error(tmp_path) -> None:
    with pytest.raises(precheck.PrecheckDataMissing, match="fetch_lexicon"):
        precheck.load_rules(tmp_path / "nope.json")


# ------------------------------------------------------------------ LLM 语义


def test_judge_skips_when_no_hits() -> None:
    with pytest.raises(SemanticSkipped, match="无 block/warn"):
        judge("正文", [Finding(level="info", rule="x")], ScriptedLLM())


def test_judge_degrades_on_llm_failure() -> None:
    """LLM 挂了不能阻断管线——前两级结论直接生效（更严格，方向安全）。"""
    llm = ScriptedLLM()  # 没有预置 SemanticReview
    with pytest.raises(SemanticSkipped, match="LLM 调用失败"):
        judge("正文", [Finding(level="block", rule="lexicon.政治类型")], llm)


def test_apply_downgrades_safe_verdict_to_info() -> None:
    """判 safe 也保留痕迹，不静默删除 finding。"""
    findings = [Finding(level="block", rule="lexicon.色情类型", excerpt="扫黄打非专项行动")]
    review_out = SemanticReview(
        judgements=[
            HitJudgement(
                rule="lexicon.色情类型",
                verdict="safe",
                reason="这是新闻叙述，语境正当",
                replacement="",
            )
        ]
    )
    merged, edits = apply(findings, review_out)
    assert merged[0].level == "info"
    assert merged[0].extra["machine_level"] == "block"
    assert merged[0].extra["llm_verdict"] == "safe"
    assert edits == {}


def test_apply_keeps_original_when_rule_id_mismatched() -> None:
    """模型抄错 rule id 不该让一条 block 悄悄消失。"""
    findings = [Finding(level="block", rule="lexicon.政治类型")]
    merged, _ = apply(
        findings,
        SemanticReview(judgements=[HitJudgement(rule="错的id", verdict="safe", reason="")]),
    )
    assert merged[0].level == "block"


def test_apply_collects_suggested_edits_and_extra_risks() -> None:
    findings = [Finding(level="warn", rule="precheck.x", excerpt="原句")]
    review_out = SemanticReview(
        judgements=[
            HitJudgement(
                rule="precheck.x", verdict="violation", reason="不行", replacement="改后的句子"
            )
        ],
        extra_risks=[
            ExtraRisk(excerpt="无出处的数据", risk="数据没有来源", suggestion="补来源或删掉")
        ],
    )
    merged, edits = apply(findings, review_out)
    assert merged[0].level == "block"
    assert edits["原句"] == "改后的句子"
    assert any(f.rule == "llm_semantic.extra_risk" for f in merged)
    assert edits["无出处的数据"] == "补来源或删掉"


# ------------------------------------------------------------------ inspect


def test_inspect_passes_clean_bundle() -> None:
    report = inspect(make_wechat_bundle())
    assert report.ok is True
    assert report.metrics["body_chars"] > MIN_BODY_CHARS


def test_inspect_blocks_over_long_title() -> None:
    report = inspect(make_wechat_bundle(title="标" * (MAX_TITLE_CHARS + 1)))
    assert report.ok is False
    assert any(f.rule == "inspect.title.too_long" for f in report.blocking)


def test_inspect_blocks_over_long_digest() -> None:
    report = inspect(make_wechat_bundle(digest="摘" * (MAX_DIGEST_CHARS + 1)))
    assert any(f.rule == "inspect.digest.too_long" for f in report.blocking)


def test_inspect_blocks_short_body() -> None:
    report = inspect(make_wechat_bundle(body="太短了"))
    assert any(f.rule == "inspect.body.too_short" for f in report.blocking)


def test_inspect_blocks_missing_body_html() -> None:
    report = inspect(make_wechat_bundle(body_html=None))
    assert any(f.rule == "inspect.body_html.missing" for f in report.blocking)


def test_inspect_blocks_external_images() -> None:
    """外链图片会被公众号直接过滤掉，必须先过素材库。"""
    bundle = make_wechat_bundle(
        body=LONG_BODY + "\n\n![图](https://cdn.example.com/a.png)",
        body_html='<p><img src="https://cdn.example.com/a.png"></p>',
    )
    report = inspect(bundle)
    assert any(f.rule == "inspect.image.external_host" for f in report.blocking)
    assert report.metrics["external_images"] >= 1


def test_inspect_allows_mmbiz_images() -> None:
    bundle = make_wechat_bundle(
        body=LONG_BODY + "\n\n![图](https://mmbiz.qpic.cn/x/640)",
        body_html='<p><img src="https://mmbiz.qpic.cn/x/640"></p>',
    )
    report = inspect(bundle)
    assert not [f for f in report.blocking if f.rule == "inspect.image.external_host"]


def test_inspect_blocks_missing_local_media(tmp_path) -> None:
    bundle = make_wechat_bundle(media=[MediaAsset(path="does/not/exist.png", cover=True)])
    report = inspect(bundle, media_root=tmp_path)
    assert any(f.rule == "inspect.media.missing_file" for f in report.blocking)


def test_inspect_accepts_existing_local_media(tmp_path) -> None:
    image = tmp_path / "cover.png"
    image.write_bytes(b"fake")
    bundle = make_wechat_bundle(media=[MediaAsset(path="cover.png", cover=True)])
    report = inspect(bundle, media_root=tmp_path)
    assert not [f for f in report.blocking if f.rule == "inspect.media.missing_file"]
    assert report.metrics["has_cover"] is True


def test_inspect_flags_missing_platform_fields() -> None:
    bundle = make_wechat_bundle()
    bundle.platform_extra.pop("digest")
    report = inspect(bundle)
    assert any(f.rule == "inspect.platform_extra.missing" for f in report.findings)


def test_inspect_report_is_json_serialisable() -> None:
    """``inspect --json`` 形态必须能直接 dump。"""
    payload = json.loads(inspect(make_wechat_bundle()).model_dump_json())
    assert payload["ok"] is True
    assert "metrics" in payload and "findings" in payload


def test_iter_image_urls_reads_markdown_and_html() -> None:
    bundle = make_wechat_bundle(
        body="![a](https://a.test/1.png)", body_html='<img src="https://b.test/2.png">'
    )
    assert set(iter_image_urls(bundle)) == {"https://a.test/1.png", "https://b.test/2.png"}


# ------------------------------------------------------------------ pipeline


def test_review_runs_all_stages_and_passes(tmp_path) -> None:
    result = review(
        make_wechat_bundle(),
        options=ReviewOptions(use_llm=False, lexicon_dir=str(tmp_path), media_root=str(tmp_path)),
    )
    assert result.passed is True
    assert "lexicon" in result.stages_run
    assert "precheck" in result.stages_run
    assert "inspect" in result.stages_run
    assert result.stages_skipped["llm_semantic"] == "options.use_llm=False"


def test_review_blocks_on_precheck_hit(tmp_path) -> None:
    bundle = make_wechat_bundle(body=LONG_BODY + " 有人问代办假证靠不靠谱")
    result = review(
        bundle,
        options=ReviewOptions(use_llm=False, lexicon_dir=str(tmp_path), media_root=str(tmp_path)),
    )
    assert result.passed is False
    assert any("general-community-safety" in f.rule for f in result.blocking)


def test_review_item_moves_draft_to_draft_and_logs(session, tmp_path) -> None:
    """机器审核不代替人工卡点：跑完仍回 draft，只写 review_notes + 审计日志。"""
    from core.models import ContentItem

    account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
    bundle = make_wechat_bundle()
    item = ContentItem(
        id="itm_review",
        account_id=account.id,
        status=ContentStatus.DRAFT.value,
        bundle_json=bundle.model_dump(mode="json"),
    )
    session.add(item)
    session.flush()

    result = review_item(
        session,
        item,
        options=ReviewOptions(use_llm=False, lexicon_dir=str(tmp_path), media_root=str(tmp_path)),
    )

    assert result.passed is True
    assert item.status == ContentStatus.DRAFT.value
    assert item.review_notes
    logs = [log for log in item.review_logs if log.action == MACHINE_REVIEW_ACTION]
    assert len(logs) == 1
    assert logs[0].actor == "system"
    assert logs[0].after_json["passed"] is True
    assert "lexicon" in logs[0].after_json["stages_run"]


def test_review_item_rejects_non_draft_status(session) -> None:
    """已批准的内容不该被机器审核悄悄打回，那要走人工撤回。"""
    from core.models import ContentItem

    account = make_account(session, account_id="wechat-demo-01", platform="wechat_mp")
    item = ContentItem(
        id="itm_approved",
        account_id=account.id,
        status=ContentStatus.APPROVED.value,
        bundle_json=make_wechat_bundle().model_dump(mode="json"),
    )
    session.add(item)
    session.flush()
    with pytest.raises(ValueError, match="只接受 draft"):
        review_item(session, item, options=ReviewOptions(use_llm=False))


def test_review_with_llm_stage(session, tmp_path) -> None:
    """有命中时才调 LLM，且判定结果要合并回 findings。"""
    llm = ScriptedLLM(
        parsed_replies=[
            SemanticReview(
                judgements=[
                    HitJudgement(
                        rule="precheck.general-community-safety.G01",
                        verdict="safe",
                        reason="这是在提醒读者不要上当，属于正当语境",
                    )
                ]
            )
        ]
    )
    bundle = make_wechat_bundle(body=LONG_BODY + " 有人问代办假证靠不靠谱")
    result = review(
        bundle,
        llm=llm,
        options=ReviewOptions(lexicon_dir=str(tmp_path), media_root=str(tmp_path)),
    )
    assert "llm_semantic" in result.stages_run
    assert result.passed is True  # block 被语境判定降级为 info
    assert any(f.extra.get("llm_verdict") == "safe" for f in result.findings)


# ---------------------------------------------------------------- 抖音 (P3)


def _douyin_bundle(
    tmp_path,
    *,
    title: str = "通勤一小时，一年亏掉十五万",
    body: str | None = None,
    tags: list[str] | None = None,
    video: str | None = "tests/fixtures/video/sample.mp4",
    cover: bool = True,
) -> ContentBundle:
    from tests.p2_helpers import png_bytes

    media: list[MediaAsset] = []
    if video is not None:
        media.append(MediaAsset(path=video, kind="video"))
    if cover:
        path = tmp_path / "cover-vertical.png"
        path.write_bytes(png_bytes(1080, 1920))
        media.append(MediaAsset(path=str(path), kind="image", cover=True))
    return ContentBundle(
        id="itm_dy",
        account_id="douyin-demo-01",
        platform="douyin",
        title=title,
        body_markdown=body
        if body is not None
        else "通勤一小时的人，一年亏掉十五万。\n\n#通勤 #时间管理",
        media=media,
        tags=tags if tags is not None else ["通勤", "时间管理"],
    )


def test_inspect_passes_clean_douyin_bundle(tmp_path) -> None:
    report = inspect(_douyin_bundle(tmp_path))
    blocking = [f.rule for f in report.blocking]
    assert blocking == [], blocking
    assert report.metrics["douyin_video_count"] == 1
    assert report.metrics["douyin_video_width"] == 720
    assert report.metrics["douyin_video_seconds"] == 2.0
    # 时长/分辨率不依赖 ffprobe：标准库解析 MP4 box 就能读出来
    assert report.metrics["douyin_probe"] == "mp4"


def test_inspect_blocks_missing_video(tmp_path) -> None:
    report = inspect(_douyin_bundle(tmp_path, video=None))
    assert any(f.rule == "inspect.douyin.video.missing" for f in report.blocking)


def test_inspect_blocks_missing_cover(tmp_path) -> None:
    report = inspect(_douyin_bundle(tmp_path, cover=False))
    assert any(f.rule == "inspect.douyin.cover.missing" for f in report.blocking)


def test_inspect_blocks_title_over_30_chars(tmp_path) -> None:
    from review.inspect import MAX_DOUYIN_TITLE_CHARS

    report = inspect(_douyin_bundle(tmp_path, title="标" * (MAX_DOUYIN_TITLE_CHARS + 1)))
    assert any(f.rule == "inspect.title.too_long" for f in report.blocking)


def test_inspect_blocks_non_vertical_video(tmp_path, monkeypatch) -> None:
    """横屏成片发抖音等于白发，这条必须 block。"""
    from review import inspect as inspect_mod

    monkeypatch.setattr(
        inspect_mod,
        "read_video_info",
        lambda path: inspect_mod.VideoInfo(1920, 1080, 30.0, source="stub"),
    )
    report = inspect(_douyin_bundle(tmp_path))
    hit = [f for f in report.blocking if f.rule == "inspect.douyin.video.aspect"]
    assert hit and "9:16" in hit[0].suggestion


def test_inspect_blocks_video_over_15_minutes(tmp_path, monkeypatch) -> None:
    from review import inspect as inspect_mod

    monkeypatch.setattr(
        inspect_mod,
        "read_video_info",
        lambda path: inspect_mod.VideoInfo(1080, 1920, 16 * 60.0, source="stub"),
    )
    report = inspect(_douyin_bundle(tmp_path))
    assert any(f.rule == "inspect.douyin.video.too_long" for f in report.blocking)


def test_inspect_warns_but_does_not_block_unreadable_video(tmp_path) -> None:
    """量不出来 = "没量到"，不能变成"内容不合规"。"""
    junk = tmp_path / "clip.mp4"
    junk.write_bytes(b"not really a video")
    report = inspect(_douyin_bundle(tmp_path, video=str(junk)))
    rules = [f.rule for f in report.findings]
    assert "inspect.douyin.video.unreadable" in rules
    assert not any(f.rule.startswith("inspect.douyin.video.") for f in report.blocking)


def test_inspect_blocks_two_videos(tmp_path) -> None:
    bundle = _douyin_bundle(tmp_path)
    bundle = bundle.model_copy(
        update={
            "media": [
                *bundle.media,
                MediaAsset(path="tests/fixtures/video/sample.mp4", kind="video"),
            ]
        }
    )
    assert any(f.rule == "inspect.douyin.video.too_many" for f in inspect(bundle).blocking)


def test_inspect_allows_short_douyin_caption(tmp_path) -> None:
    """短视频的信息在片子里，40 字文案是正常的（公众号会被判 too_short）。"""
    report = inspect(_douyin_bundle(tmp_path))
    assert not any(f.rule == "inspect.body.too_short" for f in report.findings)


def test_inspect_warns_on_douyin_tag_count(tmp_path) -> None:
    report = inspect(_douyin_bundle(tmp_path, tags=["通勤"]))
    assert any(f.rule == "inspect.douyin.tags.count" for f in report.findings)
    assert not any(f.rule == "inspect.douyin.tags.count" for f in report.blocking)


def test_review_pipeline_covers_douyin(tmp_path) -> None:
    """precheck / lexicon 都要覆盖抖音，话题也要送审。"""
    from review.pipeline import review_text

    bundle = _douyin_bundle(tmp_path, tags=["通勤", "赌博平台"])
    assert "#赌博平台" in review_text(bundle)
    result = review(bundle, options=ReviewOptions(use_llm=False))
    assert set(result.stages_run) >= {"lexicon", "precheck", "inspect"}
    assert not result.passed
    assert any("赌博" in f.excerpt for f in result.blocking)


# --------------------------------------------- 拉丁词条必须按词边界匹配


def _scan_with(words: list[str], text: str) -> list[str]:
    automaton = lexicon_mod.AhoCorasick()
    for w in words:
        automaton.add(w, "test")
    lex = lexicon_mod.Lexicon(automaton=automaton, categories={"test": len(words)})
    return [f.extra["word"] for f in lexicon_mod.scan(text, lex)]


def test_a_latin_entry_does_not_fire_inside_a_longer_english_word() -> None:
    """真实事故：装上完整词库后，配图 prompt「minimalist illustration」命中了「ma」。

    词库里有 76 条纯 ASCII 短词（`ma` `64` `AV` `BJ` `CBD` `CCTV`…），拿它们对拉丁
    文本做**子串**匹配必然误报：`ma` 藏在 `minimalist` / `format` / `many` 里，
    `av` 藏在 `available` 里。而配图 prompt 一律是英文，于是**每条稿子**
    都挂一条 warn，autopilot 的判据是 block 0 且 warn 0，流水线就静默停在 draft，
    没有任何测试会红——和题图误报同一种坏法。
    """
    assert _scan_with(["ma"], "minimalist illustration of a bowl") == []
    assert _scan_with(["av"], "available now") == []
    assert _scan_with(["64"], "总共 640 元") == []


def test_a_latin_entry_still_fires_when_it_stands_alone() -> None:
    """边界规则**不是**把这些词条静默停用——独立成词时照报。

    也别指望它治语义误报：``在 CBD 上班`` 里 CBD 前后都是空格、确实独立成词，
    照样命中。词边界只解决"藏在更长的词里"这一类，"这个词在语境里其实无害"
    是另一回事，归 llm_semantic 那一档管。
    """
    assert _scan_with(["ma"], "the ma is here") == ["ma"]
    assert _scan_with(["cbd"], "在 CBD 上班") == ["cbd"]


def test_a_cjk_entry_still_matches_as_a_substring() -> None:
    """中文没有词边界，子串匹配是对的，边界规则不许波及它。"""
    assert _scan_with(["赌博"], "这是一个赌博网站") == ["赌博"]
