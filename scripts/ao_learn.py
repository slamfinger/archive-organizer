# -*- coding: utf-8 -*-
"""自学习：从人工调整（两次基线之间的移位/改名）中提炼可复用规则。

信号：位置基线 diff 的「移位/改名」文件对 (旧路径 → 新路径) 就是人工教学样本。
提炼两类规则写入 归档整理/学习规则.csv（语法与归档规则.csv 完全一致，多一列依据）：
  移目录  同一 (源目录 → 目标目录) 迁移样本 ≥ --min-dir 时，整目录收编：
          该目录里剩下的以及以后进来的文件都归同一去处。
  移文件  归入同一目标目录的样本中提炼「区分性关键词」：该词在全树只出现在
          这批样本的文件名里（零误伤），支持样本 ≥ --min-kw，贪心去同义词。

学习结果只是建议，须人审后用于增量归档：
  ao_classify --rules 归档规则.csv --rules 学习规则.csv
被人工移动过的文件已由 ao_state 保护清单兜底，规则永不回改它们。
"""
import os
import json
import csv
import argparse
from collections import defaultdict, Counter

from ao_common import (Excludes, walk_rel, workdir, read_csv_rows,
                       extract_runs, norm_name, dir_contains, WORKDIR_NAME)
from ao_protect import ensure_unlocked, relock

SNAP = ".位置快照.json"


def moved_pairs(root, snap_path):
    if not os.path.exists(snap_path):
        raise SystemExit("[提示] 尚无位置基线，先执行 ao_state.py snapshot（人工调整后勿刷新，直接学习）")
    with open(snap_path, encoding="utf-8") as f:
        base = json.load(f)
    base_keys = defaultdict(list)
    for r, s in base.items():
        base_keys[(os.path.basename(r), s)].append(r)
    ex = Excludes()
    ex.names.add(WORKDIR_NAME)
    pairs, ambiguous = [], []
    for kind, rel, a in walk_rel(root, ex):
        if kind != "f" or rel in base:
            continue
        olds = base_keys.get((os.path.basename(rel), os.path.getsize(a)))
        if not olds:
            continue
        if len(olds) > 1:
            ambiguous.append((rel, olds))  # 审计P1-1：同名同大小多候选，来源歧义拒学防污染
            continue
        pairs.append((olds[0], rel))
    return pairs, ambiguous, base


def load_known(paths):
    """现有规则（用于去重）：{(动作,源,目标)}"""
    known = set()
    for p in paths:
        if p and os.path.exists(p):
            for r in read_csv_rows(p)[1:]:
                if len(r) >= 3:
                    known.add(tuple(c.strip() for c in r[:3]))
    return known


def learn(pairs, root, min_dir, min_kw, known, base):
    rules = []  # (动作, 源, 目标, 依据)

    # ---- 1. 目录对规则：用户把源目录内容整体搬空（样本=基线全量）才学「整目录收编」；
    #         只搬了零散几件的，不学目录级规则（防域根过度泛化），交给关键词规则 ----
    base_under_subtree = Counter()
    for r in base:
        parts = r.split(os.sep)
        for i in range(len(parts) - 1):          # 基线文件计入其每一层祖先目录的“子树全量”
            base_under_subtree[os.sep.join(parts[:i + 1])] += 1
    dir_groups = defaultdict(list)
    for o, n in pairs:
        sd, dd = os.path.dirname(o), os.path.dirname(n)
        if sd != dd:
            dir_groups[(sd, dd)].append((o, n))
    covered_old_dirs = set()
    for (sd, dd), samples in sorted(dir_groups.items(), key=lambda x: -len(x[1])):
        if len(samples) < min_dir:
            continue
        if len(samples) < base_under_subtree.get(sd, 0):
            continue  # 源子树还有未搬走的文件——不是整目录归并，宁可不学
        # 审计P0-2：与执行器同判——目标等于/位于源内部的规则，执行器会整体拒绝，提前跳过
        if dir_contains(os.path.join(root, sd), os.path.join(root, dd)):
            print(f"    [跳过] 目标等于/位于源内部，不学（样本{len(samples)}个）: {sd} → {dd}")
            continue
        rules.append(("移目录", sd, dd,
                      f"人工移位样本{len(samples)}个: " + "、".join(os.path.basename(n) for _o, n in samples[:4])
                      + ("…" if len(samples) > 4 else "")))
        covered_old_dirs.add(sd)

    # ---- 2. 关键词规则：按目标目录分组，贪心提炼零误伤区分词 ----
    by_dst = defaultdict(list)
    for o, n in pairs:
        if os.path.dirname(o) in covered_old_dirs:
            continue  # 已被移目录规则覆盖，不重复学
        by_dst[os.path.dirname(n)].append(n)
    ex = Excludes()
    ex.names.add(WORKDIR_NAME)
    tree_names = [norm_name(os.path.basename(rel)) for k, rel, _a in walk_rel(root, ex) if k == "f"]

    for dst_dir, samples in sorted(by_dst.items()):
        if len(samples) < min_kw:
            continue
        run_support = Counter()
        run_where = defaultdict(set)
        for rel in samples:
            for run in set(extract_runs(os.path.basename(rel))):
                run_support[run] += 1
                run_where[run].add(rel)
        covered = set()
        for kw, sup in sorted(run_support.items(), key=lambda x: (-x[1], -len(x[0]))):
            if sup < min_kw or run_where[kw] <= covered:
                continue
            total = sum(1 for name in tree_names if kw in name)
            if total > sup:
                continue  # 该词在树里还出现在非样本文件上 → 有误伤风险，不学
            rules.append(("移文件", kw, dst_dir,
                          f"当前树未发现误伤（全树仅{total}处，全在本组{sup}个样本中；新文件含该词将同归此目录）: "
                          + "、".join(sorted(os.path.basename(r) for r in run_where[kw])[:4])
                          + ("…" if len(run_where[kw]) > 4 else "")))
            covered |= run_where[kw]
            if len(covered) >= len(samples):
                break

    # ---- 3. 去重：与现有规则完全同 (动作,源/词,目标) 的不重复输出 ----
    rules = [r for r in rules if (r[0], r[1], r[2]) not in known]
    return rules


def main():
    ap = argparse.ArgumentParser(description="从人工调整提炼学习规则（只读，不移动文件）")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None, help="学习规则输出（默认 <root>/归档整理/学习规则.csv）")
    ap.add_argument("--known", action="append", default=[],
                    help="已有规则CSV（用于去重），可多次传入；默认含 归档规则.csv")
    ap.add_argument("--min-dir", type=int, default=2, help="目录对规则最少样本数（默认2）")
    ap.add_argument("--min-kw", type=int, default=2, help="关键词规则最少支持样本数（默认2）")
    ap.add_argument("--detail", action="store_true", help="打印全部教学样本")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    wd = workdir(root)
    out_path = args.out or os.path.join(wd, "学习规则.csv")
    known_paths = args.known or [os.path.join(wd, "归档规则.csv")]
    known_paths.append(out_path)

    pairs, ambiguous, base = moved_pairs(root, os.path.join(wd, SNAP))
    print(f"人工调整教学样本: {len(pairs)} 对移位")
    if args.detail:
        for o, n in pairs:
            print(f"   {o[:60]} → {n[:60]}")
    if ambiguous:
        print(f"歧义拒学 {len(ambiguous)} 个（同名同大小多候选，防止污染学习样本）:")
        for rel, olds in ambiguous[:8]:
            print(f"   ? {rel[:66]}  ← 候选来源: {' ; '.join(o[:40] for o in olds)}")

    known = load_known(known_paths)
    existing = []
    if os.path.exists(out_path):  # 学习规则增量合并：历史规则保留，只追加新学到的
        existing = read_csv_rows(out_path)[1:]
    was = ensure_unlocked(root)
    try:
        new_rules = learn(pairs, root, args.min_dir, args.min_kw, known, base)
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["动作", "源", "目标", "依据"])
            for r in existing:
                w.writerow(r)
            for r in new_rules:
                w.writerow(r)
    finally:
        relock(root, was)
    rules = existing + new_rules

    print(f"\n提炼学习规则 {len(rules)} 条 → {out_path}")
    for act, src, dst, why in rules:
        print(f"  [{act}] {src[:46]} → {dst[:46]}\n      依据: {why[:90]}")
    if rules:
        print("\n→ 人审后用于增量归档: ao_classify --rules 归档规则.csv --rules 学习规则.csv")
    else:
        print("→ 本次人工调整没有提炼出满足阈值的新规则。")


if __name__ == "__main__":
    main()
