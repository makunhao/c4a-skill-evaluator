#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_benchmark.py —— 用黄金集量化评审器的可靠性。

    输入：benchmark/fixtures/（8 份缺陷已知的提交）+ benchmark/golden_set.json
    输出：否决项误报率 / 漏报率 / 完整性一致率 / 条件分落点率 / 结论一致率
    退出码：0 = 没有新回归；1 = 出现基线之外的新错误（可直接接进 CI）

为什么这个文件是 C4A 的核心
--------------------------
C4A 的 rubric 第一条是「**评审器质量（25分）——自动评审器/评测器的质量与可靠性**」。
一个评审器"能跑"和"判得准"是两件事；能证明后者的只有一件事：
**用一批缺陷已知的样本跑一遍，把误报与漏报数出来。**

所以这里的指标不是装饰，它是这个交付物的主证据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import skill_evaluator as se  # noqa: E402

GOLDEN = ROOT / "benchmark" / "golden_set.json"
BASELINE = ROOT / "benchmark" / "baseline.json"

C_OK, C_BAD, C_WARN, C_DIM, C_END = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser(description="评审器黄金集基准")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="列出全部不一致")
    args = ap.parse_args()

    rubric = se.load_rubric()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    fixtures = ROOT / "benchmark" / "fixtures"

    evals = {e["author"]: e for e in se.evaluate(fixtures, rubric)}

    issues: list[dict] = []
    cells = 0
    checks = 0

    for case in golden["cases"]:
        aid = case["author"]
        e = evals.get(aid)
        if e is None:
            issues.append({"case": case["id"], "kind": "missing",
                           "id": aid, "direction": "用例缺失",
                           "note": f"评审器没有输出 {aid} 这一组"})
            continue

        # ── 否决项：误报 / 漏报 ──
        got_bl = " ".join(b["id"] for b in e["blockers"])
        for want in case["expect_blockers"]:
            checks += 1
            if want not in got_bl:
                issues.append({"case": case["id"], "kind": "blocker", "id": want,
                               "direction": "漏报",
                               "note": f"期望拦下『{want}』，实际否决项：{got_bl or '（无）'}"})
        if not case["expect_blockers"] and e["blockers"]:
            checks += 1
            issues.append({"case": case["id"], "kind": "blocker", "id": got_bl,
                           "direction": "误报",
                           "note": f"本不该否决，却报了：{got_bl}"})

        # ── 完整性 ──
        checks += 1
        if e["completeness"]["hits"] != case["expect_completeness_hits"]:
            issues.append({"case": case["id"], "kind": "completeness", "id": "hits",
                           "direction": "判定不一致",
                           "note": f"期望 {case['expect_completeness_hits']}/5，"
                                   f"实际 {e['completeness']['hits']}/5"})
        if case.get("expect_missing_file"):
            checks += 1
            if e["completeness"]["files"][case["expect_missing_file"]]["present"]:
                issues.append({"case": case["id"], "kind": "completeness",
                               "id": case["expect_missing_file"],
                               "direction": "误判", "note": "期望缺失，却判为存在"})

        # ── 条件分落点 ──
        for cid, (lo, hi) in case.get("expect_condition_bands", {}).items():
            cells += 1
            got = e["conditions"][cid]["score"]
            if not (lo <= got <= hi):
                issues.append({"case": case["id"], "kind": "band", "id": cid,
                               "direction": "落在区间外",
                               "note": f"期望 [{lo}, {hi}]，实际 {got}"})

        # ── 维度分落点 ──
        for did, (lo, hi) in case.get("expect_dim_bands", {}).items():
            cells += 1
            got = e["scores"][did]["pct"]
            if not (lo <= got <= hi):
                issues.append({"case": case["id"], "kind": "band", "id": did,
                               "direction": "落在区间外",
                               "note": f"期望 [{lo}, {hi}]，实际 {got}"})

        # ── 结论 ──
        checks += 1
        verdict = ("不合格" if e["blockers"]
                   else ("合格" if sum(v["predicted"] for v in e["scores"].values()) >= 70
                         else "待改进"))
        if verdict != case["expect_verdict"]:
            issues.append({"case": case["id"], "kind": "verdict", "id": "verdict",
                           "direction": "判定不一致",
                           "note": f"期望『{case['expect_verdict']}』，实际『{verdict}』"})

    fp = [i for i in issues if i["kind"] == "blocker" and i["direction"] == "误报"]
    fn = [i for i in issues if i["kind"] == "blocker" and i["direction"] == "漏报"]

    summary = {
        "cases": len(golden["cases"]),
        "evaluated": len(evals),
        "blocker_false_positive": len(fp),
        "blocker_false_negative": len(fn),
        "checks": checks,
        "band_cells": cells,
        "consistency": round((checks - len(issues)) / checks * 100, 1) if checks else 0.0,
        "band_hit_rate": round((cells - len([i for i in issues if i["kind"] == "band"]))
                               / cells * 100, 1) if cells else 0.0,
        "total_issues": len(issues),
    }

    if args.json:
        print(json.dumps({"summary": summary, "issues": issues},
                         ensure_ascii=False, indent=2))
        return 1 if issues else 0

    print("=" * 74)
    print(f"评审器黄金集 · {summary['cases']} 份样本（评审出 {summary['evaluated']} 组）")
    print("-" * 74)
    print(f"  否决项·误报        ：{C_OK if not fp else C_BAD}{len(fp)}{C_END}"
          f"   {C_DIM}← 冤枉一个认真交作业的同学，最坏的结果{C_END}")
    print(f"  否决项·漏报        ：{C_OK if not fn else C_BAD}{len(fn)}{C_END}"
          f"   {C_DIM}← 放过了别人根本跑不起来的提交{C_END}")
    print(f"  完整性/结论一致率  ：{summary['consistency']:>5.1f}%"
          f"  {C_DIM}({summary['checks']} 项检查){C_END}")
    print(f"  分值落点命中率     ：{summary['band_hit_rate']:>5.1f}%"
          f"  {C_DIM}({summary['band_cells']} 个区间){C_END}")
    print("-" * 74)

    if args.all or issues:
        by_case: dict[str, list[dict]] = {}
        for i in issues:
            by_case.setdefault(i["case"], []).append(i)
        for cid, lst in sorted(by_case.items()):
            case = next(c for c in golden["cases"] if c["id"] == cid)
            flag = C_BAD if any(i["kind"] == "blocker" for i in lst) else C_WARN
            print(f"{flag}{cid}{C_END} {case['title']}")
            for i in lst:
                print(f"    [{i['direction']}] {i['id']:<12} {i['note']}")
        print("-" * 74)

    if issues:
        print(f"{C_WARN}⚠ 共 {len(issues)} 处不一致{C_END}")
    else:
        print(f"{C_OK}✓ 全部通过：{len(golden['cases'])} 份样本的否决项、完整性、"
              f"分值落点、结论都符合预期{C_END}")

    if BASELINE.exists():
        base = json.loads(BASELINE.read_text(encoding="utf-8"))["summary"]
        worse = [f"{k}: {base[k]} → {summary[k]}"
                 for k in ("blocker_false_positive", "blocker_false_negative",
                           "total_issues") if summary[k] > base[k]]
        for k in ("consistency", "band_hit_rate"):
            if summary[k] < base[k] - 0.05:
                worse.append(f"{k}: {base[k]} → {summary[k]}")
        if worse:
            print(f"{C_BAD}✗ 相比基线变差：{'; '.join(worse)}{C_END}")
            print("=" * 74)
            return 1
        print(f"{C_DIM}  与基线一致（误报 {base['blocker_false_positive']} / "
              f"漏报 {base['blocker_false_negative']} / 不一致 {base['total_issues']}）{C_END}")

    print("=" * 74)
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
