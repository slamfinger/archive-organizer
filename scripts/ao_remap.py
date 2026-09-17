# -*- coding: utf-8 -*-
"""骨架重排映射生成器：把项目自己的「骨架重排规则.csv」展开成逐文件「映射总表.csv」。

机制通用、数据归项目：本脚本不含任何具体项目的关键词，全部匹配规则来自规则 CSV。
换一个项目/机器，只需另写一份规则 CSV。

规则 CSV 列（utf-8-sig，表头: 类型,源或作用域,匹配,目标）：
  移树,<旧目录前缀>,,<新目录前缀>
      子树整树移植：前缀下所有文件保持源内相对层级搬到新前缀下；最长前缀优先。
  归槽,<作用域前缀>,<关键词>,<目标槽目录>
      未被任何移树前缀覆盖的文件，按「作用域最长优先」找到所属作用域，
      在该作用域规则行内按 CSV 顺序取第一个命中的关键词；目标自动拼接文件名
      （杜绝把目录槽当文件目标的事故）。文件名匹配前做去空白归一（"承 诺 函"≈"承诺函"）。
      空关键词 = 该作用域兜底行，务必放在该作用域最后一行。
  留守,<路径前缀或文件名关键词>,,
      跳过不进映射（垃圾文件）。内置默认留守：.DS_Store / Thumbs.db / desktop.ini / ~$* / ._* 。

产出：映射总表.csv + 流量矩阵 + 未决清单 + 归槽审计（按目录聚合，裸奔目录一眼可见）。
"""
import os
import csv
import argparse
from collections import Counter, defaultdict

from ao_common import workdir, read_csv_rows, WORKDIR_NAME, norm_name
from ao_protect import ensure_unlocked, relock

JUNK_PREFIX = ("~$", "._")
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def is_junk(name):
    return name in JUNK_NAMES or name.startswith(JUNK_PREFIX)


def load_rules(path):
    rows = read_csv_rows(path)
    rules = {"移树": [], "归槽": [], "留守": []}
    for i, r in enumerate(rows[1:], 2):
        if not r or not r[0].strip():
            continue
        t = r[0].strip()
        scope = r[1].strip() if len(r) > 1 else ""
        kw = r[2].strip() if len(r) > 2 else ""
        dst = next((c.strip() for c in r[3:] if c.strip()), "")  # 目标列容错：取首个非空
        if t == "移树":
            rules["移树"].append((scope, dst))
        elif t == "归槽":
            rules["归槽"].append((scope, kw, dst))
        elif t == "留守":
            rules["留守"].append((scope, kw))
        else:
            raise SystemExit(f"[错误] 规则第{i}行类型未知: {t}")
    rules["移树"].sort(key=lambda x: -len(x[0]))
    return rules


def main():
    ap = argparse.ArgumentParser(description="骨架重排映射生成（只读，不移动文件）")
    ap.add_argument("--root", required=True)
    ap.add_argument("--rules", default=None, help="规则CSV（默认 <root>/归档整理/骨架重排规则.csv）")
    ap.add_argument("--out", default=None, help="映射总表输出（默认 <root>/归档整理/映射总表.csv）")
    ap.add_argument("--detail", action="store_true", help="逐条打印归槽明细")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    wd = workdir(root)
    rules_path = args.rules or os.path.join(wd, "骨架重排规则.csv")
    out_path = args.out or os.path.join(wd, "映射总表.csv")
    if not os.path.exists(rules_path):
        raise SystemExit(f"[错误] 规则CSV不存在: {rules_path}")
    rules = load_rules(rules_path)
    was = ensure_unlocked(root)  # 产出目录若已锁定，先临时解锁，结束恢复
    try:
        report(rules_path, out_path, root, wd, rules, args)
    finally:
        relock(root, was)


def report(rules_path, out_path, root, wd, rules, args):
    transplants = rules["移树"]
    slots_by_scope = defaultdict(list)
    for scope, kw, dst in rules["归槽"]:
        slots_by_scope[scope].append((kw, dst))
    scopes_sorted = sorted(slots_by_scope, key=len, reverse=True)

    pairs, unassigned, dispatched, kept_back = [], [], [], []
    matrix = defaultdict(Counter)

    for r_, d_, f_ in os.walk(root):
        d_[:] = [x for x in d_ if x != WORKDIR_NAME and not x.startswith(".")]
        for fn in sorted(f_):
            rel = os.path.relpath(os.path.join(r_, fn), root)
            nfn = norm_name(fn)
            if is_junk(fn) or any(k and (k in rel or k in nfn) for k, _p in rules["留守"]):
                kept_back.append(rel)
                continue
            hit = None
            for src, dst in transplants:
                if rel.startswith(src.rstrip(os.sep) + os.sep):
                    hit = dst.rstrip("/") + "/" + os.path.relpath(rel, src).replace(os.sep, "/")
                    break
            if hit is None:
                for scope in scopes_sorted:
                    if not (rel.startswith(scope.rstrip(os.sep) + os.sep) or scope == ""):
                        continue
                    kw_hit = None
                    for kw, dst in slots_by_scope[scope]:
                        if kw == "" or kw in nfn:
                            kw_hit = dst
                            break
                    if kw_hit is not None:
                        hit = kw_hit.rstrip("/") + "/" + fn
                        dispatched.append((rel, hit))
                        break
            if hit is None:
                unassigned.append(rel)
                continue
            pairs.append((rel, hit, os.path.getsize(os.path.join(root, rel))))
            matrix[rel.split(os.sep)[0]][hit.split("/")[0]] += 1

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["现相对路径", "新相对路径", "计划时大小"])
        w.writerows(pairs)

    print(f"映射 {len(pairs)} 条 → {out_path}")
    print(f"留守 {len(kept_back)}: " + " ; ".join(kept_back[:6]) + ("…" if len(kept_back) > 6 else ""))
    print(f"未决 {len(unassigned)}（必须为 0 或逐条确认为有意留守）")
    for u in unassigned[:40]:
        print("   ?", u)

    print(f"\n归槽审计（{len(dispatched)} 条，按所在目录聚合——漏建移树的目录一眼可见）:")
    dir_cnt = Counter(os.path.dirname(rel) for rel, _ in dispatched)
    for d, n in sorted(dir_cnt.items()):
        print(f"   {n:>3}  {d if d else '.'}")
    if args.detail:
        for rel, dst in dispatched:
            print(f"   {rel[:70]}\n      → {dst}")

    print("\n旧域→新域 流量矩阵:")
    news = sorted({n for c in matrix.values() for n in c})
    print("   " + " ".join(n.split(".")[0].ljust(4) for n in news))
    for old in sorted(matrix):
        print(f"{old:<14} " + " ".join(f"{n.split('.')[0]}:{c}" for n, c in sorted(matrix[old].items())))


if __name__ == "__main__":
    main()
