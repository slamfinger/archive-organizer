# -*- coding: utf-8 -*-
"""逐文件映射总表回放器：把「映射总表.csv」批量落位。骨架重排、跨机对齐、迁移回放共用。

映射总表两列（现相对路径,新相对路径）或三列（第三列=计划时大小，ao_remap 生成）。
第三列用于 TOCTOU 核对：映射是跨 invocation 的持久化计划，若源文件在生成映射后
被改动（大小不一致），预检逐条提示——落位仍按已批准映射执行，内容以现文件为准。

安全纪律（与 ao_classify 相同 + 预检错误分级）：
- 默认干跑，--execute 才真移动；日志与 ao_classify 兼容（jsonl: time/batch/src/dst），
  ao_classify --undo / --undo-last 可回滚；
- 目标重名自动加 " (n)" 序号，绝不覆盖；移动后清理搬空的目录壳；
- 预检分级：hard（源缺失/重复/越界/垃圾/非法路径）任何参数不可放行；
  shared（≥3 行共享同一目标=目录槽事故嫌疑）仅 --allow-shared-dst 豁免。
"""
import os
import argparse
from collections import Counter

from ao_common import ensure_inside, prune_empty_chain, workdir, read_csv_rows, WORKDIR_NAME
from ao_protect import ensure_unlocked, relock
from ao_state import do_snapshot, load_protect, protect_key

JUNK_PREFIX = ("~$", "._")
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def is_junk(name):
    return name in JUNK_NAMES or name.startswith(JUNK_PREFIX)


def load_pairs(path):
    """兼容两列（旧版）与三列（含计划时大小）映射表。"""
    rows = read_csv_rows(path)
    if len(rows) < 2:
        raise SystemExit(f"[错误] 映射表为空: {path}")
    pairs = []
    for r in rows[1:]:
        if len(r) < 2 or not r[0].strip():
            continue
        size = r[2].strip() if len(r) > 2 and r[2].strip() else None
        pairs.append((r[0].strip(), r[1].strip(), int(size) if size else None))
    return pairs


def preflight(pairs, root):
    """错误分级：hard = 任何情况下不可执行；shared = 目录槽事故嫌疑，仅可被 --allow-shared-dst 豁免。"""
    hard, shared, warns, drifts = [], [], [], []
    src_seen, dst_cnt = {}, Counter()
    root_abs = os.path.realpath(root)

    def boundary(p):
        if not p:
            return "空路径"
        if p.endswith(os.sep) or p.endswith("/"):
            return "路径以分隔符结尾（疑似目录槽）"
        ap = os.path.realpath(os.path.join(root_abs, p))
        if ap == root_abs:
            return "目标为归档根本身"
        try:
            ensure_inside(root_abs, ap)
        except SystemExit:
            return "越界路径"
        return None

    for s, d, plan_size in pairs:
        if s in src_seen:
            hard.append(f"源重复: {s}")
        src_seen[s] = d
        if is_junk(os.path.basename(s)):
            hard.append(f"垃圾文件不应进映射表: {s}")
        if s == d:
            hard.append(f"源等于目标: {s}")
        e = boundary(s)
        if e:
            hard.append(f"源非法（{e}）: {s}")
        e = boundary(d)
        if e:
            hard.append(f"目标非法（{e}）: {d}")
        s_abs = os.path.join(root, s)
        if not os.path.isfile(s_abs):
            hard.append(f"源不存在: {s}")
        elif plan_size is not None and os.path.getsize(s_abs) != plan_size:
            drifts.append((s, plan_size, os.path.getsize(s_abs)))
        dst_cnt[d] += 1
    for d, n in dst_cnt.items():
        if n >= 3:
            shared.append(f"目标被{n}行共享（疑似目录槽当文件目标）: {d}")
        elif n == 2:
            warns.append(f"同目标2行（版本件将序号共存）: {d}")
    return hard, shared, warns, drifts


def main():
    ap = argparse.ArgumentParser(description="逐文件映射总表回放（默认干跑）")
    ap.add_argument("--root", required=True)
    ap.add_argument("--map", required=True, help="映射总表.csv（现相对路径,新相对路径[,计划时大小]）")
    ap.add_argument("--journal", default=None, help="日志路径（默认 <root>/归档整理/.ao_journal.jsonl）")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--allow-shared-dst", action="store_true",
                    help="放行 ≥3 行共享同一目标的映射（已确认非目录槽事故时使用）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    journal_path = args.journal or os.path.join(workdir(root), ".ao_journal.jsonl")

    pairs = load_pairs(args.map)
    hard, shared, warns, drifts = preflight(pairs, root)

    # 人工保护衔接：映射按「文件名+大小」对齐保护清单，命中即知悉提示。
    # 映射是整体已审方案（remap 从现树生成，人工调整后的现位置即映射源），
    # 故不阻断，但要让人看见；增量规则请勿回改这些文件。
    prot = load_protect(root)
    n_prot = 0
    for s, _d, _sz in pairs:
        ap_ = os.path.join(root, s)
        if os.path.isfile(ap_) and protect_key(s, os.path.getsize(ap_)) in prot:
            n_prot += 1
    if n_prot:
        print(f"    [知悉] 映射含 {n_prot} 个人工保护清单文件（整体重排属正常落位）")

    print(f"[预检] {len(pairs)} 条映射")
    for s, planned, now in drifts:
        print(f"    [漂移] {s[:66]} 计划时{planned}B → 现在{now}B（仍按已批准映射落位，内容以现文件为准）")
    for w in warns:
        print("    [注意]", w)
    if hard:
        for e in hard:
            print("    [错误]", e)
        raise SystemExit("[中止] 存在不可绕过的预检错误（--allow-shared-dst 也不能放行）。")
    if shared:
        for e in shared:
            print("    [风险]", e)
        if not args.allow_shared_dst:
            raise SystemExit("[中止] 存在共享目标风险。确认为多版本件共存时加 --allow-shared-dst 放行（仅放行此项）。")

    import json, shutil, datetime
    moved = renamed = 0
    if args.execute:
        n_before = sum(len(fs) for r_, d_, fs in os.walk(root) if "归档整理" not in r_.split(os.sep))
        batch = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        was = ensure_unlocked(root)
        try:
            src_dirs = set()  # 本批实际移动的源目录：清理只沿这些父链，不扫全树
            with open(journal_path, "a", encoding="utf-8") as jf:
                for i, (s, d, _sz) in enumerate(pairs, 1):
                    s_abs, d_abs = os.path.join(root, s), os.path.join(root, d)
                    if not os.path.exists(s_abs):
                        print(f"  [跳过] 源已消失: {s}")
                        continue
                    final = d_abs
                    if os.path.exists(d_abs):
                        base, ext = os.path.splitext(d_abs)
                        k = 1
                        while os.path.exists(f"{base} ({k}){ext}"):
                            k += 1
                        final = f"{base} ({k}){ext}"
                        renamed += 1
                    os.makedirs(os.path.dirname(final), exist_ok=True)
                    shutil.move(s_abs, final)
                    src_dirs.add(os.path.dirname(s_abs))
                    jf.write(json.dumps({
                        "time": datetime.datetime.now().isoformat(timespec="seconds"),
                        "batch": batch,
                        "src": s, "dst": os.path.relpath(final, root)}, ensure_ascii=False) + "\n")
                    jf.flush()
                    moved += 1
            # 只沿本批移动源的父链自底向上清空壳；预存空目录（链外或链上非空）不受影响
            pruned = 0
            for d0 in sorted(src_dirs, key=len, reverse=True):
                pruned += prune_empty_chain(d0, root)
            do_snapshot(root)  # 与 ao_classify 同：执行成功即刷新基线（须在 relock 前）
        finally:
            relock(root, was)
        n_after = sum(len(fs) for r_, d_, fs in os.walk(root) if "归档整理" not in r_.split(os.sep))
        verdict = "守恒 ✓" if n_before == n_after else "有增减，请核对！"
        print(f"✓ 批次 {batch}：移动 {moved} 个（跳过源已消失 {len(pairs) - moved}，序号共存 {renamed}，清理空目录 {pruned}）。")
        print(f"  终验：文件总数 执行前 {n_before} → 执行后 {n_after}（{verdict}）")
        print(f"  日志: {journal_path}（ao_classify --root {root} --undo-last 只回滚本批次；--undo 回滚全部）")
    else:
        print("→ 干跑预览通过。加 --execute 执行。")


if __name__ == "__main__":
    main()
