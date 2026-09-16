# -*- coding: utf-8 -*-
"""深度内容提取（最后手段）：文件名判断不了性质时，读文档正文前 N 字供人工/模型定类。

支持的格式与读取方式：
- .docx/.xlsx/.pptx : zip 内 XML（旧版 pptx 误用文件系统 glob，导致永远读不到内容——已修）
- .pdf              : pdftotext 命令 → pypdf 库 依次回退，都不在则报大小并跳过
- .txt/.md/.csv     : 直接读

全部参数可调（--ext / --head / --min-size / --limit），无任何硬编码路径。

用法:
  python3 ao_deep.py --root /path/to/项目 --domain 某域 --out _deep_report.txt
  python3 ao_deep.py --root /path/to/项目 --limit 5 --ext pdf,docx
"""
import os
import re
import io
import zipfile
import html
import argparse

from ao_common import Excludes, walk_rel, extract_runs, workdir, WORKDIR_NAME

DEFAULT_EXTS = "docx,xlsx,pptx,pdf,txt,md,csv"
HEAD = 600  # 默认每文件截取的正文长度


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def read_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    # 注意 <w:t(?:\s...)?> 不能写成 <w:t[^>]*>，否则会误吞 <w:tab/> 等标签
    return _clean(" ".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)))


def read_xlsx(path):
    with zipfile.ZipFile(path) as z:
        try:
            shared = z.read("xl/sharedStrings.xml").decode("utf-8", errors="ignore")
        except KeyError:
            return "(纯公式表，无共享字符串)"
    return _clean(" ".join(re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", shared, re.S)))


def read_pptx(path):
    """遍历 zip 内部成员列表（旧版用 glob 查文件系统，永远为空——已修）。"""
    texts = []
    with zipfile.ZipFile(path) as z:
        slides = sorted(n for n in z.namelist()
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        for name in slides:
            xml = z.read(name).decode("utf-8", errors="ignore")
            found = re.findall(r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", xml, re.S)
            if found:
                texts.append(_clean(" ".join(found)))
    return "\n".join(texts)


def read_pdf(path):
    try:
        import subprocess
        out = subprocess.run(["pdftotext", path, "-"], capture_output=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return _clean(out.stdout.decode("utf-8", errors="ignore"))
    except Exception:
        pass
    try:  # 回退：pypdf（无外部命令依赖）
        from pypdf import PdfReader
        reader = PdfReader(path)
        text = " ".join((p.extract_text() or "") for p in reader.pages[:5])
        return _clean(text)
    except Exception:
        return f"[PDF无法解码, {os.path.getsize(path) // 1024}KB]"


def read_plain(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return _clean(f.read())


READERS = {".docx": read_docx, ".xlsx": read_xlsx, ".pptx": read_pptx,
           ".pdf": read_pdf, ".txt": read_plain, ".md": read_plain, ".csv": read_plain}


def extract_text(fp, head):
    ext = os.path.splitext(fp)[1].lower()
    reader = READERS.get(ext)
    if not reader:
        return f"[暂不支持读取 {ext}]"
    try:
        return reader(fp)[:head]
    except Exception as e:
        return f"[解码异常:{e.__class__.__name__}]"


def main():
    ap = argparse.ArgumentParser(description="深度内容提取（只读，不改结构）")
    ap.add_argument("--root", required=True, help="归档根目录")
    ap.add_argument("--domain", default=None, help="只深挖指定一级域（默认全部）")
    ap.add_argument("--out", default=None,
                    help="报告输出文件（默认 <root>/归档整理/深度提取报告.txt）")
    ap.add_argument("--ext", default=DEFAULT_EXTS, help=f"逗号分隔的扩展名（默认 {DEFAULT_EXTS}）")
    ap.add_argument("--head", type=int, default=HEAD, help=f"每文件截取正文长度（默认{HEAD}）")
    ap.add_argument("--min-size", type=int, default=512, help="小于该字节数的文件跳过（默认512）")
    ap.add_argument("--limit", type=int, default=0, help="每个域最多读多少个文件（0=不限）")
    ap.add_argument("--exclude", action="append", default=[], help="追加忽略名称/模式")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"[错误] 归档根不存在: {root}")
    ex = Excludes(args.exclude)
    ex.names.add(WORKDIR_NAME)  # 产物目录不深挖
    out_path = args.out or os.path.join(workdir(root), "深度提取报告.txt")
    exts = {"." + e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}

    # 一级域列表：指定 --domain 则只看它；伪域 (根目录) 兼容根下散文件
    doms = [d for d in sorted(os.listdir(root))
            if os.path.isdir(os.path.join(root, d)) and ex.dir_ok(d)]
    if args.domain:
        if args.domain not in doms:
            raise SystemExit(f"[错误] 域不存在: {args.domain}（可选: {', '.join(doms)}）")
        doms = [args.domain]

    out_fh = open(out_path, "w", encoding="utf-8")

    def emit(line):
        out_fh.write(line + "\n")

    emit("=" * 72)
    emit(f"深度提取范围: {', '.join(doms)}  扩展名: {', '.join(sorted(exts))}")
    emit("=" * 72)

    for dom in doms:
        base = os.path.join(root, dom)
        n_read = 0
        for kind, rel, abs_ in walk_rel(base, ex):
            if kind != "f":
                continue
            if os.path.splitext(rel)[1].lower() not in exts:
                continue
            if os.path.getsize(abs_) < args.min_size:
                continue
            if args.limit and n_read >= args.limit:
                emit(f"…（{dom} 已达 --limit {args.limit}，停止）")
                break
            full_rel = os.path.join(dom, rel)
            text = extract_text(abs_, args.head)
            emit(f"\n### {full_rel}")
            emit(text or "(空文档)")
            runs = extract_runs(os.path.splitext(os.path.basename(rel))[0])
            if runs:
                emit(f"[文件名关键词] {' / '.join(runs[:6])}")
            n_read += 1
        emit(f"\n✓ {dom} 完成（提取 {n_read} 个文件）\n")

    if out_fh:
        out_fh.close()
        print(f"✓ 报告已写入: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
