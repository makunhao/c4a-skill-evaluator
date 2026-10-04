#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill_evaluator.py —— C4 技能提交自动评审器（C4A）

    输入：一个包含 C4 提交文件的本地文件夹
    输出：Markdown 评审报告（+ JSON）

三级流水线：
    L1 文件采集与识别   → 按 `_C4_` 命名识别作者，回退到文件夹名/署名
    L2 提交完整性检查   → 五个必须文件是否齐全
    L3 技能质量评审     → 四条件（可复用 / 可执行 / 可验证 / IO 明确）

设计要点（详见 references/rubric.json 的 design_principle）
------------------------------------------------------------
1. **规则层只判确定性才能判准的**：文件结构、命名规范、包完整性、
   写死的绝对路径、私有资源依赖、计数。这些 LLM 也能做，但会不稳定，
   而『可复用』这一条的核心风险恰恰是绝对路径与私有依赖——
   这类检查必须确定性，否则一次误判就是冤枉一个同学。

2. **语义部分刻意不判**：『IO 说得清不清楚』『教学文档是不是真能教会人』
   『复盘是不是真反思』——写正则去猜只会得到一堆假报。
   这些列在 rubric.json 的 semantic_checks 里，交给可选的 LLM 层。

3. **输出分对齐 C4 的真实 rubric 五个维度**，而不是自造一套分数。
   否则谁也说不清它评得准不准；对齐之后，就能拿预测分与真实得分做对照。

安全边界：本工具**只读文件，绝不执行被评审对象里的任何代码**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
DEFAULT_RUBRIC = HERE.parent / "references" / "rubric.json"

TEXT_EXT = {".md", ".txt", ".py", ".js", ".sh", ".json", ".yaml", ".yml",
            ".html", ".css", ".csv", ".ts", ".toml", ".ini", ".cfg", ".tex"}
MEDIA_EXT = {".mp4", ".mov", ".gif", ".png", ".jpg", ".jpeg", ".webp", ".svg"}
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


def load_rubric(path: Path | None = None) -> dict:
    p = path or DEFAULT_RUBRIC
    if not p.exists():
        raise SystemExit(f"[skill-evaluator] 找不到判据表：{p}\n"
                         f"  用 --rubric <路径> 指定，或确认 references/rubric.json 存在。")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 文本读取（只读，绝不执行）
# ---------------------------------------------------------------------------
def read_text(p: Path) -> str:
    """读一个文件的文本内容。二进制（图片/视频）与不可解析格式返回空串。"""
    ext = p.suffix.lower()
    if ext in MEDIA_EXT or p.name.lower() in SKIP_NAMES:
        return ""
    if ext == ".docx":
        return _read_docx(p)
    if ext in TEXT_EXT:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def _read_docx(p: Path) -> str:
    """.docx 就是个 zip，正文在 word/document.xml。用标准库直接读，不装包。"""
    try:
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


# ---------------------------------------------------------------------------
# L1 文件采集与识别
# ---------------------------------------------------------------------------
def extract_author(name: str, rubric: dict) -> str | None:
    m = re.match(rubric["naming"]["author_extract"], name)
    return m.group(1) if m else None


def scan(root: Path, rubric: dict) -> dict[str, dict]:
    """返回 {作者: {files: [...], notes: [...]}}。

    先按 `_C4_` 命名识别；识别不到的作者名，**不丢**——
    回退到"文件名前缀"并如实标注『作者名待人工确认』。
    丢掉比认错更糟：丢掉会让一个真交了作业的人变成"没交"。
    """
    marker = rubric["naming"]["challenge_marker"]
    groups: dict[str, dict] = {}

    def slot(author: str, note: str | None = None) -> dict:
        g = groups.setdefault(author, {"author": author, "files": [], "notes": []})
        if note and note not in g["notes"]:
            g["notes"].append(note)
        return g

    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.lower() in SKIP_NAMES or ".git" in p.parts:
            continue
        author = extract_author(p.name, rubric)
        if author:
            slot(author)["files"].append(p)
            continue
        # 回退 1：文件名里没标记，但父目录名像作者
        if marker in str(p) or re.search(r"_C4", p.name, re.I):
            author = p.parent.name if p.parent != root else p.stem.split("_")[0]
            slot(author, f"『{p.name}』未按 `_C4_` 规范命名，已按上下文归入此作者")["files"].append(p)
            continue
        # 回退 2：完全无从归属 —— 单独成组，绝不静默丢弃
        slot("（未识别作者）", "这些文件既没有 `_C4_` 标记，也无法从上下文推断作者"
             )["files"].append(p)

    return groups


# ---------------------------------------------------------------------------
# L2 提交完整性检查
# ---------------------------------------------------------------------------
def check_completeness(group: dict, texts: dict[Path, str], rubric: dict) -> dict:
    hits = {}
    for rf in rubric["required_files"]:
        matched = []
        for f in group["files"]:
            name = f.name
            ext = f.suffix.lower()
            if rf["ext"] and ext not in rf["ext"]:
                continue
            by_name = any(pat.lower() in name.lower() for pat in rf["filename_patterns"])
            body = texts.get(f, "")
            need = rf.get("content_min_hits", 0)
            by_content = (need > 0 and
                          sum(1 for s in rf["content_signals"] if s in body) >= need)
            if rf.get("content_kind") == "executable":
                by_content = by_content or _is_executable(f, body)
            if by_name or by_content:
                matched.append(f.name)
        hits[rf["id"]] = {"present": bool(matched), "evidence": matched,
                          "hint": rf["hint"]}

    n = sum(1 for v in hits.values() if v["present"])
    total = len(rubric["required_files"])
    if n == total:
        level = "✅ 齐全"
    elif n >= total - 1:
        level = "⚠️ 部分缺失"
    else:
        level = "❌ 严重缺失"
    return {"files": hits, "hits": n, "total": total, "level": level}


# ---------------------------------------------------------------------------
# 检测器（确定性）
# ---------------------------------------------------------------------------
def _is_executable(p: Path, body: str = "") -> bool:
    if p.suffix.lower() in {".skill", ".py", ".sh", ".js", ".ts"}:
        return True
    if p.name == "SKILL.md" and re.search(r"^---\s*$", body or "", re.M):
        return True
    return bool(re.search(r"^```", body or "", re.M))


def _skill_package_valid(p: Path) -> tuple[bool, str]:
    """.skill 必须是可解包容器，且**顶层**含 SKILL.md。

    SKILL.md 埋在子目录里是常见的"装不上"，所以这条要精确到层级。
    """
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
        else:
            import tarfile
            if not tarfile.is_tarfile(p):
                return False, "既不是 zip 也不是 tar，无法解包"
            with tarfile.open(p) as t:
                names = t.getnames()
    except Exception as e:                                    # noqa: BLE001
        return False, f"解包失败：{type(e).__name__}"

    if not names:
        return False, "包是空的"
    depths = [n.count("/") for n in names if n.endswith("SKILL.md")]
    if not depths:
        return False, "包内没有 SKILL.md"
    if min(depths) > 1:
        return False, f"SKILL.md 埋在子目录里（深度 {min(depths)}），装不上"
    return True, "结构正常"


def run_detector(name: str, group: dict, texts: dict[Path, str]) -> tuple[bool, str]:
    files = group["files"]
    if name == "has_executable":
        for f in files:
            if _is_executable(f, texts.get(f, "")):
                return True, f"发现可执行产物：{f.name}"
        return False, "没有任何可执行产物（.skill/.py/.sh，或含 frontmatter 的 SKILL.md，或代码块）"

    if name == "skill_package_valid":
        pkgs = [f for f in files if f.suffix.lower() == ".skill"]
        if not pkgs:
            return False, "没有提交 .skill 包（如果用的是源码目录形式，这一条不适用，已在报告中标注）"
        oks, msgs = [], []
        for p in pkgs:
            ok, msg = _skill_package_valid(p)
            oks.append(ok)
            msgs.append(f"{p.name}：{msg}")
        return any(oks), "；".join(msgs)

    if name == "has_demo":
        hit = [f.name for f in files if f.suffix.lower() in MEDIA_EXT]
        return bool(hit), ("发现 demo 产物：" + "、".join(hit[:3])) if hit else "没有图片/视频类 demo"

    return False, f"未知检测器 {name}"


def eval_hard_gates(group: dict, texts: dict[Path, str], conditions: dict,
                    rubric: dict) -> list[dict]:
    """硬门槛：不满足则整体判不合格，无论其余部分多好。

    ⚠️ 第一版漏了这一层——rubric.json 里定义了 hard_gates，
    但代码只从 condition.rules 里收集 blockers，于是 hard_gates 从来没被执行过，
    『没有可执行产物』这种明确的不合格也没被拦下。
    这类 bug 危险在于：**它不报错，只是安静地少判了一件事。**
    """
    out = []
    for item in rubric["hard_gates"]["items"]:
        gid = item["id"]
        fired, why = False, ""
        if gid == "has_executable":
            ok, msg = run_detector("has_executable", group, texts)
            fired, why = (not ok), msg
        elif gid == "empty_submission":
            fired = len(group["files"]) == 0
            why = "提交目录里没有任何文件"
        elif gid == "absolute_path_blocker":
            hits = [r["id"] for r in conditions["reusable"]["details"]
                    if r["kind"] == "blocker" and r["hit"]]
            fired = bool(hits)
            why = "命中的否决项：" + "、".join(hits) if hits else ""
        if fired:
            out.append({"id": item["label"], "why": why or item["why"]})
    return out


# ---------------------------------------------------------------------------
# L3 四条件评审
# ---------------------------------------------------------------------------
def eval_conditions(group: dict, texts: dict[Path, str], rubric: dict) -> dict:
    blob = "\n".join(texts.values())
    out = {}
    for cond in rubric["conditions"]:
        got = total = 0
        details, blockers = [], []
        for r in cond["rules"]:
            total += r["weight"]
            if r.get("detector"):
                ok, why = run_detector(r["detector"], group, texts)
            else:
                m = re.search("|".join(f"(?:{p})" for p in r["patterns"]), blob, re.M) \
                    if r["patterns"] else None
                ok = m is not None
                if ok:
                    # 评审依据要写**命中的内容**，不是命中的正则。
                    # 「命中『(?<![A-Za-z])[A-Za-z]:[\\/]+Users』」给不了任何人信息；
                    # 「命中：D:\Users\yanpo\...」才能让人一眼判断判得对不对。
                    snip = m.group(0)
                    snip = (snip[:60] + "…") if len(snip) > 60 else snip
                    why = f"命中：{snip}"
                else:
                    why = "未命中 — " + r["note"]
            if r["kind"] == "blocker":
                if ok:
                    blockers.append({"id": r["id"], "why": why})
                else:
                    got += r["weight"]          # 没踩坑 = 拿满这一条的权重
            else:
                if ok:
                    got += r["weight"]
            details.append({"id": r["id"], "kind": r["kind"], "hit": ok,
                            "weight": r["weight"], "why": why})
        pct = round(got / total * 100, 1) if total else 0.0
        # 命中否决项 ⇒ 这条条件**本身**就不成立。
        # 否则会出现「🚫 不合格，但可复用 76.2%」这种自相矛盾的读数——
        # 而那条 76.2% 是因为"没踩坑的否决项拿了满分"，纯属加权方式的产物。
        if blockers:
            pct = 0.0
        out[cond["id"]] = {"id": cond["id"], "label": cond["label"],
                           "question": cond["question"], "score": pct,
                           "details": details, "blockers": blockers}
    return out


# ---------------------------------------------------------------------------
# 评分：映射到 C4 的真实 rubric 维度
# ---------------------------------------------------------------------------
def score_c4(completeness: dict, conditions: dict, texts: dict[Path, str],
             rubric: dict, blockers: list[dict] | None = None) -> dict:
    blob = "\n".join(texts.values())
    blocked = bool(blockers)
    out = {}

    for dim in rubric["c4_dimensions"]:
        maxp = dim["max_points"]
        if dim["source"] == "conditions":
            if blocked:
                # C4 的 CHALLENGE.md 原文：「你提交的技能必须**同时**满足以下四个条件」。
                # 所以任一条件被否决不成立时，技能质量这一维度就是 0，而不是"扣一点"。
                # 之前这里照常算平均分，会报出「🚫 不合格，预测 78.8/100」这种自相矛盾的结果。
                pct = 0.0
                basis = "触发否决项 → C4 要求四条件**同时满足**，故技能质量判 0"
            else:
                vals = [conditions[i]["score"] for i in dim["source_ids"]]
                pct = sum(vals) / len(vals) if vals else 0.0
                basis = "；".join(f"{conditions[i]['label']} {conditions[i]['score']}%"
                                  for i in dim["source_ids"])
        elif dim["source"] == "required_files":
            if dim.get("source_id"):
                ref = next(r for r in rubric["required_files"] if r["id"] == dim["source_id"])
                blk = completeness["files"][ref["id"]]
                n_sig = sum(1 for s in ref["content_signals"] if s in blob)
                content_pct = min(100.0, n_sig / max(1, len(ref["content_signals"])) * 100)
                pct = (60.0 if blk["present"] else 0.0) + 0.4 * content_pct
                basis = (f"文件{'存在' if blk['present'] else '缺失'}；"
                         f"内容信号命中 {n_sig}/{len(ref['content_signals'])}")
            else:
                pct = completeness["hits"] / completeness["total"] * 100
                basis = f"五个必须文件命中 {completeness['hits']}/{completeness['total']}"
        elif dim["source"] == "content_scan":
            sig = ["失败", "踩坑", "坑", "问题", "改进", "下一次", "迭代", "教训", "复盘", "AAR"]
            n = sum(1 for s in sig if s in blob)
            # ⚠️ 上限刻意压到 60%：这一维度本质上靠语义判断，
            # 用关键词给满分等于把"靠语义的维度"用"靠词频的规则"判了高分。
            pct = min(60.0, n / len(sig) * 100)
            basis = (f"规则层只能给微弱信号（命中 {n}/{len(sig)} 个词），"
                     f"上限压到 60%，其余交给 LLM 层")
        else:
            pct, basis = 0.0, "未知来源"

        out[dim["id"]] = {"label": dim["label"], "max_points": maxp,
                          "predicted": round(maxp * pct / 100, 1),
                          "pct": round(pct, 1), "basis": basis,
                          "note": dim.get("note", "")}
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def render_author(author: str, group: dict, comp: dict, conds: dict,
                  scores: dict, rubric: dict) -> str:
    L = [f"## {author}", ""]
    if group["notes"]:
        for n in group["notes"]:
            L.append(f"> ⚠️ {n}")
        L.append("")

    L.append(f"**文件数**：{len(group['files'])}　|　"
             f"**完整性**：{comp['level']}（{comp['hits']}/{comp['total']}）")
    L.append("")

    blockers = [b for c in conds.values() for b in c["blockers"]]
    if blockers:
        L.append("### 🚫 否决项")
        for b in blockers:
            L.append(f"- **{b['id']}** — {b['why']}")
        L.append("")
        L.append(f"> 共 {len(blockers)} 项。它们直接击穿 C4 四条件里的第一条『可复用』"
                 f"（或『可执行』），因此整体判**不合格**——"
                 f"C4 的原文是四条件必须**同时满足**，与其余部分做得多好无关。")
        L.append("")

    L.append("### 五个必须文件")
    L.append("")
    L.append("| 文件 | 状态 | 命中依据 |")
    L.append("|---|---|---|")
    for rf in rubric["required_files"]:
        b = comp["files"][rf["id"]]
        ev = "、".join(b["evidence"][:2]) if b["evidence"] else "—"
        L.append(f"| {rf['label']} | {'✅' if b['present'] else '❌'} | {ev} |")
    L.append("")

    L.append("### 四条件评审（规则层）")
    L.append("")
    for cid in ("reusable", "executable", "verifiable", "clear_io"):
        c = conds[cid]
        flag = "✅" if c["score"] >= 75 else ("⚠️" if c["score"] >= 45 else "❌")
        L.append(f"**{flag} {c['label']} — {c['score']}%**　*{c['question']}*")
        L.append("")
        for d in c["details"]:
            mark = "✓" if d["hit"] else "✗"
            if d["kind"] == "blocker":
                mark = "🚫" if d["hit"] else "✓"
            L.append(f"- `{mark}` {d['id']} — {d['why']}")
        L.append("")

    L.append("### C4 维度预测分（对齐真实 rubric）")
    L.append("")
    L.append("| 维度 | 满分 | 预测 | 依据 |")
    L.append("|---|---|---|---|")
    tot = tot_max = 0.0
    for k, v in scores.items():
        tot += v["predicted"]
        tot_max += v["max_points"]
        L.append(f"| {v['label']} | {v['max_points']} | **{v['predicted']}** | {v['basis']} |")
    L.append(f"| **合计** | **{tot_max:.0f}** | **{tot:.1f}** | |")
    L.append("")
    L.append("> ⚠️ 这是**规则层**的预测分，不是最终分。"
             "靠语义判断的维度（可教性、复盘质量）规则层只能给下限；"
             "`reflectionQuality` 的上限被刻意压在 60%，"
             "**避免用词频去判一个本该靠理解判的维度**。")
    L.append("")
    return "\n".join(L)


def render(evaluations: list[dict], root: Path, rubric: dict) -> str:
    L = ["# C4 技能提交自动评审报告", "",
         f"- 输入目录：`{root}`",
         f"- 评审器：skill-evaluator v{__version__}",
         f"- 覆盖提交数：**{len(evaluations)}**",
         "- 判据：`references/rubric.json`（四条件 + 五个必须文件）",
         "",
         "---", ""]
    L.append("## 总览")
    L.append("")
    L.append("| 作者 | 文件数 | 完整性 | 可复用 | 可执行 | 可验证 | IO明确 | 预测总分 | 结论 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for e in evaluations:
        c = e["conditions"]
        tot = sum(v["predicted"] for v in e["scores"].values())
        verdict = "🚫 不合格" if e["blockers"] else ("✅ 合格" if tot >= 70 else "⚠️ 待改进")
        L.append(f"| {e['author']} | {len(e['group']['files'])} | {e['completeness']['level']} | "
                 f"{c['reusable']['score']}% | {c['executable']['score']}% | "
                 f"{c['verifiable']['score']}% | {c['clear_io']['score']}% | "
                 f"**{tot:.1f}** | {verdict} |")
    L.append("")
    L.append("---")
    L.append("")
    for e in evaluations:
        L.append(render_author(e["author"], e["group"], e["completeness"],
                               e["conditions"], e["scores"], rubric))
        L.append("---")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def evaluate(root: Path, rubric: dict) -> list[dict]:
    groups = scan(root, rubric)
    out = []
    for author, g in groups.items():
        texts = {f: read_text(f) for f in g["files"]}
        comp = check_completeness(g, texts, rubric)
        conds = eval_conditions(g, texts, rubric)
        # 两类否决项合并：条件里的 blocker + rubric 里定义的硬门槛。
        # 之前只收了前者，硬门槛形同虚设。
        cond_blockers = [b for c in conds.values() for b in c["blockers"]]
        gate_blockers = eval_hard_gates(g, texts, conds, rubric)
        blockers = cond_blockers + gate_blockers
        scores = score_c4(comp, conds, texts, rubric, blockers)
        out.append({"author": author, "group": {"author": author,
                                                "files": [f.name for f in g["files"]],
                                                "notes": g["notes"]},
                    "_paths": [str(f) for f in g["files"]],
                    "completeness": comp, "conditions": conds,
                    "scores": scores, "blockers": blockers})
    out.sort(key=lambda e: -(sum(v["predicted"] for v in e["scores"].values())))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="skill_evaluator",
        description="C4 技能提交自动评审器（L1 采集 → L2 完整性 → L3 四条件）",
        epilog="示例：\n"
               "  python skill_evaluator.py ./submissions\n"
               "  python skill_evaluator.py ./submissions --json > result.json\n")
    ap.add_argument("folder", help="包含 C4 提交文件的本地文件夹")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="把 Markdown 报告写到指定文件")
    ap.add_argument("--rubric", help="自定义判据表路径")
    args = ap.parse_args()

    root = Path(args.folder)
    if not root.is_dir():
        print(f"[skill-evaluator] 不是文件夹：{root}", file=sys.stderr)
        return 2

    rubric = load_rubric(Path(args.rubric) if args.rubric else None)
    evals = evaluate(root, rubric)

    if not evals:
        print(f"[skill-evaluator] 在 {root} 下没有找到任何文件。", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"tool": "skill-evaluator", "version": __version__,
                          "root": str(root), "evaluations": evals},
                         ensure_ascii=False, indent=2))
        return 0

    md = render(evals, root, rubric)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"✓ 报告已写入 {args.out}")
        print(f"  覆盖 {len(evals)} 份提交")
        for e in evals:
            tot = sum(v["predicted"] for v in e["scores"].values())
            flag = "🚫" if e["blockers"] else ("✅" if tot >= 70 else "⚠️")
            print(f"  {flag} {e['author']:<14} 预测 {tot:>5.1f}/100  "
                  f"完整性 {e['completeness']['hits']}/{e['completeness']['total']}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
