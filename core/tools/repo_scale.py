#!/usr/bin/env python3
"""Reproducible Git scale report; Python standard library and Git only."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class ScaleError(Exception):
    """An expected input or Git failure, safe to present without a traceback."""


DEFAULT_CONFIG = {
    "exclude_path_globs": [],
    "exclude_extensions": [],
    "text_extensions": [],
    "test_path_globs": [],
    "include_merges": False,
}
REGULAR_MODES = {"100644", "100755"}


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScaleError("time must be ISO 8601 with an explicit UTC offset") from exc
    if result.tzinfo is None:
        raise ScaleError("time must include an explicit UTC offset")
    return result.astimezone(timezone.utc)


def load_config(path=None):
    config = {key: list(value) if isinstance(value, list) else value
              for key, value in DEFAULT_CONFIG.items()}
    if path is not None:
        try:
            supplied = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ScaleError("cannot read a valid UTF-8 JSON configuration") from exc
        if not isinstance(supplied, dict) or set(supplied) - set(config):
            raise ScaleError("configuration must be an object containing only documented keys")
        config.update(supplied)
    for key in config:
        if key == "include_merges":
            if type(config[key]) is not bool:
                raise ScaleError("include_merges must be a JSON boolean")
        elif (not isinstance(config[key], list)
              or any(not isinstance(item, str) or not item for item in config[key])):
            raise ScaleError(key + " must be an array of nonempty strings")
        elif key.endswith("extensions"):
            if any(not item.startswith(".") or "/" in item for item in config[key]):
                raise ScaleError(key + " entries must start with '.' and contain no '/'")
            config[key] = sorted(set(item.lower() for item in config[key]))
        else:
            config[key] = sorted(set(config[key]))
    return config


def glob_regex(pattern):
    """Path globs: * and ? stay in a component; **/ also matches zero dirs."""
    result = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                index += 1
                if pattern[index + 1:index + 2] == "/":
                    index += 1
                    result += "(?:.*/)?"
                else:
                    result += ".*"
            else:
                result += "[^/]*"
        elif char == "?":
            result += "[^/]"
        else:
            result += re.escape(char)
        index += 1
    return re.compile("^" + result + "$", re.DOTALL)


class Rules:
    def __init__(self, config):
        self.config = config
        self.excludes = [glob_regex(p) for p in config["exclude_path_globs"]]
        self.tests = [glob_regex(p) for p in config["test_path_globs"]]

    def included(self, path):
        lower = path.lower()
        return (not any(p.fullmatch(path) for p in self.excludes)
                and not any(lower.endswith(e) for e in self.config["exclude_extensions"])
                and (not self.config["text_extensions"]
                     or any(lower.endswith(e) for e in self.config["text_extensions"])))

    def bucket(self, path):
        return "test" if any(p.fullmatch(path) for p in self.tests) else "non_test"


class Git:
    def __init__(self, repo):
        # Absolute path prevents a repository argument from becoming a Git option.
        self.command = ["git", "--no-pager", "-C", str(Path(repo).resolve()),
                        "-c", "core.quotePath=false", "-c", "color.ui=false",
                        "-c", "diff.renames=true", "-c", "diff.renameLimit=1000",
                        "-c", "diff.algorithm=myers", "-c", "core.attributesFile=" + os.devnull]
        # Do this even for source-repository discovery: inherited GIT_DIR,
        # GIT_CONFIG_COUNT, alternates or lazy-fetch settings must not redirect it.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.upper().startswith("GIT_")}
        self.env.update(GIT_OPTIONAL_LOCKS="0", GIT_ATTR_NOSYSTEM="1",
                        GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")

    def run(self, args, **kwargs):
        try:
            result = subprocess.run(self.command + args, env=self.env,
                                    stderr=subprocess.PIPE, **kwargs)
        except OSError as exc:
            raise ScaleError("cannot start Git; install Git and check the repository") from exc
        if result.returncode:
            # Do not leak local repository paths or locale-dependent stderr into reports.
            raise ScaleError("Git " + args[0] + " failed (exit " + str(result.returncode) + ")")
        return result

    def capture(self, *args):
        return self.run(list(args), stdout=subprocess.PIPE).stdout


@contextmanager
def isolated_objects(source, commit):
    """Borrow source objects read-only without its config, attributes or refs."""
    if source.capture("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ScaleError("shallow repositories are unsupported; provide complete local history")
    object_format = source.capture("rev-parse", "--show-object-format").strip().decode("ascii")
    if object_format not in ("sha1", "sha256"):
        raise ScaleError("unsupported Git object format")
    objects = decode_path(source.capture("rev-parse", "--path-format=absolute",
                                         "--git-path", "objects").rstrip(b"\n"))
    # No git init and no writes anywhere under the source repository. All private
    # metadata lives in a temporary directory; the shared object store is read-only.
    with tempfile.TemporaryDirectory(prefix="repo-scale-") as temporary:
        directory = Path(temporary)
        (directory / "objects").mkdir()
        (directory / "refs").mkdir()
        (directory / "HEAD").write_text(commit + "\n", encoding="ascii")
        metadata = "[core]\nrepositoryformatversion = " + ("1" if object_format == "sha256" else "0") + "\nbare = true\n"
        if object_format == "sha256":
            metadata += "[extensions]\nobjectformat = sha256\n"
        (directory / "config").write_text(metadata, encoding="ascii")
        git = Git(directory)
        git.env["GIT_OBJECT_DIRECTORY"] = objects
        # Resolve attributes from the fixed tree. Explicit option rejects old Git.
        git.command.append("--attr-source=" + commit)
        yield git


@dataclass
class Counts:
    added: int = 0
    deleted: int = 0
    lines: int = 0
    text_files: int = 0
    binary_files: int = 0
    binary_changes: int = 0


@dataclass
class Report:
    commit: str
    start: datetime
    end: datetime
    config: dict
    commits: int = 0
    buckets: dict = field(default_factory=lambda: {"non_test": Counts(), "test": Counts()})
    excluded_files: int = 0
    symlinks: int = 0
    gitlinks: int = 0


def nul_tokens(stream):
    pending = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        pieces = (pending + chunk).split(b"\0")
        yield from pieces[:-1]
        pending = pieces[-1]
    if pending:
        raise ScaleError("unexpected non-NUL-terminated Git output")


def decode_path(value):
    # Git paths need not be UTF-8. Preserve undecodable bytes for deterministic matching.
    return value.decode("utf-8", errors="surrogateescape")


def count_changes(git, commits, rules, report):
    if not commits:
        return
    # File-backed stdin/stdout prevents pipe deadlock and unbounded diff-output RAM.
    with tempfile.TemporaryFile() as requests, tempfile.TemporaryFile() as output:
        requests.write("".join(commit + "\n" for commit in commits).encode("ascii"))
        requests.seek(0)
        args = ["log", "--no-walk=unsorted", "--stdin", "--root", "-r", "--raw", "--numstat",
                "-z", "--find-renames=50%", "--no-ext-diff", "--no-textconv",
                "--ignore-submodules=none", "--format=%H"]
        if report.config["include_merges"]:
            args.append("--diff-merges=first-parent")
        git.run(args, stdin=requests, stdout=output)
        output.seek(0)
        tokens = iter(nul_tokens(output))
        modes = {}
        for token in tokens:
            token = token.lstrip(b"\n")
            if not token:
                continue
            if token.startswith(b":"):
                old_mode, new_mode, _, _, status = token[1:].split(b" ")
                old_path = decode_path(next(tokens))
                new_path = decode_path(next(tokens)) if status[:1] in (b"R", b"C") else old_path
                modes[(old_path, new_path)] = (old_mode.decode(), new_mode.decode())
            elif b"\t" in token:
                added, deleted, path = token.split(b"\t", 2)
                if path:
                    old_path = new_path = decode_path(path)
                else:
                    old_path, new_path = decode_path(next(tokens)), decode_path(next(tokens))
                old_mode, new_mode = modes[(old_path, new_path)]
                old_ok = old_mode in REGULAR_MODES and rules.included(old_path)
                new_ok = new_mode in REGULAR_MODES and rules.included(new_path)
                if added == b"-" or deleted == b"-":
                    # A change touching both categories is reported once in each category.
                    buckets = set()
                    if old_ok:
                        buckets.add(rules.bucket(old_path))
                    if new_ok:
                        buckets.add(rules.bucket(new_path))
                    for bucket in buckets:
                        report.buckets[bucket].binary_changes += 1
                else:
                    if new_ok:
                        report.buckets[rules.bucket(new_path)].added += int(added)
                    if old_ok:
                        report.buckets[rules.bucket(old_path)].deleted += int(deleted)
            else:
                # Commit headers have no tab; paths are consumed only in their record states.
                modes.clear()


def read_blob(stream, size):
    remaining, lines, binary, last = size, 0, False, b""
    while remaining:
        chunk = stream.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise ScaleError("truncated cat-file blob")
        remaining -= len(chunk)
        lines += chunk.count(b"\n")
        binary = binary or b"\0" in chunk
        last = chunk[-1:]
    if stream.read(1) != b"\n":
        raise ScaleError("invalid cat-file blob separator")
    return (0 if binary else lines + int(size > 0 and last != b"\n"), binary)


def count_tree(git, commit, rules, report):
    cache = {}
    with tempfile.TemporaryFile() as listing, tempfile.TemporaryFile() as errors:
        git.run(["ls-tree", "-r", "-z", "--full-tree", commit], stdout=listing)
        listing.seek(0)
        try:
            process = subprocess.Popen(git.command + ["cat-file", "--batch"],
                                       env=git.env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=errors)
        except OSError as exc:
            raise ScaleError("cannot start Git cat-file") from exc
        try:
            for token in nul_tokens(listing):
                metadata, raw_path = token.split(b"\t", 1)
                mode, kind, oid = metadata.split(b" ")
                path = decode_path(raw_path)
                if not rules.included(path):
                    report.excluded_files += 1
                    continue
                if mode == b"120000":
                    report.symlinks += 1
                    continue
                if mode == b"160000":
                    report.gitlinks += 1
                    continue
                if mode.decode() not in REGULAR_MODES or kind != b"blob":
                    raise ScaleError("unexpected tree entry type")
                if oid not in cache:
                    # One outstanding request: read each blob fully before the next write.
                    process.stdin.write(oid + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if len(header) != 3 or header[1] != b"blob":
                        raise ScaleError("Git cat-file could not read a blob")
                    cache[oid] = read_blob(process.stdout, int(header[2]))
                lines, binary = cache[oid]
                bucket = report.buckets[rules.bucket(path)]
                bucket.lines += lines
                if binary:
                    bucket.binary_files += 1
                else:
                    bucket.text_files += 1
            process.stdin.close()
            if process.wait() != 0:
                raise ScaleError("Git cat-file failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


def analyze(repo, ref, start, end, config=None):
    start, end = parse_time(start), parse_time(end)
    if start >= end:
        raise ScaleError("start must be earlier than end")
    config = load_config() if config is None else config
    source = Git(repo)
    commit = source.capture("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip().decode("ascii")
    report = Report(commit, start, end, config)
    with isolated_objects(source, commit) as git:
        # Do not use --since: Git can prune ancestors with out-of-order commit dates.
        history = git.capture("log", "--format=%H %ct %P", commit, "--")
        selected = []
        for line in history.splitlines():
            parts = line.decode("ascii").split()
            if len(parts) > 3 and not config["include_merges"]:
                continue
            instant = datetime.fromtimestamp(int(parts[1]), timezone.utc)
            if start <= instant < end:
                selected.append(parts[0])
        report.commits = len(selected)
        rules = Rules(config)
        count_changes(git, selected, rules, report)
        count_tree(git, commit, rules, report)
    return report


def render(report):
    lines = ["# Git repository scale", "", "- Fixed commit: `" + report.commit + "`",
             "- Committer time window (UTC, start inclusive, end exclusive): `["
             + report.start.isoformat() + ", " + report.end.isoformat() + ")`",
             "- Merge policy: " + ("included; diff against first parent" if report.config["include_merges"]
                                       else "excluded from commit count and numstat"), "",
             "| Window commits | Added (numstat, total) | Deleted (numstat, total) | Fixed-tree net lines (total) |",
             "| ---: | ---: | ---: | ---: |",
             "| " + str(report.commits) + " | " + " | ".join(str(sum(getattr(row, attr) for row in report.buckets.values()))
                                                                  for attr in ("added", "deleted", "lines")) + " |", "",
             "| Category | Added (numstat) | Deleted (numstat) | Fixed-tree net lines | Text files | Binary files | Binary changes |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key, label in (("non_test", "Non-test"), ("test", "Test")):
        row = report.buckets[key]
        lines.append("| " + label + " | " + " | ".join(str(getattr(row, attr)) for attr in
                     ("added", "deleted", "lines", "text_files", "binary_files", "binary_changes")) + " |")
    lines.extend(["", "剔除项如下（完整生效配置）：", ""])
    for key, value in report.config.items():
        lines.append("- `" + key + "`: " + json.dumps(value, ensure_ascii=True))
    lines.extend(["", "- Tree entries excluded by path/extension rules: " + str(report.excluded_files),
                  "- Eligible symlinks skipped: " + str(report.symlinks)
                  + "; eligible gitlinks skipped: " + str(report.gitlinks), "",
                  "## Counting semantics", "",
                  "- Requires Python 3.9+ and Git supporting --attr-source; no pip packages. Git reads the source object store with GIT_OPTIONAL_LOCKS=0, lazy-fetch and replacement objects disabled. Private metadata is created only in a temporary directory; source repository metadata and working-tree contents are never modified.",
                  "- Commits are all commits reachable from the fixed ref with committer timestamps in the window, regardless of path filters. Empty commits count. An unborn/invalid ref is an error; a window with no commits has zero churn and still counts the fixed tree.",
                  "- Net lines are measured in the complete fixed tree, not additions minus deletions in the window. Regular-file bytes are counted as LF separators plus one final unterminated nonempty line; blank lines, comments, CRLF and undecodable non-NUL bytes are included. No encoding or language inference is used.",
                  "- Tree blobs containing any NUL byte are binary and contribute no lines. Numstat uses Git binary detection with attributes from the fixed tree, so its binary classification can differ; '-' changes are counted separately, never converted to estimated lines. Source info/attributes, repository diff-driver settings, inherited GIT_* settings and global/system configuration are isolated; external diff/textconv never runs.",
                  "- Shallow repositories are rejected. Missing objects fail locally without fetching; complete local reachable history and blobs are required. SHA-1 and SHA-256 repositories are supported.",
                  "- Symlinks and gitlinks contribute no lines or churn. Rename detection is 50% with an inexact-candidate limit of 1000; exact pure renames contribute 0/0. Above that limit Git can report unmatched files as deletions/additions. Added lines use the new path's rules/category and deleted lines the old path's rules/category. Binary changes touching both categories count once in each, so that column is not necessarily additive.",
                  "- When merges are enabled, reachable side-branch commits and first-parent merge diffs both count; integration churn may therefore be counted again at the merge.",
                  "- Path globs are case-sensitive, repository-relative with '/' separators: '*' and '?' do not cross '/', '**' crosses directories and '**/' may match zero directories; other characters are literal. Extension suffixes are case-insensitive. Empty text_extensions permits every extension; an empty test list classifies everything as non-test.",
                  "- Configuration rules apply to both churn and tree lines. Exclude rules win over text_extensions; test files are separated from non-test files. Invalid UTF-8 path bytes are preserved for matching; output contains no paths, current time, or commit messages.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Count reachable commits, Git numstat churn and exact fixed-tree lines using Python 3.9+ and Git with --attr-source support (no pip packages).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""JSON configuration (all keys optional):
  exclude_path_globs: array of repository-relative path globs (default [])
  exclude_extensions: array of suffixes such as '.png' (default [])
  text_extensions: allowed suffixes; [] permits all files (default [])
  test_path_globs: globs for the separate Test row (default [])
  include_merges: boolean; true uses first-parent merge diffs (default false)
Glob '*'/'?' stay within a directory; '**/' matches zero or more directories.
Extension matching ignores case. Other glob characters are literal.
The window is [start,end), uses committer time, and requires timezone offsets.
All reachable ancestors are checked even when commit dates are out of order.
Paths/refs are passed as arguments, never interpreted by a shell.
Example:
  python repo_scale.py --repo . --ref HEAD --start 2026-08-26T00:00:00-07:00 --end 2026-09-29T00:00:00-07:00 --config rules.json
An empty repository/invalid ref exits 2. No matching commits still counts the tree.
Shallow repositories are rejected; missing objects never trigger a lazy fetch.
Private temporary metadata isolates local attributes/config while borrowing objects read-only.
Output is UTF-8 Markdown on stdout; redirect it to save the report.""")
    parser.add_argument("--repo", required=True, help="Git working tree or bare repository path")
    parser.add_argument("--ref", required=True, help="commit/ref resolved once to a full commit ID")
    parser.add_argument("--start", required=True, help="inclusive ISO 8601 timestamp with timezone")
    parser.add_argument("--end", required=True, help="exclusive ISO 8601 timestamp with timezone")
    parser.add_argument("--config", help="UTF-8 JSON file with exclusion, text and test rules")
    args = parser.parse_args(argv)
    try:
        output = render(analyze(args.repo, args.ref, args.start, args.end, load_config(args.config)))
        sys.stdout.buffer.write(output.encode("utf-8"))
        return 0
    except (ScaleError, OSError) as exc:
        print("repo_scale: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
