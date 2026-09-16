# -*- coding: utf-8 -*-
"""规则体检：不移动任何文件，静态核验规则CSV与目录现状的相容性。

检查全部由「规则 + 目录现状」推导，不含任何特定项目的词：
  移文件        零命中[错误] / 全部已在目标[知悉,规则已完成] / 跨域命中[警告] /
                关键词过短或命中过多[警告] / 跨域同名疑似镜像副本[警告] /
                同名多份加(n)[知悉] / 规则重叠顺序裁决[知悉] /
                捞走后续目录规则源树文件并压平[知悉,调整规则顺序可避免]
  并目录/移目录  源不存在[错误] / 目标位于源内部[错误] / 收口重名加(n)[知悉]

规则顺序语义（classify 先展开全部规则、再按CSV顺序执行，先到先得）：
  移文件规则排在目录规则之前 → 会把源树内命中文件「压平」捞进自己的目标；
  目录规则排在前面 → 文件随目录整体走、保留内部结构。体检按此语义报告。

用法:
  python3 ao_verify.py --root /path/to/项目 [--rules rules.csv]
退出码: 存在[错误] → 1；否则 0（[警告]需人工判断是否有意）。
"""
import os
import argparse
import collections

from ao_common import (Excludes, walk_rel, ensure_inside, read_csv_rows,
                       workdir, WORKDIR_NAME)
from ao_classify import load_rules
from ao_state import load_protect, protect_key

ERR, WARN, INFO = "错误", "警告", "知悉"
BROAD_HITS = 50  # 命中超过此数视为宽泛词


def top_of(rel):
    parts = rel.split(os.sep)
    return parts[0] if len(parts) > 1 else "(根层级)"


def under(abs_path, dir_abs):
    try:
        return os.path.commonpath([abs_path, dir_abs]) == dir_abs
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser(description="静态核验规则CSV（只读，不移动）")
    ap.add_argument("--root", required=True, help="归档根目录")
    ap.add_argument("--rules", default=None, help="规则CSV（默认 <root>/归档整理/归档规则.csv）")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    if args.rules:
        rules_path = args.rules
    else:
        rules_path = os.path.join(workdir(root), "归档规则.csv")
    if not os.path.exists(rules_path):
        raise SystemExit(f"[错误] 规则表不存在: {rules_path}")
    rules = load_rules(rules_path)

    ex = Excludes()
    ex.names.add(WORKDIR_NAME)
    files = [(rel, abs_) for kind, rel, abs_ in walk_rel(root, ex) if kind == "f"]

    dir_rules = [(i, src) for i, (a, src, _d) in enumerate(rules) if a in ("并目录", "移目录")]
    prot = load_protect(root)
    matched_by = collections.defaultdict(list)  # 文件rel -> 命中它的移文件规则号(升序)
    total_moves = 0
    per_rule = {}       # 规则号 -> [预计移动数, 已在目标数]
    findings = []       # (规则号, 级别, 文本)

    for i, (action, src, dst) in enumerate(rules):
        no = i + 1
        if action != "移文件":
            src_abs = os.path.join(root, src)
            dst_abs = os.path.join(root, dst)
            if not os.path.isdir(src_abs):
                findings.append((no, ERR, f"源目录不存在: {src}"))
                continue
            try:
                ensure_inside(root, src_abs)
                ensure_inside(root, dst_abs)
            except SystemExit as e:
                findings.append((no, ERR, str(e)))
                continue
            if src_abs == dst_abs or under(dst_abs, src_abs):
                findings.append((no, ERR, f"目标等于/位于源内部: {dst}"))
                continue
            total_moves += sum(1 for kind, rel, a in walk_rel(src_abs, ex)
                               if kind == "f"
                               and protect_key(os.path.basename(rel), os.path.getsize(a)) not in prot)
            if action == "并目录" and os.path.isdir(dst_abs) and not under(dst_abs, src_abs):
                existing = set(os.listdir(dst_abs))
                coll = sum(1 for kind, rel, _a in walk_rel(src_abs, ex)
                           if kind == "f" and os.path.basename(rel) in existing)
                if coll:
                    findings.append((no, INFO,
                        f"收口重名: 源树 {coll} 个文件与目标现有文件同名，将加 (n) 序号"))
            continue

        dst_abs = os.path.join(root, dst)
        try:
            ensure_inside(root, dst_abs)
        except SystemExit as e:
            findings.append((no, ERR, str(e)))
            continue
        hits = [(rel, abs_) for rel, abs_ in files if src in os.path.basename(rel)]
        in_target = [(rel, abs_) for rel, abs_ in hits
                     if os.path.dirname(abs_) == dst_abs]
        eff = [h for h in hits if h not in in_target]
        prot_hits = [(rel, abs_) for rel, abs_ in eff
                     if protect_key(os.path.basename(rel), os.path.getsize(abs_)) in prot]
        eff = [h for h in eff if h not in set(prot_hits)]
        for rel, _a in eff:
            matched_by[rel].append(no)
        total_moves += len(eff)
        per_rule[no] = [len(eff), len(in_target)]
        if prot_hits:
            findings.append((no, INFO,
                f"人工保护跳过 {len(prot_hits)} 个文件（人工调整不回改）"))

        if not hits:
            findings.append((no, ERR, f"零命中: 根内没有文件名含「{src}」（关键词过时或过窄）"))
            continue
        if not eff:
            findings.append((no, INFO, f"规则已完成: {len(hits)} 个命中文件全部已在目标目录"))
            continue
        doms = collections.Counter(top_of(rel) for rel, _a in eff)
        if len(doms) > 1:
            detail = " ".join(f"{d}({n})" for d, n in sorted(doms.items()))
            findings.append((no, WARN, f"跨域命中: {detail} —— 若非有意归拢，改用更精确关键词"))
        if len(src) < 3:
            findings.append((no, WARN, f"关键词过短「{src}」，易误捞"))
        if len(eff) > BROAD_HITS:
            findings.append((no, WARN, f"命中过多（{len(eff)} 个），疑似宽泛词"))
        by_name = collections.Counter(os.path.basename(rel) for rel, _a in eff)
        dups = {n: c for n, c in by_name.items() if c > 1}
        if dups:
            sample = "、".join(list(dups)[:3])
            dup_total = sum(dups.values())
            dup_doms = sorted({top_of(rel) for rel, _a in eff
                               if os.path.basename(rel) in dups})
            if len(dup_doms) > 1:
                findings.append((no, WARN,
                    f"跨域同名疑似镜像副本: 「{sample}」等 {dup_total} 个文件将并入同一目标"
                    f"（加 (n) 序号）—— 先确认以哪份为准，勿对镜像对写规则"))
            else:
                findings.append((no, INFO,
                    f"同名多份: {dup_total} 个文件同名，并入目标将加 (n) 序号"))
        for j, jsrc in dir_rules:
            if j <= i:
                continue  # 目录规则在前 → 文件随目录整体走；只报告「移文件在前」的压平捞取
            jsrc_abs = os.path.join(root, jsrc)
            if not os.path.isdir(jsrc_abs):
                continue
            inside = [rel for rel, abs_ in eff if under(abs_, jsrc_abs)]
            if inside:
                jact, jdst = rules[j][0], rules[j][2]
                findings.append((no, INFO,
                    f"会先捞出规则{j + 1}（{jact} → {jdst}）源树内 {len(inside)} 个文件并压平"
                    f"——若要保留其内部结构，把该目录规则排到本规则之前"))

    # 规则重叠：同一文件被多条移文件规则命中，靠顺序裁决（后面的规则少移）
    overlap_later = collections.Counter(nos[1] for nos in matched_by.values() if len(nos) > 1)
    overlap_first = collections.Counter(nos[0] for nos in matched_by.values() if len(nos) > 1)
    for no, k in sorted(overlap_later.items()):
        findings.append((no, INFO,
            f"规则重叠: {k} 个文件也被更早的规则命中，将由规则顺序先到先得（本规则少移 {k} 个）"))

    errs = [f for f in findings if f[1] == ERR]
    warns = [f for f in findings if f[1] == WARN]
    infos = [f for f in findings if f[1] == INFO]

    print(f"规则 {len(rules)} 条；预计移动 {total_moves} 个文件（应与干跑计数一致）")
    if findings:
        print(f"发现 {len(errs)} 错误 / {len(warns)} 警告 / {len(infos)} 知悉，其余规则无发现：")
        by_no = collections.defaultdict(list)
        for no, sev, text in findings:
            by_no[no].append((sev, text))
        for no in sorted(by_no):
            action, src, dst = rules[no - 1]
            print(f"  规则{no} {action}: {src} → {dst}")
            for sev, text in by_no[no]:
                print(f"      [{sev}] {text}")
    else:
        print("✓ 全部规则无发现")
    raise SystemExit(1 if errs else 0)


if __name__ == "__main__":
    main()
