#!/usr/bin/env python3
"""codereview —— LLM 代码评审：`git diff --staged` 进，结构化评审出。

用法：
    codereview                 # 评审已暂存 (staged) 的 diff
    codereview --all           # 评审未暂存 (unstaged) 的 diff
    codereview --range HEAD~2  # 评审指定区间的 diff
    codereview --dry-run       # 只打印 prompt + diff 统计，不联网
    codereview --strict        # 发现 blocker 时 exit 1（CI 门禁）

API Key 从环境变量 OPENAI_API_KEY 读取，可选 OPENAI_BASE_URL
（默认 https://api.openai.com/v1），兼容任何 OpenAI 兼容接口。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error

VERSION = "0.1.0"
MAX_DIFF_CHARS = 30_000
DEFAULT_MODEL = "gpt-4o-mini"

SEVERITIES = ("blocker", "major", "minor", "nit")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}  # blocker=0 最严重

# ---------------- i18n ----------------
STR = {
    "zh": {
        "sev": "严重程度", "loc": "文件:行", "issue": "问题", "sugg": "建议",
        "verdict_merge": "✅ 结论：可合并",
        "verdict_changes": "⛔ 结论：需修改后合并",
        "no_diff": "error: 没有可评审的 diff（暂存区为空）。先 git add 要评审的文件。",
        "no_diff_all": "error: 没有可评审的 diff（工作区干净）。",
        "no_diff_range": "error: 指定区间没有 diff：{r}",
        "not_repo": "error: 当前目录不是 git 仓库。请在 git 仓库内运行。",
        "git_fail": "error: git diff 执行失败：{e}",
        "no_key": "error: 未找到 API key。请先设置环境变量 OPENAI_API_KEY（或 --api-key）。",
        "net_fail": "error: 网络请求失败：{e}",
        "bad_resp": "error: 模型返回无法解析为评审 JSON：{e}",
        "diff_files": "diff 统计：{n} 个文件，+{a} -{d}",
        "diff_trunc": "（diff 过长，已截断至前 {n} 字符）",
        "prompt_head": "===== 将发送给模型的 prompt（dry-run，不联网）=====",
        "blockers_found": "error: 发现 {n} 个 blocker，--strict 拒绝通过。",
        "api_key_flag": "--api-key",
    },
    "en": {
        "sev": "Severity", "loc": "File:Line", "issue": "Issue", "sugg": "Suggestion",
        "verdict_merge": "✅ Verdict: LGTM, safe to merge",
        "verdict_changes": "⛔ Verdict: changes requested before merge",
        "no_diff": "error: nothing to review (staging area is empty). git add files first.",
        "no_diff_all": "error: nothing to review (working tree is clean).",
        "no_diff_range": "error: no diff in range: {r}",
        "not_repo": "error: not a git repository. Run inside a git repo.",
        "git_fail": "error: git diff failed: {e}",
        "no_key": "error: API key not found. Set OPENAI_API_KEY env var (or --api-key).",
        "net_fail": "error: network request failed: {e}",
        "bad_resp": "error: model response is not valid review JSON: {e}",
        "diff_files": "diff stats: {n} files, +{a} -{d}",
        "diff_trunc": "(diff truncated to first {n} chars)",
        "prompt_head": "===== prompt that would be sent (dry-run, no network) =====",
        "blockers_found": "error: found {n} blocker(s), --strict gate failed.",
        "api_key_flag": "--api-key",
    },
}

PROMPT_ZH = """你是资深代码审查员。审查下面的 git diff，只输出 JSON，不要输出任何其他文字。

JSON 格式（严格遵守）：
{{"findings": [{{"severity": "blocker|major|minor|nit", "file": "文件路径", "line": 行号整数（不确定填 0）, "issue": "问题描述（一句话）", "suggestion": "修改建议（一句话）"}}], "verdict": "merge|needs_changes"}}

严重程度定义：
- blocker：不修复不能合并（安全漏洞如 SQL 注入/命令注入、数据丢失、必然崩溃）
- major：强烈建议修复（逻辑错误、并发问题、明显性能坑）
- minor：建议修复（边界条件、可读性、小缺陷）
- nit：可改可不改（风格、命名、注释）
{focus}
评审只针对 diff 中的新增/修改行，不要对未改动的旧代码发表意见。
下面是 git diff：
```
{diff}
```"""

PROMPT_EN = """You are a senior code reviewer. Review the git diff below. Output ONLY JSON, no other text.

Strict JSON format:
{{"findings": [{{"severity": "blocker|major|minor|nit", "file": "file path", "line": line number as integer (0 if unsure), "issue": "one-sentence problem", "suggestion": "one-sentence fix"}}], "verdict": "merge|needs_changes"}}

Severity definitions:
- blocker: must fix before merge (security vuln like SQL/command injection, data loss, certain crash)
- major: strongly recommended (logic bug, concurrency issue, obvious perf pitfall)
- minor: recommended (edge cases, readability, small defects)
- nit: optional (style, naming, comments)
{focus}
Only review added/modified lines in the diff; do not comment on untouched old code.
The git diff:
```
{diff}
```"""

FOCUS_LINES = {
    "zh": {"security": "本次请重点关注：安全问题（注入、密钥硬编码、越权、敏感信息泄露）。",
           "performance": "本次请重点关注：性能问题（复杂度、N+1、重复计算、阻塞调用）。",
           "style": "本次请重点关注：代码风格与可读性（命名、结构、注释）。"},
    "en": {"security": "Focus this review on: security (injection, hardcoded secrets, authz, sensitive data leaks).",
           "performance": "Focus this review on: performance (complexity, N+1, redundant work, blocking calls).",
           "style": "Focus this review on: code style and readability (naming, structure, comments)."},
}


# ---------------- git ----------------
def run_git(args: list[str]) -> str:
    try:
        p = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("git_not_found")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"exit {p.returncode}")
    return p.stdout


def get_diff(mode: str, lang: str) -> str:
    t = STR[lang]
    try:
        inside = run_git(["rev-parse", "--is-inside-work-tree"]).strip()
        if inside != "true":
            raise RuntimeError("not_repo")
    except RuntimeError as e:
        if str(e) == "not_repo" or "not a git repository" in str(e).lower():
            print(t["not_repo"], file=sys.stderr)
            sys.exit(1)
        print(t["git_fail"].format(e=e), file=sys.stderr)
        sys.exit(1)

    try:
        if mode == "staged":
            diff = run_git(["diff", "--staged", "--no-color", "-U3"])
            empty_msg = t["no_diff"]
        elif mode == "all":
            diff = run_git(["diff", "--no-color", "-U3"])
            empty_msg = t["no_diff_all"]
        else:  # range
            diff = run_git(["diff", "--no-color", "-U3", mode])
            empty_msg = t["no_diff_range"].format(r=mode)
    except RuntimeError as e:
        print(t["git_fail"].format(e=e), file=sys.stderr)
        sys.exit(1)

    if not diff.strip():
        print(empty_msg, file=sys.stderr)
        sys.exit(1)
    return diff


def diff_stats(diff: str) -> tuple[int, int, int]:
    files, adds, dels = set(), 0, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:])
        elif line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return len(files), adds, dels


# ---------------- llm ----------------
def build_prompt(diff: str, lang: str, focus: str | None) -> str:
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
    tpl = PROMPT_ZH if lang == "zh" else PROMPT_EN
    focus_line = FOCUS_LINES[lang].get(focus, "") if focus else ""
    return tpl.format(diff=diff, focus=focus_line)


def call_llm(prompt: str, model: str, base_url: str, api_key: str, lang: str) -> str:
    t = STR[lang]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(t["net_fail"].format(e=f"HTTP {e.code}: {detail}"), file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # URLError / timeout / JSON
        print(t["net_fail"].format(e=e), file=sys.stderr)
        sys.exit(1)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(t["bad_resp"].format(e="unexpected response shape"), file=sys.stderr)
        sys.exit(1)


def strip_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", s, re.S)
    return m.group(1) if m else s


def parse_review(raw: str, lang: str) -> dict:
    t = STR[lang]
    try:
        data = json.loads(strip_fences(raw))
        findings = data.get("findings", [])
        clean = []
        for f in findings:
            sev = str(f.get("severity", "minor")).lower()
            if sev not in SEV_RANK:
                sev = "minor"
            try:
                line = int(f.get("line", 0))
            except (TypeError, ValueError):
                line = 0
            clean.append({
                "severity": sev,
                "file": str(f.get("file", "?")),
                "line": line,
                "issue": str(f.get("issue", "")),
                "suggestion": str(f.get("suggestion", "")),
            })
        verdict = str(data.get("verdict", "")).lower()
        if verdict not in ("merge", "needs_changes"):
            verdict = "needs_changes" if any(f["severity"] == "blocker" for f in clean) else "merge"
        return {"findings": clean, "verdict": verdict}
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        print(t["bad_resp"].format(e=e), file=sys.stderr)
        sys.exit(1)


# ---------------- output ----------------
def dwidth(s: str) -> int:
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E7F and not ch.isascii() else 1
    return w


def trunc(s: str, width: int) -> str:
    s = s.replace("\n", " ").strip()
    if dwidth(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E7F and not ch.isascii() else 1
        if w + cw > width - 1:
            return out + "…"
        out += ch
        w += cw
    return out


def print_table(review: dict, lang: str, min_sev: str) -> None:
    t = STR[lang]
    findings = [f for f in review["findings"]
                if SEV_RANK[f["severity"]] <= SEV_RANK[min_sev]]
    findings.sort(key=lambda f: (SEV_RANK[f["severity"]], f["file"], f["line"]))

    headers = [t["sev"], t["loc"], t["issue"], t["sugg"]]
    rows = [[f["severity"],
             f"{f['file']}:{f['line']}" if f["line"] else f["file"],
             trunc(f["issue"], 34),
             trunc(f["suggestion"], 40)] for f in findings]

    widths = [8, 22, 36, 42]
    widths = [max(w, dwidth(h)) for w, h in zip(widths, headers)]
    for r in rows:
        widths = [max(w, dwidth(c)) for w, c in zip(widths, r)]

    def fmt(cells):
        return " | ".join(c + " " * (w - dwidth(c)) for c, w in zip(cells, widths))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))
    if not rows:
        print("—" if lang == "zh" else "(no findings at this severity)")

    print()
    if review["verdict"] == "merge":
        print(t["verdict_merge"])
    else:
        print(t["verdict_changes"])


# ---------------- cli ----------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codereview",
        description="LLM 代码评审：git diff 进，结构化评审出（严重程度分级）。")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--all", action="store_true", help="评审未暂存的改动（默认评审已暂存）")
    src.add_argument("--range", metavar="R", help="评审指定区间，如 HEAD~2")
    p.add_argument("--severity", choices=SEVERITIES, default="nit",
                   help="只显示该级别及以上的发现（默认 nit，即全部）")
    p.add_argument("--focus", choices=("security", "performance", "style"),
                   help="让模型聚焦某个维度")
    p.add_argument("--strict", action="store_true",
                   help="发现 blocker 时 exit 1（CI 门禁）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印 prompt + diff 统计，不联网")
    p.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    p.add_argument("--lang", choices=("zh", "en"), default="zh", help="界面语言")
    p.add_argument("--model", default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL))
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL",
                                                        "https://api.openai.com/v1"))
    p.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lang = args.lang
    t = STR[lang]

    mode = "staged"
    if args.all:
        mode = "all"
    elif args.range:
        mode = args.range
    diff = get_diff(mode, lang)

    n, adds, dels = diff_stats(diff)
    truncated = len(diff) > MAX_DIFF_CHARS
    prompt = build_prompt(diff, lang, args.focus)

    if args.dry_run:
        print(t["diff_files"].format(n=n, a=adds, d=dels))
        if truncated:
            print(t["diff_trunc"].format(n=MAX_DIFF_CHARS))
        print(t["prompt_head"])
        print(prompt)
        return 0

    if not args.api_key:
        print(t["no_key"], file=sys.stderr)
        return 1

    raw = call_llm(prompt, args.model, args.base_url, args.api_key, lang)
    review = parse_review(raw, lang)

    if args.json:
        out = {"findings": [f for f in review["findings"]
                            if SEV_RANK[f["severity"]] <= SEV_RANK[args.severity]],
               "verdict": review["verdict"]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(t["diff_files"].format(n=n, a=adds, d=dels))
        print()
        print_table(review, lang, args.severity)

    if args.strict:
        blockers = sum(1 for f in review["findings"] if f["severity"] == "blocker")
        if blockers:
            if not args.json:
                print(file=sys.stderr)
            print(t["blockers_found"].format(n=blockers), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
