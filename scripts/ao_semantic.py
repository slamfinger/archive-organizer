# -*- coding: utf-8 -*-
"""语义画像与错位探测（纯通用：画像由目录树自身推导，不含任何项目词）。

原理：
  1. 域画像：统计每个一级域内「二级目录名 + 文件名」的词元（extract_runs）
     出现文件数。
  2. 词元区分度 w(词, 域) = 该域含此词的文件数 ÷ 所有域含此词文件数合计。
     各域都常见的词（如 通知/表）区分度趋零，天然不干扰判断——这正是
     「同一词在不同域含义不同」的细微语义得以分辨的机制：词本身不决定
     归属，词在各域画像中的分布决定归属。
  3. 文件得分 S(文件, 域) = 其文件名词元的 Σw(词, 域)。
  4. 他域最高分 > 本域分 × --margin 且差值 ≥ --min-diff → 错位候选。

输出 <root>/归档整理/错位候选.csv（人审用，绝不自动移动）：
  文件 | 现位置 | 建议域 | 依据词元(域:权重) | 本域分 | 建议域分
人工评审确认后，翻译成归档规则.csv 走 verify → 干跑 → 确认 → execute。

用法:
  python3 ao_semantic.py --root /path/to/项目 [--margin 1.6] [--min-diff 1.0] [--top 200]
"""
import os
import re
import argparse
import collections

from ao_common import Excludes, walk_rel, workdir, extract_runs, write_csv_rows, WORKDIR_NAME

IDENT_RE = re.compile(r"[\dA-Za-z]")


def is_identifier(tok):
    """无汉字且数字占比过半的词元视为编号/桩号/日期类标识符，不具语义。"""
    if any("\u4e00" <= ch <= "\u9fff" for ch in tok):
        return False
    digits = sum(ch.isdigit() for ch in tok)
    return digits * 2 >= len(tok) and len(tok) >= 1


def semantic_tokens(name):
    return {t for t in extract_runs(name) if not is_identifier(t)}


def build(root, ex):
    """返回 (files, prof)：files=[(rel, tokens(set), domain)]，prof={域: Counter(词:文件数)}"""
    files, prof = [], collections.defaultdict(collections.Counter)
    for kind, rel, a in walk_rel(root, ex):
        if kind != "f":
            continue
        parts = rel.split(os.sep)
        if len(parts) < 2:
            continue  # 根下散落文件无域归属，不参与画像
        dom = parts[0]
        toks = semantic_tokens(os.path.basename(rel))
        if not toks:
            continue
        files.append((rel, toks, dom))
        seen = set(toks)
        for p in parts[1:-1]:  # 目录名也计入画像（不计入文件打分）
            seen |= semantic_tokens(p)
        for t in seen:
            prof[dom][t] += 1
    return files, prof


def weights(prof):
    """w(词,域) = 域内文件数 ÷ 全域合计；单域独占词权重=1。"""
    total = collections.Counter()
    for d, c in prof.items():
        for t in c:
            total[t] += c[t]
    w = {}
    for d, c in prof.items():
        for t, n in c.items():
            w[(t, d)] = n / total[t]
    return w


def main():
    ap = argparse.ArgumentParser(description="语义错位探测（输出候选CSV供人审）")
    ap.add_argument("--root", required=True, help="归档根目录")
    ap.add_argument("--margin", type=float, default=1.6, help="他域/本域分数比阈值（默认1.6）")
    ap.add_argument("--min-diff", type=float, default=1.0, help="分数差绝对值下限（默认1.0）")
    ap.add_argument("--top", type=int, default=200, help="候选输出上限")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    ex = Excludes()
    ex.names.add(WORKDIR_NAME)

    files, prof = build(root, ex)
    w = weights(prof)

    cands = []
    for rel, toks, dom in files:
        scores = collections.Counter()
        basis = collections.defaultdict(list)
        for t in toks:
            for d, c in prof.items():
                if c.get(t):
                    scores[d] += w[(t, d)]
                    basis[d].append((t, round(w[(t, d)], 2)))
        if not scores:
            continue
        own = scores.get(dom, 0.0)
        other = (scores - collections.Counter({dom: own})).most_common()
        if not other:
            continue
        b_dom, b_score = other[0]
        if b_score > own * args.margin and b_score - own >= args.min_diff:
            cands.append((b_score - own, rel, dom, b_dom, own, b_score,
                          sorted(basis[b_dom], key=lambda x: -x[1])[:4]))

    cands.sort(key=lambda x: -x[0])
    cands = cands[:args.top]
    out = os.path.join(workdir(root), "错位候选.csv")
    rows = [["文件", "现位置域", "建议域", "本域分", "建议域分", "依据词元", "相对路径"]]
    for gap, rel, dom, b_dom, own, b_score, basis in cands:
        rows.append([os.path.basename(rel), dom, b_dom, round(own, 2), round(b_score, 2),
                     " ".join(f"{t}:{v}" for t, v in basis), rel])
    write_csv_rows(out, rows)
    print(f"✓ 扫描 {len(files)} 个文件，检出错位候选 {len(cands)} 个 → {out}")
    print("→ 候选仅为画像推导的提示，须人工逐条评审后翻译成归档规则.csv")
    for gap, rel, dom, b_dom, own, b_score, basis in cands[:15]:
        print(f"  [{dom} → {b_dom}] {os.path.basename(rel)[:60]}  "
              f"({own:.2f}→{b_score:.2f}, {' '.join(t for t, _ in basis)})")


if __name__ == "__main__":
    main()
