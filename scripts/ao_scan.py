# -*- coding: utf-8 -*-
"""全量扫描归档根 → 生成清单（只读不写，不改任何结构）。

与旧版 scan_inventory.py 的区别：
- 无硬编码路径：归档根由 --root 指定
- 清单记录相对路径（可移植，不再绑死某台机器/某个绝对路径）
- [F] 行以制表符附带文件大小，供后续环节免二次 stat
- 默认跳过隐藏目录、.DS_Store、._* 、~$* 等垃圾文件（--exclude 可追加）

用法:
  python3 ao_scan.py --root /path/to/项目 --out _inventory.txt
"""
import os
import sys
import argparse

from ao_common import Excludes, walk_rel, workdir, WORKDIR_NAME
from ao_protect import ensure_unlocked, relock


def main():
    ap = argparse.ArgumentParser(description="全量扫描归档根，生成相对路径清单（只读）")
    ap.add_argument("--root", required=True, help="归档根目录（一级域=其下各子目录）")
    ap.add_argument("--out", default=None,
                    help="清单输出路径（默认 <root>/归档整理/清单.txt）")
    ap.add_argument("--exclude", action="append", default=[],
                    help="追加忽略的名称或通配模式（可多次）")
    ap.add_argument("--no-default-excludes", action="store_true",
                    help="关闭内置忽略规则（隐藏目录仍会跳过）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    ex = Excludes(args.exclude, use_defaults=not args.no_default_excludes)
    out_path = args.out or os.path.join(workdir(root), "清单.txt")
    ex.names.add(WORKDIR_NAME)  # 产物目录不进清单

    # 一级域 = 根下每个子目录；根下散落文件记入「(根目录)」伪域，保证不漏
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        raise SystemExit(f"[错误] 无法读取归档根: {e}")
    domains = [d for d in entries if os.path.isdir(os.path.join(root, d)) and ex.dir_ok(d)]
    loose = [f for f in entries
             if os.path.isfile(os.path.join(root, f)) and ex.file_ok(f)]

    total_files = 0
    with open(out_path, "w", encoding="utf-8") as out:
        if loose:
            out.write("【一级域: (根目录)】\n")
            for f in loose:
                size = os.path.getsize(os.path.join(root, f))
                out.write(f"[F] {f}\t{size}\n")
                total_files += 1

        for dom in domains:
            n_f = n_d = 0
            max_depth = 1
            out.write(f"【一级域: {dom}】\n")
            for kind, rel, abs_ in walk_rel(os.path.join(root, dom), ex):
                depth = rel.count(os.sep) + 1
                max_depth = max(max_depth, depth)
                if kind == "d":
                    out.write(f"[D] {os.path.join(dom, rel)}\n")
                    n_d += 1
                else:
                    out.write(f"[F] {os.path.join(dom, rel)}\t{os.path.getsize(abs_)}\n")
                    n_f += 1
            total_files += n_f
            print(f"  {dom}: 文件={n_f} 子目录={n_d} 最大深度={max_depth}")

    print(f"✓ 发现 {len(domains)} 个一级域，共 {total_files} 个文件")
    print(f"✓ 清单已写入: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    _root = None
    if "--root" in sys.argv:
        _i = sys.argv.index("--root")
        if _i + 1 < len(sys.argv):
            _root = os.path.abspath(sys.argv[_i + 1])
    _was = ensure_unlocked(_root) if _root and os.path.isdir(_root) else False
    try:
        main()
    finally:
        relock(_root, _was)
