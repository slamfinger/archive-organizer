# -*- coding: utf-8 -*-
"""从清单生成「归档骨架评审 CSV」——给人审的迁移计划，本脚本不移动任何文件。

与旧版 build_archive_schema.py 的区别：
- 无硬编码路径：清单与输出都走参数
- CSV 用标准库 csv 模块写出（旧版手工 join 会损坏含逗号/引号的字段）
- 域级「建议方案」附带中文关键词聚类提示（词频思想），帮人快速看出该按什么主题收口
- 全部阈值参数化：--max-calm / --busy / --top

CSV 六列（沿用既有约定，人工在「是否调整」列勾选）:
  一级目录 | 第二级业务类型（推荐） | 第三级通用文件归档标准 | 是否调整 | 建议方案 | 退回现状

用法:
  python3 ao_schema.py _inventory.txt --out archive_schema_draft.csv
  python3 ao_schema.py _inventory.txt --max-calm 12 --busy 30 --top 15
"""
import os
import re
import argparse
from collections import Counter

from ao_common import read_csv_rows, write_csv_rows, cluster_hints

HEADER = ["一级目录", "第二级业务类型（推荐）", "第三级通用文件归档标准",
          "是否调整", "建议方案", "退回现状"]


def load_inventory(path):
    """解析清单：{一级域: {"二级名": {"files": n, "dirs": n}}}，二级名 "" 表示域根散落文件。
    关键：文件归属哪个二级，看它路径的第二段是否是真实目录（用 [D] 行核对），
    否则视为域根散文件——旧版把散文件文件名当二级名，聚类提示被扩展名词污染。
    [D] 行总是先于其下 [F] 行出现（扫描器先序遍历保证），单遍即可核对。"""
    domains = {}    # 域 -> {二级名: 统计}
    dircnt = {}     # 域 -> Counter{二级名: 子目录数}
    cur = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"【一级域:\s*(.+?)】", line)
            if m:
                cur = m.group(1).strip()
                domains.setdefault(cur, {})
                dircnt.setdefault(cur, Counter())
                continue
            if not cur or not (line.startswith("[D] ") or line.startswith("[F] ")):
                continue
            rel = line[4:].split("\t")[0].strip()
            parts = rel.split("/")
            if line.startswith("[D] "):
                if len(parts) >= 2:
                    dircnt[cur][parts[1]] += 1
                continue
            # [F]：第二段是真实目录才计入该二级，否则归入域根散落文件
            bucket = parts[1] if len(parts) >= 2 and parts[1] in dircnt[cur] else ""
            domains[cur].setdefault(bucket, {"files": 0, "dirs": 0})["files"] += 1
    for dom, cnt in dircnt.items():
        for sec, n in cnt.items():
            domains[dom].setdefault(sec, {"files": 0, "dirs": 0})["dirs"] = n
    return domains


def build(domains, max_calm, busy, top):
    rows = [HEADER]
    for dom in sorted(domains):
        secs = domains[dom]
        loose_files = secs.get("", {})  # 直接落在域根的散文件（二级段缺省）
        n_secs = len([s for s in secs if s])
        hints = cluster_hints([s for s in secs if s], min_members=2)

        if n_secs <= max_calm:
            rows.append([dom, "现有结构已合理", "", "否", dom, "保持不变"])
            continue

        hint_txt = "；聚类提示: " + "、".join(f"{w}({n}个)" for w, n in hints) if hints else ""
        rows.append([dom, "按业务主题合并收口", "", "是",
                     f"{n_secs}个二级→按主题归并为≥2组，标签区分流向/年份{hint_txt}", "保留"])

        ranked = sorted(((s, v) for s, v in secs.items() if s),
                        key=lambda x: -x[1]["files"])
        shown = 0
        for name, info in ranked:
            if info["files"] < busy and info["dirs"] == 0:
                continue  # 冷清二级不值得动
            if shown >= top:
                rows.append([dom, f"…（其余{n_secs - shown}个二级略，见清单）", "", "-", "", ""])
                break
            rows.append([dom, name, "", "是",
                         "同类文件原地归并或标签压平（不增层）", "保留该二级"])
            shown += 1
        if loose_files.get("files"):
            rows.append([dom, "(域根散落文件)", f"{loose_files['files']}个", "是",
                         "按关键词并入下方对应二级", "原地保留"])
    return rows


def main():
    ap = argparse.ArgumentParser(description="从清单生成归档骨架评审CSV（不移动文件）")
    ap.add_argument("inventory", nargs="?", default=None,
                    help="ao_scan.py 生成的清单文件（缺省取 <root>/归档整理/清单.txt，"
                         "需配合 --root 或在归档根内运行）")
    ap.add_argument("--root", default=None, help="归档根目录（用于推导默认清单/输出位置）")
    ap.add_argument("--out", default=None,
                    help="评审CSV输出路径（默认与清单同目录: 骨架评审.csv）")
    ap.add_argument("--max-calm", type=int, default=12,
                    help="二级≤该数的域视为结构已合理，出锚定行（默认12）")
    ap.add_argument("--busy", type=int, default=30,
                    help="文件数≥该数的二级建议压平（默认30；含子目录的二级一律列出）")
    ap.add_argument("--top", type=int, default=15, help="每个域最多列多少个二级明细行（默认15）")
    args = ap.parse_args()

    if args.inventory:
        inv = args.inventory
    elif args.root:
        from ao_common import WORKDIR_NAME
        inv = os.path.join(os.path.abspath(args.root), WORKDIR_NAME, "清单.txt")
    else:
        raise SystemExit("[错误] 需提供清单路径，或用 --root 推导默认位置")
    if not os.path.exists(inv):
        raise SystemExit(f"[错误] 清单不存在: {inv}（先跑 ao_scan.py）")

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(inv)), "骨架评审.csv")
    domains = load_inventory(inv)
    rows = build(domains, args.max_calm, args.busy, args.top)
    write_csv_rows(out, rows)

    n_dom = len(domains)
    n_intv = sum(1 for r in rows[1:] if r[3] == "是")
    print(f"✓ 发现 {n_dom} 个一级域；评审行 {len(rows) - 1} 条（其中待调整 {n_intv} 条）")
    print(f"✓ 评审CSV已写入: {os.path.abspath(out)}")
    print("→ 下一步：人工逐行勾「是否调整」，再写成归档规则.csv 用 ao_classify.py 执行")


if __name__ == "__main__":
    main()
