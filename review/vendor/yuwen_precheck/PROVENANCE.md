# vendored: yuwen-cool/yuwen-publish-precheck

| 项 | 值 |
|---|---|
| 上游 | https://github.com/yuwen-cool/yuwen-publish-precheck |
| License | MIT（原文见同目录 `LICENSE`，Copyright (c) 2026 yuwen-cool） |
| 取用分支 | `master`（**注意不是 `main`**） |
| 取用文件 | `scripts/terms.json` → 本目录 `terms.json` |
| 取用日期 | 2026-08-15 |

## 为什么是 vendored 而不是依赖

上游**没有发布到 PyPI 也没有发布到 npm**，仓库里没有 `pyproject.toml` / `setup.py` /
`package.json`，也没有包目录与 `__init__.py`——它的形态是一个 **Claude Agent Skill**
（`SKILL.md` + `scripts/scan.py` CLI），无法 `pip install`，也无法作为库 `import`。

因此按任务书的兜底方案 vendored 拷入，并且**只拷数据文件**：

- ✅ `scripts/terms.json`——纯规则数据（41 条 term + 3 条 debunked_myth）。
- ✅ `LICENSE`——MIT 全文，随数据保留。
- ❌ **不拷 `SKILL.md` / `references/*.md`**。这些是写给 AI agent 执行的指令性文本；
  把它们放进本仓库会让"上游文档"变成对我们自己 agent 的隐式指令，属于提示注入面。
  规则数据（正则 + 严重度）不含指令，是安全的。
- ❌ **不拷 `scripts/scan.py`**。匹配逻辑我们自己实现（`review/precheck.py`），
  这样能接上本项目的 `Finding` 契约、`ContentBundle` 输入与中文注释规范，
  也避免把上游的 CLI/文件系统假设（`data/my-rules.md`、`data/wordlists/`）带进来。

## terms.json 结构

```json
{
  "generated_at": "...",
  "terms": [
    {
      "rule": "general-community-safety.G01",
      "title": "违法犯罪与违禁活动",
      "severity": "critical",          // critical | high | medium
      "commercial_only": false,        // 仅带货 / 商业场景生效
      "industries": [],                // 空 = 通用；否则命中行业才生效
      "pattern": "代办假证|出售枪支|…", // Python 正则（选择分支）
      "doc": "general-community-safety"
    }
  ],
  "debunked_myths": [
    {
      "pattern": "赚米|搞米|…",
      "myth": "……（被平台辟谣的错误认知）",
      "note": "……",
      "suggestion": "……"
    }
  ]
}
```

`industries` 实际出现的取值：`education`、`finance`、`health`、`health_food`、
`medical`、`medical_beauty`、`pharma`。

`debunked_myths` 是**反向**规则：命中说明作者在做没必要的"谐音规避"，
输出 `info` 级提示建议改回正常表达，不是违规。

## 更新方式

```bash
uv run python scripts/fetch_lexicon.py --precheck-only
```

或手工：

```bash
curl -fsSL -o review/vendor/yuwen_precheck/terms.json \
  https://raw.githubusercontent.com/yuwen-cool/yuwen-publish-precheck/master/scripts/terms.json
```

更新后请同步本文件的"取用日期"，并跑 `uv run pytest tests/test_review_precheck.py`。
