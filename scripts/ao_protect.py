# -*- coding: utf-8 -*-
"""归档整理/ 目录的隐藏与锁定（macOS / Windows）。

lock     隐藏工作目录并锁定全部内容：macOS 用 chflags hidden + uchg（递归，
         阻止增删改）；Windows 用 attrib +h +r（+h 隐藏，+r 建议性只读）。
         加锁前先把状态写入 <workdir>/.ao_lock.json（含全部受锁条目清单），
         解锁时按清单逐一恢复并移除该文件。
unlock   解除锁定并恢复显示。
status   查看当前状态。

其余脚本（scan/classify/state）写入产物前调用 ensure_unlocked() 自动解锁、
结束后 relock() 恢复——「平时上锁、整理时临时开锁」对使用者透明。
Linux 无隐藏/锁定等价物，脚本可运行但不加标志（状态里注明）。

用法:
  python3 ao_protect.py --root /path/to/项目 lock|unlock|status
"""
import os
import sys
import json
import argparse
import subprocess

from ao_common import workdir

MARKER = ".ao_lock.json"


def _is_win():
    return os.name == "nt"


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        print(f"[警告] 命令不可用: {e.filename}（该平台保护能力受限）")
        return None


def marker_path(root):
    return os.path.join(workdir(root), MARKER)


def lock_state(root):
    """返回锁定状态 dict；未锁定返回 None。"""
    p = marker_path(root)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _collect_paths(wd):
    """工作目录内全部条目（含目录自身与隐藏文件）。"""
    out = [wd]
    for dirpath, dirnames, filenames in os.walk(wd):
        for dn in dirnames:
            out.append(os.path.join(dirpath, dn))
        for fn in filenames:
            out.append(os.path.join(dirpath, fn))
    return out


def _apply(root, entries, lock, hide):
    """按平台施加/解除标志。entries: 相对 workdir 的路径列表。"""
    wd = workdir(root)
    plat = "win" if _is_win() else ("mac" if sys.platform == "darwin" else "other")
    if plat == "other":
        print("[提示] 当前平台无隐藏/锁定机制，跳过标志设置")
        return
    for r in entries:
        p = os.path.join(wd, r)
        if not os.path.exists(p):
            continue
        if _is_win():
            _run(["attrib", "+r" if lock else "-r", p])
        else:
            _run(["chflags", "uchg" if lock else "nouchg", p])
    root_p = os.path.join(wd, ".")
    if _is_win():
        _run(["attrib", "+h" if hide else "-h", wd])
    elif plat == "mac":
        _run(["chflags", "hidden" if hide else "nohidden", wd])


def lock(root):
    wd = workdir(root)
    st = lock_state(root)
    if st and st.get("locked"):
        print("[提示] 已处于锁定状态")
        return
    entries = [os.path.relpath(p, wd) for p in _collect_paths(wd)]
    with open(marker_path(root), "w", encoding="utf-8") as f:
        json.dump({"locked": True, "hidden": True, "entries": entries},
                  f, ensure_ascii=False, indent=1)
    _apply(root, entries, lock=True, hide=True)
    print(f"✓ 已隐藏并锁定: {wd}（{len(entries)} 个条目）→ 解锁: ao_protect.py --root ... unlock")


def unlock(root):
    st = lock_state(root)
    if not st:
        print("[提示] 未锁定")
        return
    wd = workdir(root)
    _apply(root, st.get("entries", []), lock=False, hide=False)
    try:
        os.remove(marker_path(root))
    except OSError as e:
        print(f"[警告] 状态文件未能移除: {e}")
    print("✓ 已解锁并恢复显示")


def status(root):
    st = lock_state(root)
    if not st:
        print("状态: 未锁定（归档整理/ 可见可改）")
    else:
        print(f"状态: 已锁定并隐藏（{len(st.get('entries', []))} 个条目受保护）")


def ensure_unlocked(root):
    """若已锁定则解锁；返回是否发生过解锁（供 relock 恢复）。"""
    if lock_state(root):
        unlock(root)
        return True
    return False


def relock(root, was_locked):
    if was_locked:
        lock(root)


def main():
    ap = argparse.ArgumentParser(description="归档整理目录的隐藏与锁定")
    ap.add_argument("--root", required=True, help="归档根目录")
    ap.add_argument("action", choices=["lock", "unlock", "status"])
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    {"lock": lock, "unlock": unlock, "status": status}[args.action](root)


if __name__ == "__main__":
    main()
