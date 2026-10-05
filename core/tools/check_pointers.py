#!/usr/bin/env python3
"""只读指针核对:扫描 markdown 文本里的五类指针,逐类核验存在性,产出悬空清单。退出码恒 0。

五类指针(判定规则):
  ①闸号      `闸 N` 与 `闸 N①`;存在性=私库 desk/宪法.md 的 `^### 闸 N` 标题集合,
              子项=该闸标题行以下的正文里出现该带圈数字(标题行的【①核心】/
              【②可选】标记不算子项)。
  ②版本号    `宪法 v1.4` 与 `XX模板_v1`;已知集=宪法.md 首行版本+历史附录全部版本
              (模板取两仓名字含「模板」的文件名版本并集);`A → B` 两边各自独立判。
  ③路径      以 desk/ core/ docs/ examples/ .agents/ .claude/ photo/ _work/ _office/
              开头的仓相对路径(_office/ 映射办公仓根,其余映射私库根);
              `X:\\...` 绝对路径直接验盘,反引号与「、」等分隔符处截断、每条单独核。
              先抽⑤并遮蔽已消费区段,再抽③,避免误判。
  ④需求号与单号  `需求-NNN` 存在性=办公仓里有文件名以 需求-NNN 开头的文件;
              `T-NNNNNN` 存在性=台面库 items/ 下每单一文件(core/tools/tickets/store.py
              的文件后端格式:items/T-NNNNNN.json)。
  ⑤文件:行   `名字.md:125`(行号任意位数;冒号前须是名单扩展名的文件名,
              纯时间 12:30 不会误中);文件名在两仓全树建
              「文件名→路径集」索引;找不到文件、行号或区间末行超总行数即悬空;
              行号后直接跟「…」/“…”引文时,引文去空白按 … 分段后须逐段含于该行。
  增量(`--baseline`)按 (类别, 原文, 文件) 做多重集(计数)比对:某键本次多于
  基线,差额份进「新悬空」;基线多于本次,差额份进「已消失」;行号不在键里,
  同一悬空挪行零影响,重复指针按份数计。

只读铁律(写死在代码里):
  - 全程不以写模式打开任何被扫文件;除 --out 目录外不在任何地方创建东西。
  - 只扫所给根本身的真实子树:目录里的符号链接与 Windows junction 一律不下钻、
    链接文件不扫(实测办公仓根下有指向仓外 D:/mmoharness/.agents 的 junction,
    os.walk 默认会钻进去);解析后不在所扫根内的文件直接跳过,绝不入清单。
  - git 子进程只允许 rev-parse 一个子命令。
  - 永远退出 0;一切异常只打一行提示。

用法:
  python -X utf8 core/tools/check_pointers.py --out <输出目录> [--baseline <上次跑例目录>]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

REPORT_NAMES = {"悬空清单.md", "新悬空.md", "已消失.md"}
SKIP_DIR_NAMES = {".git", "__pycache__"}

CAT_GATE = "①闸号"
CAT_VERSION = "②版本号"
CAT_PATH = "③路径"
CAT_TICKET = "④需求号单号"
CAT_FILELINE = "⑤文件:行"
CAT_ORDER = {CAT_GATE: 0, CAT_VERSION: 1, CAT_PATH: 2, CAT_TICKET: 3, CAT_FILELINE: 4}

GATE_REF_RE = re.compile(r"闸\s*(\d+)(\s*[\u2460-\u2473])?")
GATE_HEAD_RE = re.compile(r"^### 闸\s*(\d+)")
HEADING_RE = re.compile(r"^#{1,6}\s")
CONST_VER_REF_RE = re.compile(r"宪法\s*v(\d+(?:\.\d+)*)(?:\s*\u2192\s*v(\d+(?:\.\d+)*))?")
TPL_VER_REF_RE = re.compile(r"([A-Za-z\u4e00-\u9fa5_-]*模板)[_ ]v(\d+)(?:\s*\u2192\s*v(\d+))?")
TPL_VER_NAME_RE = re.compile(r"模板[_ ]v(\d+)")
ANY_VER_RE = re.compile(r"v(\d+(?:\.\d+)*)")
REQ_REF_RE = re.compile(r"(需求-\d{3})(?!\d)")
TICKET_REF_RE = re.compile(r"(T-\d{6})(?!\d)")
REL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./\u4e00-\u9fa5-])"
    r"((?:\.agents|\.claude|_office|_work|examples|desk|docs|core|photo)/"
    r"[A-Za-z0-9_.\-\u4e00-\u9fa5/]+)")
ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s「」“”\"'()（）,，。；;：:`、]+")
FILE_LINE_RE = re.compile(
    r"([\w\u4e00-\u9fa5.-]+\.(?:md|py|json|txt|cfg|ini))[:\uff1a](\d+)(?:~(\d+))?")
ENTRY_LINE_RE = re.compile(r"^- \[([^\]]+)\] (.+?):(\d+) 「(.+)」$")
TRAILING_PUNCT = ".,;:!?)]}）】」”’\"'`…。、；:"


def fold_ws(text):
    """去空白折叠:引文比对前把所有空白字符删掉。"""
    return re.sub(r"\s+", "", text)


def strip_trailing(path_text):
    return path_text.rstrip(TRAILING_PUNCT)


def git_rev_parse(cwd, ref):
    """只读的 git 访问:只允许 rev-parse 一个子命令,失败返回 None。"""
    try:
        result = subprocess.run(["git", "-C", str(cwd), "rev-parse", ref],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip() or None


def read_lines(path):
    """只读读取;缓存行列表。绝无写模式。"""
    cache = read_lines.cache
    if path not in cache:
        try:
            cache[path] = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            cache[path] = []
    return cache[path]


read_lines.cache = {}


@dataclass
class Pointer:
    cat: str
    text: str
    dangle: bool


@dataclass
class Entry:
    cat: str
    text: str
    ref: str      # 引用文件:行(所扫根内的仓相对路径)
    area: str     # 覆盖行的区域标签
    dangle: bool

    @property
    def base_key(self):
        """增量比对键:(类别, 原文, 文件);不含行号,挪行不影响增量。"""
        return (self.cat, self.text, self.ref.rpartition(":")[0])

    def render(self):
        return "- [{}] {} 「{}」".format(self.cat, self.ref, self.text)


@dataclass
class Context:
    repo_root: Path
    office_root: Path
    office_exists: bool
    office_reqs: set | None          # None = 办公仓不存在,需求号跳过
    tickets_root: Path
    tickets_ok: bool                 # 台面库在不在
    gate_blocks: dict | None         # 闸号 -> 标题块文本;None = 宪法.md 不存在,跳过
    const_versions: set | None       # None = 宪法与附录都不在,宪法版本跳过
    template_versions: set
    name_index: dict                 # 文件名 -> 路径列表(两仓全树)
    notes: list = field(default_factory=list)


@dataclass
class Area:
    label: str
    root: Path
    base: Path                       # 相对化基准(repo 根 / 办公仓根 / 扫描根本身)
    prefix: str                      # 引用文件路径前缀(如 desk/ 或 _office/)
    exists: bool


def inside_any(path, resolved_dirs):
    for skip in resolved_dirs:
        try:
            if path == skip or skip in path.parents:
                return True
        except (OSError, ValueError):
            continue
    return False


def excluded_resolved(args_out, args_baseline):
    dirs = [Path(args_out).resolve()]
    if args_baseline:
        dirs.append(Path(args_baseline).resolve())
    return dirs


def is_linklike(path):
    """符号链接或 Windows junction:会把所给根之外的目录接进来,一律不下钻、不扫。"""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def walk_tree(root, skip_dirs):
    """os.walk 的只读遍历:剪掉 .git、__pycache__、排除目录与一切符号链接/junction。

    junction 不是 symlink,os.walk 的 followlinks=False 拦不住它;不剪的话,
    办公仓根下的 .agents/.claude junction 会把仓外 D:/mmoharness/.agents 的
    技能目录整棵接进扫描。链接文件同理不当作被扫文件。
    """
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        folder = Path(dirpath)
        dirnames[:] = [name for name in dirnames
                       if name not in SKIP_DIR_NAMES
                       and not is_linklike(folder / name)
                       and not inside_any(folder / name, skip_dirs)]
        yield folder, [name for name in filenames
                       if not is_linklike(folder / name)]


def iter_markdown_files(area, skip_dirs):
    """产出 (文件, 引用文件路径);解析后不在所扫根内的文件直接跳过。"""
    for folder, names in walk_tree(area.root, skip_dirs):
        for name in sorted(names):
            if not name.endswith(".md") or name in REPORT_NAMES:
                continue
            path = folder / name
            if inside_any(path, skip_dirs):
                continue
            ref = area_ref(area, path)
            if ref is None:
                continue
            yield path, ref


def area_ref(area, path):
    """所扫根内的仓相对路径;resolve() 后逃出根内(借链接进来的)返回 None。"""
    try:
        rel = path.resolve().relative_to(area.base).as_posix()
    except (ValueError, OSError):
        return None
    return area.prefix + rel


def load_gate_blocks(repo_root):
    """闸号 -> 标题块正文;块=`### 闸 N` 标题行以下、到下一道闸标题或任何更早的标题行为止。

    标题行本身不算正文:子项(带圈数字)只在标题行以下找,标题行的
    【①核心】/【②可选】标记不是子项,否则无子项的闸配 ①/② 会漏判悬空。
    """
    path = repo_root / "desk" / "宪法.md"
    if not path.is_file():
        return None
    lines = read_lines(path)
    heads = [(i, int(m.group(1))) for i, line in enumerate(lines)
             for m in [GATE_HEAD_RE.match(line)] if m]
    blocks = {}
    for pos, (start, number) in enumerate(heads):
        end = len(lines)
        if pos + 1 < len(heads):
            end = heads[pos + 1][0]
        j = start + 1
        while j < end and not HEADING_RE.match(lines[j]):
            j += 1
        blocks[number] = "\n".join(lines[start + 1:min(j, end)])
    return blocks


def load_constitution_versions(repo_root):
    constitution = repo_root / "desk" / "宪法.md"
    appendix = repo_root / "desk" / "宪法_历史附录.md"
    if not constitution.is_file() and not appendix.is_file():
        return None
    known = set()
    if constitution.is_file():
        first = read_lines(constitution)[:1]
        if first:
            known.update(m.group(1) for m in ANY_VER_RE.finditer(first[0]))
    if appendix.is_file():
        known.update(m.group(1) for m in ANY_VER_RE.finditer("\n".join(read_lines(appendix))))
    return known


def load_template_versions(roots, skip_dirs):
    known = set()
    for root in roots:
        for folder, names in walk_tree(root, skip_dirs):
            for name in names:
                if "模板" not in name:
                    continue
                for m in TPL_VER_NAME_RE.finditer(name):
                    known.add(str(int(m.group(1))))
    return known


def load_office_requirements(office_root, skip_dirs):
    found = set()
    for folder, names in walk_tree(office_root, skip_dirs):
        for name in names:
            m = re.match(r"(需求-\d{3})(?!\d)", name)
            if m:
                found.add(m.group(1))
    return found


def build_name_index(roots, skip_dirs):
    index = {}
    for root in roots:
        for folder, names in walk_tree(root, skip_dirs):
            for name in names:
                if name in REPORT_NAMES:
                    continue
                index.setdefault(name, []).append(folder / name)
    return index


def extract_quote(text, pos):
    """⑤ 行号之后直接跟的引文(无空格隔开);「…」与“…”两种,未闭合算没有引文。"""
    rest = text[pos:]
    for opener, closer in (("\u300c", "\u300d"), ("\u201c", "\u201d")):
        if rest.startswith(opener):
            end = rest.find(closer, 1)
            if end == -1:
                return None
            return rest[1:end]
    return None


def quote_in_line(quote, line):
    folded = fold_ws(quote)
    target = fold_ws(line)
    if not folded:
        return True
    parts = [part for part in re.split(r"…|\.\.\.", folded) if part]
    if not parts:
        return True
    return all(part in target for part in parts)


def judge_file_line_matches(line, ctx):
    results = []
    for m in FILE_LINE_RE.finditer(line):
        name = m.group(1)
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else None
        dangle = True
        for path in ctx.name_index.get(name, []):
            lines = read_lines(path)
            total = len(lines)
            if not 1 <= start <= total:
                continue
            if end is not None and end > total:
                continue
            quote = extract_quote(line, m.end())
            if quote is not None and not quote_in_line(quote, lines[start - 1]):
                continue
            dangle = False
            break
        results.append((Pointer(CAT_FILELINE, m.group(0), dangle), m.start(), m.end()))
    return results


def judge_line(line, ctx):
    pointers = []

    # ⑤ 先抽:把已消费区段遮蔽后再抽③,避免把 文件:行 误当路径。
    file_matches = judge_file_line_matches(line, ctx)
    for pointer, _, _ in file_matches:
        pointers.append(pointer)
    chars = list(line)
    for _, start, end in file_matches:
        for i in range(start, end):
            chars[i] = " "
    masked = "".join(chars)

    for m in GATE_REF_RE.finditer(line):
        if ctx.gate_blocks is None:
            continue
        number = int(m.group(1))
        sub = m.group(2)
        dangle = number not in ctx.gate_blocks
        if not dangle and sub:
            dangle = sub.strip() not in ctx.gate_blocks[number]
        pointers.append(Pointer(CAT_GATE, m.group(0).strip(), dangle))

    for m in CONST_VER_REF_RE.finditer(line):
        if ctx.const_versions is None:
            continue
        sides = [m.group(1)] + ([m.group(2)] if m.group(2) else [])
        dangle = any(side not in ctx.const_versions for side in sides)
        pointers.append(Pointer(CAT_VERSION, m.group(0), dangle))
    for m in TPL_VER_REF_RE.finditer(line):
        sides = [str(int(m.group(2)))] + ([str(int(m.group(3)))] if m.group(3) else [])
        dangle = any(side not in ctx.template_versions for side in sides)
        pointers.append(Pointer(CAT_VERSION, m.group(0), dangle))

    for m in REQ_REF_RE.finditer(line):
        if ctx.office_reqs is None:
            continue
        pointers.append(Pointer(CAT_TICKET, m.group(1), m.group(1) not in ctx.office_reqs))
    for m in TICKET_REF_RE.finditer(line):
        if not ctx.tickets_ok:
            continue
        ticket_id = m.group(1)
        exists = (ctx.tickets_root / "items" / (ticket_id.upper() + ".json")).is_file()
        pointers.append(Pointer(CAT_TICKET, ticket_id, not exists))

    for m in REL_PATH_RE.finditer(masked):
        rel = strip_trailing(m.group(1))
        if rel.startswith("_office/"):
            target = ctx.office_root / rel[len("_office/"):]
        else:
            target = ctx.repo_root / rel
        pointers.append(Pointer(CAT_PATH, rel, not target.exists()))
    for m in ABS_PATH_RE.finditer(masked):
        raw = strip_trailing(m.group(0))
        pointers.append(Pointer(CAT_PATH, raw, not Path(raw).exists()))

    return pointers


def scan_areas(areas, ctx, skip_dirs):
    entries = []
    for area in areas:
        if not area.exists:
            continue
        for path, ref_file in iter_markdown_files(area, skip_dirs):
            for number, line in enumerate(read_lines(path), 1):
                for pointer in judge_line(line, ctx):
                    entries.append(Entry(pointer.cat, pointer.text,
                                         "{}:{}".format(ref_file, number),
                                         area.label, pointer.dangle))
    return entries


def parse_baseline(path):
    """读上一跑例 悬空清单.md 的悬空条目;文件缺失返回 None(跳过增量)。"""
    if not path.is_file():
        return None
    entries = []
    for line in read_lines(path):
        m = ENTRY_LINE_RE.match(line)
        if m:
            entries.append(Entry(m.group(1), m.group(4),
                                 "{}:{}".format(m.group(2), m.group(3)), "", True))
    return entries


def entry_sort_key(entry):
    return (split_ref(entry.ref), CAT_ORDER.get(entry.cat, 9), entry.text)


def diff_dangling(current, baseline):
    """按 (类别, 原文, 文件) 的多重集(计数)比对产增量。

    某键本次计数 > 基线计数 → 「新悬空」列出差额份(用本次跑的行号);
    基线计数 > 本次计数 → 「已消失」列出差额份(用基线的行号);
    计数相等两份都不进。行号不在键里,同一悬空挪了行零影响;重复指针按份数计。
    """
    def grouped(entries):
        groups = {}
        for entry in entries:
            groups.setdefault(entry.base_key, []).append(entry)
        return groups

    current_groups = grouped(current)
    baseline_groups = grouped(baseline)
    fresh, gone = [], []
    for key, group in current_groups.items():
        extra = len(group) - len(baseline_groups.get(key, []))
        if extra > 0:
            fresh.extend(sorted(group, key=entry_sort_key)[:extra])
    for key, group in baseline_groups.items():
        extra = len(group) - len(current_groups.get(key, []))
        if extra > 0:
            gone.extend(sorted(group, key=entry_sort_key)[:extra])
    fresh.sort(key=entry_sort_key)
    gone.sort(key=entry_sort_key)
    return fresh, gone


def write_report(path, title, entries):
    lines = ["# {} · {}".format(title, datetime.now().isoformat(timespec="seconds")), ""]
    for entry in entries:
        lines.append(entry.render())
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def build_context(args, repo_root, office_root, skip_dirs):
    office_exists = office_root.is_dir()
    tickets_root = Path(args.tickets_data).resolve() if args.tickets_data \
        else repo_root / "core" / "data"
    tickets_ok = tickets_root.is_dir()
    gate_blocks = load_gate_blocks(repo_root)
    const_versions = load_constitution_versions(repo_root)
    notes = []
    if not office_exists:
        notes.append("办公仓不存在")
    if not tickets_ok:
        notes.append("台面库不存在,跳过")
    if gate_blocks is None:
        notes.append("宪法.md 不存在,闸号核验跳过")
    if const_versions is None:
        notes.append("宪法与历史附录均不存在,宪法版本核验跳过")
    index_roots = [root for root in (repo_root, office_root) if root.is_dir()]
    return Context(
        repo_root=repo_root,
        office_root=office_root,
        office_exists=office_exists,
        office_reqs=load_office_requirements(office_root, skip_dirs) if office_exists else None,
        tickets_root=tickets_root,
        tickets_ok=tickets_ok,
        gate_blocks=gate_blocks,
        const_versions=const_versions,
        template_versions=load_template_versions(index_roots, skip_dirs),
        name_index=build_name_index(index_roots, skip_dirs),
        notes=notes,
    )


def render_report(args, repo_root, office_root, areas, entries, ctx):
    dangling = [entry for entry in entries if entry.dangle]
    direct = bool(args.paths)
    if direct:
        head_line = "所核 (直扫模式,无 git 头)"
    else:
        head_repo = git_rev_parse(repo_root, "HEAD") or "(取不到)"
        if office_root.is_dir():
            head_office = git_rev_parse(office_root, "HEAD") or "(取不到)"
        else:
            head_office = "(办公仓不存在)"
        head_line = "所核私库头 {} · 办公仓头 {}".format(head_repo, head_office)
    coverage = []
    for area in areas:
        if not area.exists:
            coverage.append("{} 不存在".format(area.label))
        else:
            count = sum(1 for entry in dangling if entry.area == area.label)
            coverage.append("{} {}".format(area.label, count))
    lines = [
        "# 悬空清单 · {}".format(datetime.now().isoformat(timespec="seconds")),
        head_line,
        "覆盖:" + " · ".join(coverage),
        "总指针数 {} · 悬空数 {}".format(len(entries), len(dangling)),
        "",
    ]
    lines.extend("- {}".format(note) for note in ctx.notes)
    if ctx.notes:
        lines.append("")
    lines.append("## 悬空明细")
    lines.append("")
    if dangling:
        dangling.sort(key=entry_sort_key)
        lines.extend(entry.render() for entry in dangling)
    else:
        lines.append("(无)")
    return "\n".join(lines) + "\n"


def split_ref(ref):
    """引用处 文件:行 拆成 (文件, 行号数值),行号非法按 0。"""
    file_part, _, line_part = ref.rpartition(":")
    try:
        return (file_part, int(line_part))
    except ValueError:
        return (ref, 0)


def run(args):
    script_dir = Path(__file__).resolve().parent
    if args.repo:
        repo_root = Path(args.repo).resolve()
    else:
        toplevel = git_rev_parse(script_dir, "--show-toplevel")
        if not toplevel:
            print("check_pointers: 取不到私库根(git rev-parse --show-toplevel 失败),"
                  "请用 --repo 显式指定。")
            return 0
        repo_root = Path(toplevel).resolve()
    office_root = Path(args.office).resolve() if args.office \
        else repo_root.parent / "_office"
    skip_dirs = excluded_resolved(args.out, args.baseline)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.paths:
        areas = []
        for raw in args.paths:
            root = Path(raw).resolve()
            label = root.as_posix()
            areas.append(Area(label, root, root, "", root.is_dir()))
    else:
        areas = [
            Area("desk/", repo_root / "desk", repo_root, "", (repo_root / "desk").is_dir()),
            Area(".agents/skills/", repo_root / ".agents" / "skills", repo_root, "",
                 (repo_root / ".agents" / "skills").is_dir()),
            Area(".claude/skills/", repo_root / ".claude" / "skills", repo_root, "",
                 (repo_root / ".claude" / "skills").is_dir()),
            Area("_office/", office_root, office_root, "_office/", office_root.is_dir()),
        ]

    ctx = build_context(args, repo_root, office_root, skip_dirs)
    baseline_entries = None
    if args.baseline:
        baseline_report = Path(args.baseline).resolve() / "悬空清单.md"
        baseline_entries = parse_baseline(baseline_report)
        if baseline_entries is None:
            ctx.notes.append("baseline 不存在,跳过增量")
    entries = scan_areas(areas, ctx, skip_dirs)
    report = render_report(args, repo_root, office_root, areas, entries, ctx)
    with open(out_dir / "悬空清单.md", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report)

    if args.baseline and baseline_entries is not None:
        fresh, gone = diff_dangling([entry for entry in entries if entry.dangle],
                                    baseline_entries)
        write_report(out_dir / "新悬空.md", "新悬空", fresh)
        write_report(out_dir / "已消失.md", "已消失", gone)
    return 0


class AlwaysZeroParser(argparse.ArgumentParser):
    """退出码恒 0:连参数错误也只提示一行、以 0 退出。"""

    def error(self, message):
        print("check_pointers: " + message + "(退出码恒 0)", file=sys.stderr)
        raise SystemExit(0)


def main(argv=None):
    parser = AlwaysZeroParser(
        prog="check_pointers.py",
        description="只读指针核对:扫描 md 文本里的闸号/版本号/路径/需求号单号/文件:行五类指针,"
                    "产出悬空清单与增量;纯标准库,只读,退出码恒 0。",
        epilog="例子:\n"
               "  python -X utf8 core/tools/check_pointers.py --out 报告目录\n"
               "  python -X utf8 core/tools/check_pointers.py --out 报告目录 --baseline 上次报告目录\n"
               "  python -X utf8 core/tools/check_pointers.py --out 报告目录 --paths desk 别处/文档\n"
               "git 子进程只用 rev-parse;被扫文件绝无写模式。")
    parser.add_argument("--out", required=True, help="输出目录(不存在则建)")
    parser.add_argument("--baseline", help="上一跑例目录;其下 悬空清单.md 缺失则记一行并跳过增量")
    parser.add_argument("--repo", help="私库根;默认=脚本所在文件向上 git rev-parse --show-toplevel")
    parser.add_argument("--office", help="办公仓根;默认=<私库根>/../_office")
    parser.add_argument("--tickets-data", help="台面库目录;默认=<私库根>/core/data")
    parser.add_argument("--paths", nargs="+", metavar="目录",
                        help="显式扫描根(可多值);给出后不做默认扫描、不印两仓头(直扫模式)")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # 退出码恒 0:意外也只报一行。
        print("check_pointers: " + str(exc), file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
