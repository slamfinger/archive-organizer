# -*- coding: utf-8 -*-
"""按规则表执行移动。默认干跑（只打印将发生什么）；--execute 才真动文件；
每次执行写移动日志，--undo 可整体回滚。所有移动重名自动加序号、绝不覆盖。

规则 CSV（3列，含表头，utf-8 可用 Excel 编辑）:
  动作,源,目标
  并目录,<根内源目录>,<根内目标目录>    源目录整棵树的文件压平并入目标（收口/不增层）
  移目录,<根内源目录>,<根内目标目录>    源目录整体搬到目标下（保留子结构）
  移文件,<文件名关键词>,<根内目标目录>  全根文件名含该关键词的文件移入目标

借鉴 organize 工具的 journal+undo、rclone/Finder 的重名加序号；
路径全部相对 --root 解析并做越界校验（旧版把文件移到当前工作目录——已修）。

用法:
  python3 ao_classify.py --root /path/to/项目 --rules rules.csv            # 干跑预览
  python3 ao_classify.py --root /path/to/项目 --rules rules.csv --execute  # 真正执行
  python3 ao_classify.py --root /path/to/项目 --rules rules.csv --undo     # 回滚上次执行
"""
import os
import argparse
import datetime
from collections import defaultdict

from ao_common import (Excludes, walk_rel, ensure_inside, unique_dst,
                       move_file, Journal, read_csv_rows, workdir, WORKDIR_NAME,
                       norm_name, dir_contains)
from ao_protect import ensure_unlocked, relock
from ao_state import load_protect, protect_key, do_snapshot

ACTIONS = ("并目录", "移目录", "移文件")


def load_rules(path):
    rows = read_csv_rows(path)
    if not rows:
        raise SystemExit(f"[错误] 规则表为空: {path}")
    rules = []
    for i, row in enumerate(rows):
        if i == 0 and any("动作" in c for c in row):
            continue  # 表头
        if len(row) < 3 or not row[0].strip():
            continue
        action, src, dst = (c.strip() for c in row[:3])
        if action not in ACTIONS:
            raise SystemExit(f"[错误] 规则表第{i + 1}行动作未知: {action}（可选: {', '.join(ACTIONS)}）")
        rules.append((action, src, dst))
    if not rules:
        raise SystemExit(f"[错误] 规则表没有任何规则行: {path}")
    return rules


def _protected(rel, size, prot):
    """文件是否在人工保护清单（按 文件名+大小 识别）。"""
    return protect_key(os.path.basename(rel), size) in prot


def collect_rules_moves(rules, root, ex, prot=frozenset()):
    """把规则展开成具体移动清单 [(rule_idx, src_abs, dst_abs)]。
    命中人工保护清单的文件一律跳过（人工调整不可被规则回改）。"""
    plan = []
    n_prot = 0
    for idx, (action, src, dst) in enumerate(rules):
        dst_abs = os.path.join(root, dst)
        ensure_inside(root, dst_abs)
        if action == "移文件":
            for kind, rel, abs_ in walk_rel(root, ex):
                if kind != "f" or src not in norm_name(os.path.basename(rel)):
                    continue
                if os.path.dirname(abs_) == dst_abs:
                    continue  # 已在目标目录，跳过
                if _protected(rel, os.path.getsize(abs_), prot):
                    n_prot += 1
                    continue
                plan.append((idx, abs_, os.path.join(dst_abs, os.path.basename(abs_)), None))
            continue
        src_abs = os.path.join(root, src)
        if not os.path.isdir(src_abs):
            print(f"[警告] 规则{idx + 1}: 源目录不存在，跳过 → {src}")
            continue
        ensure_inside(root, src_abs)
        if src_abs == dst_abs or dir_contains(src_abs, dst_abs):
            raise SystemExit(f"[拒绝] 规则{idx + 1}: 目标不能等于/位于源内部 → {dst}")
        keep = (action == "移目录")
        for kind, rel, abs_ in walk_rel(src_abs, ex):
            if kind != "f":
                continue
            if _protected(rel, os.path.getsize(abs_), prot):
                n_prot += 1
                continue
            tail = rel if keep else os.path.basename(rel)
            plan.append((idx, abs_, os.path.join(dst_abs, tail), None))
    if n_prot:
        print(f"[保护] 按人工保护清单跳过 {n_prot} 个文件（人工调整不回改）")
    # 冲突可见化：被多条规则命中（目标不同）的文件，干跑阶段列给人看。
    # 裁决仍按既定契约「规则顺序先到先得」，不阻断——要改顺序请调整规则表。
    multi = defaultdict(set)
    for idx, s_abs, d_abs, _sz in plan:
        multi[s_abs].add((idx, os.path.relpath(d_abs, root)))
    conflicts = {s: v for s, v in multi.items() if len({d for _i, d in v}) > 1}
    if conflicts:
        print(f"[冲突提示] {len(conflicts)} 个文件被多条规则命中（目标不同），按规则顺序先到先得：")
        for s, v in list(sorted(conflicts.items()))[:8]:
            print(f"    {os.path.relpath(s, root)[:66]}")
            for idx, d in sorted(v):
                print(f"        规则{idx + 1} → {d[:70]}")
        if len(conflicts) > 8:
            print(f"    …（其余 {len(conflicts) - 8} 个略）")
    return plan


def apply_plan(plan, root, journal_path, confirm=False, batch=None):
    journal = Journal(journal_path)
    moved = skipped = 0
    drifted = 0
    touched_rules = set()  # 真正搬走文件的规则，事后才清理其源目录空壳
    try:
        for i, (idx, src_abs, dst_abs, plan_size) in enumerate(plan):
            if not os.path.exists(src_abs):
                skipped += 1
                continue
            rel_src = os.path.relpath(src_abs, root)
            rel_dst = os.path.relpath(dst_abs, root)
            cur_size = os.path.getsize(src_abs)
            if plan_size is not None and cur_size != plan_size:
                drifted += 1
                print(f"  [注意] 文件在计划生成后有过改动（{plan_size}B → {cur_size}B），仍按已批准计划移动: {rel_src}")
            if confirm:
                if input(f"  移动 {rel_src} → {rel_dst} ? (y/N): ").strip().lower() != "y":
                    continue
            final = move_file(src_abs, dst_abs)
            note = "" if final == dst_abs else f"  (重名→{os.path.basename(final)})"
            print(f"  [{i + 1}/{len(plan)}] {rel_src} → {os.path.relpath(final, root)}{note}")
            journal.log(rel_src, os.path.relpath(final, root), batch=batch)
            touched_rules.add(idx)
            moved += 1
    finally:
        journal.close()
    if drifted:
        print(f"  [注意] 共 {drifted} 个文件与计划时大小不同（已按批准计划移动，内容以现文件为准）")
    return moved, skipped, touched_rules


def prune_empty_dirs(start_abs, stop_abs):
    """自底向上清理空壳目录：先清 start 子树内的空目录，再向上清到 stop。
    目录已消失视为可继续向上；任何非空目录立即停（隐藏文件也算占用）。
    start 允许传入文件路径（回滚后的日志目标），自动从其所在目录开始。"""
    stop = os.path.abspath(stop_abs)
    cur = os.path.abspath(start_abs)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)   # 审计P0-1：日志目标是文件，不能对文件 listdir
    if not os.path.isdir(cur):
        return
    for dirpath, _, _ in os.walk(cur, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
            else:
                break
        except OSError:
            break
    while cur != stop:
        try:
            if os.path.exists(cur) and os.listdir(cur):
                break
        except OSError:
            break
        try:
            if os.path.exists(cur):
                os.rmdir(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)  # 已消失的子目录视为可清，继续向上


def do_execute(rules, root, ex, args, prot=frozenset()):
    plan = collect_rules_moves(rules, root, ex, prot)
    by_rule = {}
    for idx, _s, _d, _sz in plan:
        by_rule[idx] = by_rule.get(idx, 0) + 1
    print(f"[干跑将执行] 共 {len(plan)} 个文件移动：")
    for idx, (action, src, dst) in enumerate(rules):
        print(f"  规则{idx + 1} {action}: {src} → {dst}  ({by_rule.get(idx, 0)}个文件)")
    for idx, src_abs, dst_abs, _sz in plan[:args.samples]:
        print(f"    例: {os.path.relpath(src_abs, root)} → {os.path.relpath(dst_abs, root)}")
    if len(plan) > args.samples:
        print(f"    …（其余 {len(plan) - args.samples} 条略）")
    if not args.execute:
        print("→ 当前为干跑预览。确认无误后加 --execute 真正执行。")
        return

    batch = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    n_before = sum(len(fs) for r_, d_, fs in os.walk(root) if WORKDIR_NAME not in r_.split(os.sep))
    moved, skipped, touched = apply_plan(plan, root, args.journal, confirm=args.confirm, batch=batch)
    # 源目录若已搬空，清掉残留空壳（只清真搬过文件的，不动原有空目录）
    for idx in touched:
        action, src, dst = rules[idx]
        if action in ("移目录", "并目录"):
            src_abs = os.path.join(root, src)
            if os.path.isdir(src_abs):
                prune_empty_dirs(src_abs, root)
    print(f"✓ 批次 {batch}：已移动 {moved} 个文件（跳过 {skipped} 个已消失），日志: {os.path.abspath(args.journal)}")
    print("→ 如需撤销: 同命令加 --undo-last（只回滚本批次）或 --undo（回滚全部）")
    if args.execute:
        do_snapshot(root)  # 执行成功即刷新位置基线，供下轮增量/人工调整检测
        n_after = sum(len(fs) for r_, d_, fs in os.walk(root) if WORKDIR_NAME not in r_.split(os.sep))
        verdict = "守恒 ✓" if n_before == n_after else "有增减，请核对！"
        print(f"  终验：文件总数 执行前 {n_before} → 执行后 {n_after}（{verdict}）")


def undo_entries(root, entries):
    undone = 0
    for e in reversed(entries):  # 逆序回滚
        src_abs = os.path.join(root, e["src"])
        dst_abs = os.path.join(root, e["dst"])
        if not os.path.exists(dst_abs):
            print(f"[跳过] 目标已不在: {e['dst']}")
            continue
        if os.path.exists(src_abs):
            src_abs = unique_dst(src_abs)  # 原位已被占，放回旁边的序号位
        os.makedirs(os.path.dirname(src_abs), exist_ok=True)
        os.rename(dst_abs, src_abs)
        # 清理起点是目标所在目录（文件已搬走，目录才可能空）；传文件路径会在
        # prune 的守卫处因“已不存在”直接返回，空壳清理静默失效（三轮审计P1）
        prune_empty_dirs(os.path.dirname(dst_abs), root)
        print(f"  {e['dst']} → {os.path.relpath(src_abs, root)}")
        undone += 1
    print(f"✓ 已回滚 {undone}/{len(entries)} 条移动。日志保留（可重复回滚追溯更早批次）。")


def do_undo(root, args):
    entries = Journal.entries(args.journal)
    if not entries:
        raise SystemExit(f"[错误] 日志为空或不存在: {args.journal}")
    undo_entries(root, entries)


def do_undo_last(root, args):
    """只回滚最近一次执行（按日志里最后一个批次号；旧版日志无批次号则等同整体回滚）。"""
    entries = Journal.entries(args.journal)
    if not entries:
        raise SystemExit(f"[错误] 日志为空或不存在: {args.journal}")
    last = entries[-1].get("batch")
    if not last:
        print("[提示] 日志为旧版格式（无批次号），--undo-last 等同于整体回滚。")
        undo_entries(root, entries)
        return
    subset = [e for e in entries if e.get("batch") == last]
    undo_entries(root, subset)


def do_undo_batch(root, args, bid):
    entries = [e for e in Journal.entries(args.journal) if e.get("batch") == bid]
    if not entries:
        raise SystemExit(f"[错误] 日志中没有批次 {bid}（用 --list-batches 查看）")
    undo_entries(root, entries)


def do_list_batches(root, args):
    entries = Journal.entries(args.journal)
    if not entries:
        raise SystemExit(f"[错误] 日志为空或不存在: {args.journal}")
    groups = {}
    order = []
    for e in entries:
        b = e.get("batch") or "(旧版无批次)"
        if b not in groups:
            groups[b] = {"n": 0, "t0": e["time"], "t1": e["time"], "first": e["src"]}
            order.append(b)
        groups[b]["n"] += 1
        groups[b]["t1"] = e["time"]
    print(f"共 {len(entries)} 条移动，{len(order)} 个批次：")
    for b in order:
        g = groups[b]
        print(f"  {b}  {g['n']:>5} 条  {g['t0']} ~ {g['t1']}  首条: {g['first'][:60]}")
    print("→ 回滚指定批次: --undo-batch <批次号>；回滚最近一批: --undo-last")


def main():
    ap = argparse.ArgumentParser(description="按规则表执行文件移动（默认干跑）")
    ap.add_argument("--root", required=True, help="归档根目录")
    ap.add_argument("--rules", action="append", default=None,
                    help="规则CSV，可多次传入按顺序合并（默认 <root>/归档整理/归档规则.csv）；"
                         "增量归档可同时给 --rules 归档规则.csv --rules 学习规则.csv")
    ap.add_argument("--execute", action="store_true", help="真正执行移动（缺省只干跑预览）")
    ap.add_argument("--confirm", action="store_true", help="执行前逐项确认")
    ap.add_argument("--undo", action="store_true", help="按日志逆序回滚全部移动")
    ap.add_argument("--undo-last", action="store_true", help="只回滚最近一次执行（最近批次）")
    ap.add_argument("--undo-batch", default=None, metavar="ID", help="回滚指定批次")
    ap.add_argument("--list-batches", action="store_true", help="列出日志中的全部批次")
    ap.add_argument("--journal", default=None,
                    help="移动日志路径（默认 <root>/归档整理/.ao_journal.jsonl）")
    ap.add_argument("--samples", type=int, default=5, help="干跑时每类展示的样例数（默认5）")
    ap.add_argument("--exclude", action="append", default=[], help="追加忽略名称/模式（移文件规则用）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    wd = workdir(root)  # 产物目录（规则/日志默认都在这里）
    if not args.rules:
        args.rules = os.path.join(wd, "归档规则.csv")
    if not args.journal:
        args.journal = os.path.join(wd, ".ao_journal.jsonl")

    if args.undo:
        do_undo(root, args)
        return
    if args.undo_last:
        do_undo_last(root, args)
        return
    if args.undo_batch:
        do_undo_batch(root, args, args.undo_batch)
        return
    if args.list_batches:
        do_list_batches(root, args)
        return

    rules_paths = args.rules or [os.path.join(wd, "归档规则.csv")]
    rules = []
    for rp in rules_paths:
        if not os.path.exists(rp):
            raise SystemExit(f"[错误] 规则表不存在: {rp}")
        rules.extend(load_rules(rp))
    if len(rules_paths) > 1:
        print(f"[规则] 已合并 {len(rules_paths)} 份规则表，共 {len(rules)} 条（按传入顺序裁决）")
    ex = Excludes(args.exclude)
    ex.names.add(WORKDIR_NAME)  # 移文件规则不扫产物目录
    was = ensure_unlocked(root)  # 产物目录若已锁定，先临时解锁
    try:
        do_execute(rules, root, ex, args, prot=load_protect(root))
    finally:
        relock(root, was)


if __name__ == "__main__":
    main()
