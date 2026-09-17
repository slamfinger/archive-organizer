# -*- coding: utf-8 -*-
"""逐文件映射总表回放器：把「映射总表.csv」（一行一文件：现相对路径,新相对路径）
批量落位。骨架重排、跨机对齐、迁移回放共用这一机制。

与 ao_classify.py 同一套安全纪律：
- 默认干跑，--execute 才真移动；
- 日志与 ao_classify 兼容（jsonl: time/src/dst 相对路径），ao_classify --undo 可整体回滚；
- 目标重名自动加 " (n)" 序号，绝不覆盖；
- 移动后自底向上清理搬空的目录壳（工作目录除外，非空即停）。

预检守卫（血泪教训固化为规则）：
- 源必须存在、不得重复、不得等于目标；
- 同一目标字符串被 ≥3 行共享 → 判定为「目录槽当文件目标」事故，拒绝执行
  （散件分派必须拼上文件名；≤2 行共享视为版本件共存，放行并提示）。"""
import os
import argparse
from collections import Counter

from ao_common import ensure_inside, workdir, read_csv_rows, WORKDIR_NAME
from ao_protect import ensure_unlocked, relock
from ao_state import do_snapshot, load_protect, protect_key

JUNK_PREFIX = ("~$", "._")
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def is_junk(name):
    return name in JUNK_NAMES or name.startswith(JUNK_PREFIX)


def load_pairs(path):
    rows = read_csv_rows(path)
    if len(rows) < 2:
        raise SystemExit(f"[错误] 映射表为空: {path}")
    return [(r[0].strip(), r[1].strip()) for r in rows[1:] if len(r) >= 2 and r[0].strip()]


def preflight(pairs, root):
    errs, warns = [], []
    src_seen, dst_cnt = {}, Counter()
    for s, d in pairs:
        if s in src_seen:
            errs.append(f"源重复: {s}")
        src_seen[s] = d
        if is_junk(os.path.basename(s)):
            errs.append(f"垃圾文件不应进映射表: {s}")
        if s == d:
            errs.append(f"源等于目标: {s}")
        for p in (s, d):
            ap = os.path.abspath(os.path.join(root, p))
            if not ap.startswith(os.path.abspath(root) + os.sep):
                errs.append(f"越界路径: {p}")
        if not os.path.isfile(os.path.join(root, s)):
            errs.append(f"源不存在: {s}")
        dst_cnt[d] += 1
    for d, n in dst_cnt.items():
        if n >= 3:
            errs.append(f"目标被{n}行共享（疑似目录槽当文件目标）: {d}")
        elif n == 2:
            warns.append(f"同目标2行（版本件将序号共存）: {d}")
    return errs, warns


def main():
    ap = argparse.ArgumentParser(description="逐文件映射总表回放（默认干跑）")
    ap.add_argument("--root", required=True)
    ap.add_argument("--map", required=True, help="映射总表.csv（现相对路径,新相对路径）")
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
    errs, warns = preflight(pairs, root)

    # 人工保护衔接：映射按「文件名+大小」对齐保护清单，命中即知悉提示。
    # 映射是整体已审方案（remap 从现树生成，人工调整后的现位置即映射源），
    # 故不阻断，但要让人看见；增量规则请勿回改这些文件。
    prot = load_protect(root)
    n_prot = 0
    for s, _d in pairs:
        ap = os.path.join(root, s)
        if os.path.isfile(ap) and protect_key(s, os.path.getsize(ap)) in prot:
            n_prot += 1
    if n_prot:
        print(f"    [知悉] 映射含 {n_prot} 个人工保护清单文件（整体重排属正常落位）")

    print(f"[预检] {len(pairs)} 条映射")
    for w in warns:
        print("    [注意]", w)
    for e in errs:
        print("    [错误]", e)
    if errs and not args.allow_shared_dst:
        raise SystemExit("[中止] 预检未通过，未移动任何文件。")

    import json, shutil, datetime
    moved = renamed = 0
    if args.execute:
        was = ensure_unlocked(root)
        try:
            with open(journal_path, "a", encoding="utf-8") as jf:
                for i, (s, d) in enumerate(pairs, 1):
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
                    jf.write(json.dumps({
                        "time": datetime.datetime.now().isoformat(timespec="seconds"),
                        "src": s, "dst": os.path.relpath(final, root)}, ensure_ascii=False) + "\n")
                    jf.flush()
                    moved += 1
                    if i % 300 == 0:
                        print(f"  … {i}/{len(pairs)}")
            pruned = 0
            for dirpath, dnames, _ in os.walk(root, topdown=False):
                if os.path.abspath(dirpath) == root or WORKDIR_NAME in dirpath.split(os.sep):
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        pruned += 1
                except OSError:
                    pass
            do_snapshot(root)  # 与 ao_classify 同：执行成功即刷新基线（须在 relock 前）
        finally:
            relock(root, was)
        print(f"✓ 移动 {moved} 个，序号共存 {renamed} 个，清理空目录 {pruned} 个。")
        print(f"  日志: {journal_path}（ao_classify --root {root} --undo 可整体回滚）")
    else:
        print("→ 干跑预览通过。加 --execute 执行。")


if __name__ == "__main__":
    main()
