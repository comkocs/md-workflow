#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fold_index.py:把记忆索引(MEMORY.md)超限的整节原样折成二级入口。

规则出处:desk/memory/规范.md §5。纯标准库,Python 3.8+;读写一律 UTF-8、无 BOM、LF。

用法:
  python fold_index.py <索引文件> [--section "<节名>"]... [--slug <slug>]...
                       [--auto] [--limit 24000] [--date YYYY-MM-DD] [--dry-run]
  python fold_index.py --selftest [--out <目录>]

折法:
  1. 按 "## " 行定位节。节体 = 标题下一行起,到下一个 "# " 或 "## " 标题之前;
     代码围栏(``` 或 ~~~)里的行不算标题。
  2. 节体首尾的空行留在原位,中间全部行原文逐字搬到同目录 <slug>-index.md:
     文件不存在就新建(带 frontmatter:name / description / metadata.type=reference,
     加 "# " 标题);已存在就把这些行原文追加到末尾。
  3. 原位保留 "## " 标题,节体换成一行入口:
       - [<节名>入口](<slug>-index.md) — <N> 条已折入 <日期>
  4. 折过又长出新条目的节:节首那几行「已指向同一个 <slug>-index.md 的入口行」留在原位,
     只搬它们后面的新行,新入口行加在旧入口行下面。
  5. slug:给了 --slug 就用它(与 --section 按先后一一配对);没给时,节名全是 ASCII
     就取小写、非字母数字连成一个短横线;节名含非 ASCII 就取
     section-<节名 UTF-8 编码的 sha1 前 8 位>。同名节永远得到同一个 slug,与节的先后无关。
     slug 末尾的 "-index" 会被去掉,免得生成 xxx-index-index.md。

条目 = 以 "- " 顶格开头的行(缩进的子项不算条目,但照样逐字搬)。守恒式:
  折前主索引条目数 = 折后主索引条目数 - 新增入口行数 + 本次搬入二级入口的条目数
另核:每个被搬行在二级入口里逐字存在;把入口行换回被搬的那一块能逐字还原折前全文;
二级入口原有内容一字不动。任一核对失败:不写任何文件、非零退出、打印原因。
先写临时文件,全部核过再原子替换(二级入口先、主索引最后),写完回读再比一次。

报告最后一行固定是 "末行: <折后主索引最后一行>",与 tail -1 <折后文件> 逐字相同。

退出码:0 成功;1 核对失败或输入不合规;2 参数错误。
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile

DEFAULT_LIMIT = 24000
ENTRY_PREFIX = '- '
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SELFTEST_MARKER = '.fold_selftest'


class FoldError(Exception):
    """核对失败或输入不合规。"""


# ---------------------------------------------------------------- 基础读写

def setup_stdio():
    """stdout/stderr 一律 UTF-8、只出 LF,防 Windows 控制台乱码和 CRLF。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', newline='\n')
        except (AttributeError, ValueError):
            pass


def read_text(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        raise FoldError('读不到 %s: %s' % (path, e))
    if data.startswith(b'\xef\xbb\xbf'):
        raise FoldError('%s 带 UTF-8 BOM;记忆文件要求无 BOM,先去掉再折' % path)
    if b'\r' in data:
        raise FoldError('%s 含 CR(CRLF 换行);记忆文件要求 LF,先转换再折' % path)
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise FoldError('%s 不是合法 UTF-8: %s' % (path, e))


def write_plain(path, text):
    with open(path, 'wb') as f:
        f.write(text.encode('utf-8'))


def to_lines(text):
    """拆行,返回 (行列表, 末尾是否有换行)。末尾换行不产生多余空行,与 tail -1 同口径。"""
    if text == '':
        return [], False
    if text.endswith('\n'):
        return text[:-1].split('\n'), True
    return text.split('\n'), False


def from_lines(lines, trailing_newline):
    text = '\n'.join(lines)
    if lines and trailing_newline:
        text += '\n'
    return text


def last_line(text):
    """与 tail -1 同义的末行(不含换行符)。"""
    lines, _ = to_lines(text)
    return lines[-1] if lines else ''


def count_entries(lines):
    return sum(1 for ln in lines if ln.startswith(ENTRY_PREFIX))


def yaml_scalar(s):
    """frontmatter 的值:普通文字原样写;含 YAML 特殊组合时用双引号。"""
    risky = (': ' in s or ' #' in s or s.endswith(':') or s != s.strip()
             or (s and s[0] in '!&*[]{}|>%@`"\',?-#'))
    return json.dumps(s, ensure_ascii=False) if risky else s


# ---------------------------------------------------------------- 分节与 slug

class Section(object):
    def __init__(self, name, head, end):
        self.name = name   # "## " 后面的文字,去首尾空白
        self.head = head   # 标题行下标
        self.end = end     # 节体结束(不含)下标


def parse_sections(lines):
    heads = []
    in_fence = False
    for i, ln in enumerate(lines):
        if ln.startswith('```') or ln.startswith('~~~'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.startswith('# ') or ln.startswith('## '):
            heads.append(i)
    sections = []
    for k, i in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        if lines[i].startswith('## '):
            sections.append(Section(lines[i][3:].strip(), i, end))
    return sections


def strip_index_suffix(slug):
    return slug[:-len('-index')] if slug.endswith('-index') else slug


def make_slug(name):
    if name and all(ord(c) < 128 for c in name):
        s = strip_index_suffix(re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'))
        if s:
            return s
    return 'section-' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]


def check_slug(slug):
    s = strip_index_suffix(slug.strip())
    if not SLUG_RE.match(s):
        raise FoldError('slug「%s」不合规:只许小写字母、数字与单个短横线' % slug)
    return s


def find_section(lines, name):
    hits = [s for s in parse_sections(lines) if s.name == name]
    if not hits:
        names = [s.name for s in parse_sections(lines)]
        raise FoldError('找不到节「%s」。现有节: %s' % (name, ' / '.join(names) or '(无)'))
    if len(hits) > 1:
        raise FoldError('有 %d 个同名节「%s」,先改名再折' % (len(hits), name))
    return hits[0]


# ---------------------------------------------------------------- 折一节

def plan_fold(lines, sec, slug, date):
    """算出一节怎么折。返回 (a, b, block, n, entry):节体[a:b] 是被搬块。"""
    body = lines[sec.head + 1:sec.end]
    b = len(body)
    while b > 0 and body[b - 1].strip() == '':
        b -= 1
    link = '](%s-index.md)' % slug
    a = 0
    while a < b and (body[a].strip() == '' or
                     (body[a].startswith(ENTRY_PREFIX) and link in body[a])):
        a += 1
    block = body[a:b]
    for ln in block:
        if ln.startswith(ENTRY_PREFIX) and link in ln:
            raise FoldError('节「%s」中段有指向 %s-index.md 的入口行,折进去会自己指向自己;'
                            '先把它挪到节首再折' % (sec.name, slug))
    n = count_entries(block)
    entry = '- [%s入口](%s-index.md) — %d 条已折入 %s' % (sec.name, slug, n, date)
    return a, b, block, n, entry


def new_secondary_text(slug, sec_name, src_name, date, block):
    desc = '%s 的二级入口,%s 起由 fold_index.py 从 %s 原样折入' % (sec_name, date, src_name)
    header = [
        '---',
        'name: %s-index' % slug,
        'description: %s' % yaml_scalar(desc),
        'metadata:',
        '  type: reference',
        '---',
        '',
        '# %s' % sec_name,
        '',
        '以下各行 %s 起由 fold_index.py 从 %s「%s」节原样折入,逐字未改;后续折入追加在末尾。'
        % (date, src_name, sec_name),
        '',
    ]
    return '\n'.join(header + block) + '\n'


def append_block(old, block):
    sep = '' if (old == '' or old.endswith('\n')) else '\n'
    return old + sep + '\n'.join(block) + '\n'


class FoldRecord(object):
    def __init__(self, name, filename, created, n_entries, n_lines):
        self.name = name
        self.filename = filename
        self.created = created
        self.n_entries = n_entries
        self.n_lines = n_lines


class FoldResult(object):
    def __init__(self):
        self.records = []
        self.before_chars = 0
        self.after_chars = 0
        self.before_entries = 0
        self.after_entries = 0
        self.final_text = ''
        self.pending = {}


def run_fold(index_path, sections, slugs, auto, limit, date, dry_run):
    """执行折叠。成功返回 (报告行列表, FoldResult);任何核对失败抛 FoldError 且不写文件。"""
    index_path = os.path.abspath(index_path)
    text = read_text(index_path)
    lines, trailing = to_lines(text)
    orig_lines = list(lines)
    base_dir = os.path.dirname(index_path)
    src_name = os.path.basename(index_path)
    res = FoldResult()
    pending = res.pending   # 文件名 -> {'path', 'old', 'new'}
    notes = []
    state = {'lines': lines}

    def do_fold(sec, slug):
        cur = state['lines']
        a, b, block, n, entry = plan_fold(cur, sec, slug, date)
        if n == 0:
            raise FoldError('节「%s」里没有可搬的条目行(可能已经折过)' % sec.name)
        fname = slug + '-index.md'
        fpath = os.path.join(base_dir, fname)
        if os.path.normcase(os.path.abspath(fpath)) == os.path.normcase(index_path):
            raise FoldError('二级入口 %s 与索引文件同名' % fname)
        if fname in pending:
            base = pending[fname]['new']
            created = pending[fname]['old'] is None
            new = append_block(base, block)
        elif os.path.exists(fpath):
            base = read_text(fpath)
            pending[fname] = {'path': fpath, 'old': base, 'new': base}
            created = False
            new = append_block(base, block)
        else:
            base = ''
            pending[fname] = {'path': fpath, 'old': None, 'new': ''}
            created = True
            new = new_secondary_text(slug, sec.name, src_name, date, block)
        # 核 1:二级入口原有内容一字不动
        if not new.startswith(base):
            raise FoldError('%s 原有内容被改动(内部错误)' % fname)
        # 核 2:每个被搬行在二级入口里逐字存在,且整块连续出现
        new_line_set = set(to_lines(new)[0])
        missing = [ln for ln in block if ln not in new_line_set]
        if missing or '\n'.join(block) not in new:
            raise FoldError('节「%s」有 %d 行在 %s 里找不到逐字原文' % (sec.name, len(missing), fname))
        # 核 3:二级入口新增条目数 == 搬走条目数
        gained = count_entries(to_lines(new)[0]) - count_entries(to_lines(base)[0])
        if gained != n:
            raise FoldError('%s 新增条目 %d 条,与搬走的 %d 条不符' % (fname, gained, n))
        # 主索引:原位换成一行入口
        body = cur[sec.head + 1:sec.end]
        new_lines = cur[:sec.head + 1] + body[:a] + [entry] + body[b:] + cur[sec.end:]
        pos = sec.head + 1 + a
        # 核 4:把入口行换回被搬块,能逐字还原折前
        if new_lines[:pos] + block + new_lines[pos + 1:] != cur:
            raise FoldError('节「%s」还原核对不一致(内部错误)' % sec.name)
        pending[fname]['new'] = new
        state['lines'] = new_lines
        res.records.append(FoldRecord(sec.name, fname, created, n, len(block)))

    # 1) 指定的节
    if len(slugs) > len(sections):
        raise FoldError('--slug 给了 %d 个,多于 --section 的 %d 个' % (len(slugs), len(sections)))
    for i, name in enumerate(sections):
        name = name.strip()
        slug = check_slug(slugs[i]) if i < len(slugs) else make_slug(name)
        do_fold(find_section(state['lines'], name), slug)

    # 2) --auto:从最大节起逐节折,直到 ≤ limit
    if auto:
        while len(from_lines(state['lines'], trailing)) > limit:
            best = None
            for sec in parse_sections(state['lines']):
                slug = make_slug(sec.name)
                try:
                    a, b, block, n, entry = plan_fold(state['lines'], sec, slug, date)
                except FoldError as e:
                    msg = '跳过: %s' % e
                    if msg not in notes:
                        notes.append(msg)
                    continue
                size = len('\n'.join(block))
                if n == 0 or size - len(entry) <= 0:
                    continue
                if best is None or size > best[0]:
                    best = (size, sec, slug)
            if best is None:
                raise FoldError('能折的节都折完了,仍有 %d 字符 > limit %d;需要删过期条目或拆主题'
                                % (len(from_lines(state['lines'], trailing)), limit))
            do_fold(best[1], best[2])

    final_lines = state['lines']
    final_text = from_lines(final_lines, trailing)
    res.before_chars = len(text)
    res.after_chars = len(final_text)
    res.before_entries = count_entries(orig_lines)
    res.after_entries = count_entries(final_lines)
    res.final_text = final_text
    added = len(res.records)
    moved = sum(r.n_entries for r in res.records)
    # 核 5:条目守恒
    right = res.after_entries - added + moved
    if res.before_entries != right:
        raise FoldError('条目守恒不成立: 折前 %d != 折后 %d - 新增入口 %d + 搬入 %d = %d'
                        % (res.before_entries, res.after_entries, added, moved, right))

    # 写:先临时文件,全部核过再原子替换,写后回读
    written = []
    if res.records and not dry_run:
        files = [(p['path'], p['new']) for p in pending.values()]
        files.append((index_path, final_text))
        written = atomic_write_all(files)

    # 报告
    rep = ['== fold_index.py 报告 ==',
           '索引文件: %s' % index_path,
           '量法: Python len(),按字符计(不是字节)',
           'limit: %d' % limit,
           '折前字符数: %d' % res.before_chars,
           '折后字符数: %d' % res.after_chars]
    if res.records:
        rep.append('折叠节数: %d' % len(res.records))
        for i, r in enumerate(res.records, 1):
            rep.append('  %d. 「%s」 -> %s(%s) 搬入 %d 条(共 %d 行)'
                       % (i, r.name, r.filename, '新建' if r.created else '追加',
                          r.n_entries, r.n_lines))
    else:
        rep.append('折叠节数: 0(已 ≤ limit,无需折叠)')
    for fname in sorted(pending):
        size = len(pending[fname]['new'])
        rep.append('二级入口: %s 折后 %d 字符' % (fname, size))
        if size > limit:
            rep.append('注意: %s 超过 limit;按规范 §5 先给它分 "## " 小节,再用本脚本对它折'
                       '(最多到三级)' % fname)
    rep.extend(notes)
    rep.append('守恒式: 折前条目 %d = 折后条目 %d - 新增入口 %d + 搬入 %d'
               % (res.before_entries, res.after_entries, added, moved))
    rep.append('守恒核对: 左 %d / 右 %d 成立' % (res.before_entries, right))
    if res.records:
        rep.append('逐字核对: 搬出 %d 行在二级入口里逐字存在;入口行换回原块可逐字还原折前全文'
                   % sum(r.n_lines for r in res.records))
    if res.after_chars > limit:
        rep.append('注意: 折后仍 > limit,再指定别的节折,或加 --auto')
    if dry_run:
        rep.append('写入: dry-run,未写任何文件')
    elif written:
        rep.append('写入: 已写 %d 个文件(临时文件核过后原子替换,写后回读一致)' % len(written))
    else:
        rep.append('写入: 无改动,未写文件')
    rep.append('末行: %s' % last_line(final_text))
    return rep, res


def atomic_write_all(files):
    temps = []
    done = []
    try:
        for path, text in files:
            d = os.path.dirname(os.path.abspath(path))
            fd, tmp = tempfile.mkstemp(prefix='.%s.' % os.path.basename(path), suffix='.tmp', dir=d)
            with os.fdopen(fd, 'wb') as f:
                f.write(text.encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
            temps.append((tmp, path))
        for tmp, path in temps:
            os.replace(tmp, path)
            done.append(path)
    except OSError as e:
        for tmp, _ in temps:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise FoldError('写文件失败: %s;已替换的文件: %s' % (e, ', '.join(done) or '无'))
    for path, text in files:
        if read_text(path) != text:
            raise FoldError('写后回读与预期不一致: %s' % path)
    return done


# ---------------------------------------------------------------- 自测

FAKE_SECTIONS = [          # (节名, 权重);权重拉平,让 --auto 必须连折两节以上
    ('换窗接管指针', 2),
    ('工单与流程', 6),
    ('派单与收口', 6),
    ('代码与仓库', 5),
    ('并线与部署', 5),
    ('设计口径与真源', 5),
    ('取证与实机验证', 4),
    ('数据与存储', 4),
    ('Tooling Notes', 3),
    ('项目与治理', 5),
]
FAKE_TOPICS = ['工单台', '接管件', '记忆索引', '子代理', '工作树', '提交信息', '部署头', '回读',
               '时间戳', '代理设置', '权限模式', '备份', '日志', '缓存', '构建', '测试集',
               '配置表', '数据库', '证据图', '换行符']
FAKE_ACTIONS = ['先核再写', '只读不改', '当场落记', '逐字搬运', '整份重写', '分开存放',
                '按字符量', '一次问完', '不许绕过', '写后回读']
FAKE_HOOKS = ['撞过一次才立的规矩', '别凭印象要现查', '以仓里现值为准', '放行和拦截两侧都要验',
              '超限先折整节', '收口交给子代理起草', '不在主检出里干活', '逐个路径添加',
              '失败就停下来报告', '余量不足就主动折', '跨窗复用的教训才写', '过期就删']


def fake_entry(si, k):
    t1 = FAKE_TOPICS[(si * 7 + k) % len(FAKE_TOPICS)]
    t2 = FAKE_TOPICS[(si * 3 + k * 5 + 1) % len(FAKE_TOPICS)]
    act = FAKE_ACTIONS[(si + k * 3) % len(FAKE_ACTIONS)]
    hook = FAKE_HOOKS[(si * 5 + k) % len(FAKE_HOOKS)]
    month = 8 + (si + k) % 2
    day = 1 + (si * 11 + k * 7) % 28
    return ('- [%s%s·第 %d 条](s%02d-item-%03d.md) — %02d-%02d 定;%s;碰%s之前先读这一条'
            % (t1, act, k, si + 1, k, month, day, hook, t2))


def render_fake(counts):
    out = []
    for si, (name, _) in enumerate(FAKE_SECTIONS):
        out.append('## ' + name)
        if si == 1:
            out.append('### 子类:台面操作')          # 非条目行,测逐字搬运
        for k in range(1, counts[si] + 1):
            out.append(fake_entry(si, k))
            if si in (1, 3) and k % 7 == 0:
                out.append('  - 补充:第 %d 条的例外情形见同目录说明' % k)   # 缩进子项,不算条目
        out.append('')
    return '\n'.join(out)


def make_fake_index(target=30000):
    order = [si for si, (_, w) in enumerate(FAKE_SECTIONS) for _ in range(w)]
    counts = [0] * len(FAKE_SECTIONS)
    step = 0
    while True:
        text = render_fake(counts)
        if len(text) >= target:
            return text
        counts[order[step % len(order)]] += 1
        step += 1


def prepare_out_dir(d):
    if os.path.isdir(d):
        names = os.listdir(d)
        if names and SELFTEST_MARKER not in names:
            raise FoldError('%s 不是空目录,也没有自测标记 %s;换一个目录' % (d, SELFTEST_MARKER))
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                os.remove(p)
    else:
        os.makedirs(d)
    write_plain(os.path.join(d, SELFTEST_MARKER), '')


def selftest(out_dir, date):
    if out_dir:
        out_dir = os.path.abspath(out_dir)
        prepare_out_dir(out_dir)
    else:
        out_dir = tempfile.mkdtemp(prefix='fold_selftest_')
        write_plain(os.path.join(out_dir, SELFTEST_MARKER), '')
    index = os.path.join(out_dir, 'MEMORY.md')
    before = make_fake_index()
    write_plain(index, before)
    write_plain(os.path.join(out_dir, 'MEMORY.折前.md'), before)
    b_lines = to_lines(before)[0]
    n_sec = len(parse_sections(b_lines))
    print('自测: 生成假索引 %s(%d 字符,%d 节,%d 条)' % (index, len(before), n_sec, count_entries(b_lines)))

    rep, res = run_fold(index, [], [], True, DEFAULT_LIMIT, date, False)
    for ln in rep:
        print(ln)

    # 独立断言:只读盘上文件重算,不信 run_fold 自己的数
    after = read_text(index)
    a_lines = to_lines(after)[0]
    sec_texts = {}
    for r in res.records:
        sec_texts[r.filename] = read_text(os.path.join(out_dir, r.filename))
    moved = sum(count_entries(to_lines(t)[0]) for t in sec_texts.values())
    added = len(res.records)
    restored = list(a_lines)
    for r in res.records:
        body = sec_texts[r.filename].split('\n\n', 3)[3]      # 跳过 frontmatter、标题、说明行
        block = to_lines(body)[0]
        idx = [i for i, ln in enumerate(restored)
               if ln.startswith('- [%s入口](%s)' % (r.name, r.filename))]
        if len(idx) == 1:                                     # 找不到或多于一处:不还原,下面必判失败
            restored = restored[:idx[0]] + block + restored[idx[0] + 1:]
    checks = [
        ('假索引约 30,000 字符且 ≥8 节', 29000 <= len(before) <= 31000 and n_sec >= 8,
         '%d 字符 / %d 节' % (len(before), n_sec)),
        ('折后 ≤ %d 字符' % DEFAULT_LIMIT, len(after) <= DEFAULT_LIMIT, '%d 字符' % len(after)),
        ('条目守恒(按盘上文件重数)',
         count_entries(b_lines) == count_entries(a_lines) - added + moved,
         '%d = %d - %d + %d' % (count_entries(b_lines), count_entries(a_lines), added, moved)),
        ('入口行换回二级入口正文 == 折前全文', restored == b_lines, '%d 行' % len(b_lines)),
        ('二级入口带 frontmatter 且 type=reference',
         all(t.startswith('---\nname: ') and '\nmetadata:\n  type: reference\n---\n' in t
             for t in sec_texts.values()), '%d 个文件' % len(sec_texts)),
        ('报告末行 == 文件真实末行', rep[-1] == '末行: ' + last_line(after), repr(last_line(after))),
        ('slug 规则', make_slug('Tooling Notes') == 'tooling-notes'
         and make_slug('代码与仓库') == make_slug('代码与仓库')
         and make_slug('代码与仓库').startswith('section-'), make_slug('代码与仓库')),
    ]
    ok = True
    print('== 自测断言 ==')
    for name, passed, detail in checks:
        ok = ok and passed
        print('%s %s: %s' % ('通过' if passed else '失败', name, detail))
    print('自测结果: %s' % ('全部通过' if ok else '有失败'))
    print('折后索引: %s' % index)
    print('折前原件: %s' % os.path.join(out_dir, 'MEMORY.折前.md'))
    print('对照命令: tail -1 "%s"' % index)
    print('末行: %s' % last_line(after))
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

def main(argv=None):
    setup_stdio()
    p = argparse.ArgumentParser(
        prog='fold_index.py',
        description='把记忆索引超限的整节原样折成二级入口(规则见 desk/memory/规范.md §5)。')
    p.add_argument('index', nargs='?', help='索引文件路径(通常是 MEMORY.md)')
    p.add_argument('--section', action='append', default=[], metavar='节名',
                   help='要折的节名("## " 后面的文字),可重复')
    p.add_argument('--slug', action='append', default=[], metavar='slug',
                   help='与 --section 按先后配对的二级入口名,生成 <slug>-index.md;可重复')
    p.add_argument('--auto', action='store_true', help='从最大节起逐节折,直到 ≤ limit')
    p.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='字符上限,默认 24000')
    p.add_argument('--date', default=None, help='入口行里的日期 YYYY-MM-DD,默认今天')
    p.add_argument('--dry-run', action='store_true', help='只算、只核、只报告,不写任何文件')
    p.add_argument('--selftest', action='store_true', help='生成约 30,000 字符的假索引,跑一次 --auto 并断言')
    p.add_argument('--out', default=None, help='--selftest 的输出目录(缺省建临时目录)')
    args = p.parse_args(argv)

    date = args.date or datetime.date.today().isoformat()
    if not DATE_RE.match(date):
        p.error('--date 要写成 YYYY-MM-DD')
    if args.limit <= 0:
        p.error('--limit 必须是正整数')

    try:
        if args.selftest:
            if args.index or args.section or args.slug or args.auto or args.dry_run:
                p.error('--selftest 只能配 --out/--date')
            return selftest(args.out, date)
        if not args.index:
            p.error('缺索引文件路径')
        if not args.section and not args.auto:
            p.error('至少给一个 --section,或加 --auto')
        if len(args.slug) > len(args.section):
            p.error('--slug 只能与 --section 按先后配对,个数不能多于 --section')
        rep, _ = run_fold(args.index, args.section, args.slug, args.auto, args.limit, date, args.dry_run)
        for ln in rep:
            print(ln)
        return 0
    except FoldError as e:
        print('核对失败: %s' % e, file=sys.stderr)
        print('未写任何文件(若上一行列出了已替换的文件,以上一行为准)。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
