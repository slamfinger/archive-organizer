# -*- coding: utf-8 -*-
"""位置快照与增量检测：基线之外的变化即外部（人工）变更。

snapshot  记录当前全树 <相对路径: 大小> 基线。ao_classify --execute 成功后
          会自动刷新基线，因此两次整理之间的任何差异都是人工操作。
diff      对比基线并报告三类变化：
          移位/改名 → 人工调整；新增 → 待归档增量；消失 → 外部删除
  --apply-protection  把「移位/改名」写入人工保护清单（.人工保护.json），
          此后 ao_classify / ao_verify 对这些文件（按 文件名+大小 识别）
          一律跳过——人工调整永不被规则回改。

典型增量工作流：
  unlock → diff --apply-protection（人工调整受保护，新增列清单）
        → 对新增文件拟增量规则 → verify → 干跑 → 确认 → execute
        → （execute 自动刷新基线）→ lock
"""
import os
import json
import argparse
import collections

from ao_common import Excludes, walk_rel, workdir, WORKDIR_NAME
from ao_protect import ensure_unlocked, relock

SNAP = ".位置快照.json"
PROTECT = ".人工保护.json"


def snap_path(root):
    return os.path.join(workdir(root), SNAP)


def protect_path(root):
    return os.path.join(workdir(root), PROTECT)


def _snap(root):
    ex = Excludes()
    ex.names.add(WORKDIR_NAME)
    out = {}
    for kind, rel, a in walk_rel(root, ex):
        if kind == "f":
            out[rel] = os.path.getsize(a)
    return out


def do_snapshot(root):
    s = _snap(root)
    with open(snap_path(root), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, sort_keys=True)
    print(f"✓ 位置基线已记录: {len(s)} 个文件")


def load_protect(root):
    p = protect_path(root)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_protect(root, prot):
    with open(protect_path(root), "w", encoding="utf-8") as f:
        json.dump(sorted(prot), f, ensure_ascii=False, indent=1)


def protect_key(rel, size):
    return f"{os.path.basename(rel)}\t{size}"


def do_diff(root, apply_protection):
    p = snap_path(root)
    if not os.path.exists(p):
        raise SystemExit("[提示] 尚无位置基线，先执行 snapshot")
    with open(p, encoding="utf-8") as f:
        base = json.load(f)
    now = _snap(root)
    base_keys = collections.defaultdict(list)
    for r, s in base.items():
        base_keys[(os.path.basename(r), s)].append(r)

    moved, new = [], []
    for r, s in now.items():
        if r in base:
            continue
        if base_keys.get((os.path.basename(r), s)):
            moved.append(r)
        else:
            new.append(r)
    gone = [r for r in base if r not in now]

    print(f"移位/改名（人工调整）{len(moved)} | 新增 {len(new)} | 消失 {len(gone)}")
    for tag, items in (("[移]", moved), ("[新]", new), ("[失]", gone)):
        for r in sorted(items):
            print(f"  {tag} {r[:110]}")
    if not (moved or new or gone):
        print("✓ 与基线一致，无外部变更")
    if apply_protection and moved:
        prot = load_protect(root)
        before = len(prot)
        for r in moved:
            prot.add(protect_key(r, now[r]))
        save_protect(root, prot)
        print(f"✓ 人工保护清单已更新：新增 {len(prot) - before} 项，共 {len(prot)} 项")
        print("→ 后续规则移动将自动跳过这些文件")


def main():
    ap = argparse.ArgumentParser(description="位置快照与增量/人工调整检测")
    ap.add_argument("--root", required=True, help="归档根目录")
    ap.add_argument("action", choices=["snapshot", "diff"])
    ap.add_argument("--apply-protection", action="store_true",
                    help="diff 时把移位/改名写入人工保护清单")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    was = ensure_unlocked(root)
    try:
        do_snapshot(root) if args.action == "snapshot" else do_diff(root, args.apply_protection)
    finally:
        relock(root, was)


if __name__ == "__main__":
    main()
