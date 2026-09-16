# -*- coding: utf-8 -*-
"""archive-organizer 共用库：忽略规则、目录遍历、安全移动、可回滚日志、中文关键词聚类。

设计借鉴的开源做法（见 references/method.md）：
- organize (ThomasWaldmann)   : 默认干跑、执行时写 journal、支持回滚
- rsync / fdupes              : exclude 模式过滤垃圾文件
- Finder / rclone             : 目标重名自动追加 " (n)" 序号，绝不覆盖
"""
import os
import re
import sys
import csv
import json
import shutil
import fnmatch
import datetime

# ---------- 默认忽略规则（--exclude 追加 / --no-default-excludes 关闭） ----------
DEFAULT_IGNORE_NAMES = [".DS_Store", "Thumbs.db", "desktop.ini"]
DEFAULT_IGNORE_PATTERNS = ["._*", "~$*"]           # macOS AppleDouble / Office 锁文件
DEFAULT_DIR_IGNORES = ["node_modules", "__pycache__", ".git"]

# 工作目录名：所有产物（清单/评审CSV/规则/日志）默认落在 <root>/归档整理/ 下，
# 扫描与移动遍历时自动跳过，避免把整理产物当档案。
WORKDIR_NAME = "归档整理"


def workdir(root):
    """归档根内的工作目录：<root>/归档整理/。自动创建。"""
    path = os.path.join(os.path.abspath(root), WORKDIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def with_workdir_excluded(extra=None):
    """把工作目录追加进 --exclude 列表，供各脚本扫描时排除自身产物。"""
    return (list(extra) if extra else []) + [WORKDIR_NAME]


class Excludes:
    """文件/目录过滤器：隐藏目录一律跳过；精确名 + 通配模式两种匹配。"""

    def __init__(self, extra=None, use_defaults=True):
        self.names = set()
        self.patterns = []
        if use_defaults:
            self.names.update(DEFAULT_IGNORE_NAMES, DEFAULT_DIR_IGNORES)
            self.patterns.extend(DEFAULT_IGNORE_PATTERNS)
        for e in extra or []:
            if any(ch in e for ch in "*?["):
                self.patterns.append(e)
            else:
                self.names.add(e)

    def file_ok(self, fn):
        return fn not in self.names and not any(fnmatch.fnmatch(fn, p) for p in self.patterns)

    def dir_ok(self, dn):
        if dn.startswith("."):
            return False  # 隐藏目录（.git/.Trash 等）一律不进清单
        return dn not in self.names and not any(fnmatch.fnmatch(dn, p) for p in self.patterns)


# ---------- 遍历 ----------
def walk_rel(root, ex):
    """确定性遍历，产出 (kind, relpath, abspath)。kind: 'd'|'f'。相对路径均相对 root。
    不跟随目录符号链接，避免环；读不了的目录降级为警告而非崩溃。"""
    root = os.path.abspath(root)

    def rec(rel):
        ap = os.path.join(root, rel) if rel else root
        try:
            entries = sorted(os.listdir(ap))
        except OSError as e:
            print(f"[警告] 无法读取 {rel or '.'}: {e}", file=sys.stderr)
            return
        for name in entries:
            r = os.path.join(rel, name) if rel else name
            a = os.path.join(root, r)
            if os.path.isdir(a) and not os.path.islink(a):
                if ex.dir_ok(name):
                    yield ("d", r, a)
                    yield from rec(r)
            elif ex.file_ok(name):
                yield ("f", r, a)

    yield from rec("")


# ---------- 路径安全 ----------
def ensure_inside(root, target):
    """目标路径必须落在归档根内，拒绝 ../ 越界。"""
    root_a, target_a = os.path.abspath(root), os.path.abspath(target)
    try:
        if os.path.commonpath([root_a, target_a]) != root_a:
            raise SystemExit(f"[拒绝] 路径越出归档根 {root_a}: {target_a}")
    except ValueError:  # Windows 跨盘等场景
        raise SystemExit(f"[拒绝] 路径越出归档根 {root_a}: {target_a}")


def unique_dst(dst):
    """目标已存在时追加 " (n)" 序号（rclone/Finder 风格），绝不静默覆盖。"""
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def move_file(src_abs, dst_abs):
    """移动单文件：自动建父目录、重名加序号；shutil.move 跨盘自动回退复制+删除。
    返回最终落点绝对路径。"""
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
    final = unique_dst(dst_abs)
    shutil.move(src_abs, final)
    return final


# ---------- 移动日志（journal / undo，借鉴 organize 工具） ----------
class Journal:
    def __init__(self, path, enabled=True):
        self.path = path
        self.fh = open(path, "a", encoding="utf-8") if enabled else None

    def log(self, src, dst):
        if not self.fh:
            return
        self.fh.write(json.dumps({
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "src": src, "dst": dst}, ensure_ascii=False) + "\n")
        self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()

    @classmethod
    def entries(cls, path):
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


# ---------- CSV（统一 utf-8-sig，Excel 直接可开） ----------
def read_csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.reader(f) if any(c.strip() for c in r)]


def write_csv_rows(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)


# ---------- 中文文件名关键词提取与聚类（词频思想，参考 TF 关键词聚合） ----------
TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}")
NUM_PREFIX_RE = re.compile(r"^[\d\s.\-、_()（）#]+")

def extract_runs(name):
    """从目录/文件名提取候选词：去序号前缀后，取连续汉字段或字母数字段（≥2字）。"""
    return [m for m in TOKEN_RE.findall(NUM_PREFIX_RE.sub("", name)) if len(m) >= 2]


def cluster_hints(names, min_members=2, top=5):
    """在名称集合里找「出现在 ≥min_members 个名称中」的关键词，作为合并主题提示。
    长词优先、被长词完全覆盖且不带来新成员的短词丢弃；返回 [(词, 成员数)] 降序。"""
    runs = {}
    for n in names:
        for r in set(extract_runs(n)):
            runs.setdefault(r, set()).add(n)
    kept = []  # (词, 成员集合)
    for r in sorted(runs, key=len, reverse=True):
        if any(r in k for k, _ in kept):
            continue  # 已有更具体的词覆盖它
        kept.append((r, runs[r]))
    hints = [(k, len(m)) for k, m in kept if len(m) >= min_members]
    hints.sort(key=lambda x: (-x[1], -len(x[0])))
    return hints[:top]
