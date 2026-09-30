"""Hand-counted fixture repositories; no network or upstream repositories."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "repo_scale.py"
SPEC = importlib.util.spec_from_file_location("repo_scale", SCRIPT)
scale = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scale
SPEC.loader.exec_module(scale)

START = "2026-01-02T00:00:00Z"
END = "2026-01-05T00:00:00Z"


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="repo scale 测试 ")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Scale Test")
        self.git("config", "user.email", "scale@example.invalid")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args, input=None, date=None):
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       input=input, env=env, stderr=subprocess.PIPE)

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def commit(self, date="2026-01-03T12:00:00Z"):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "fixture", date=date)
        return self.git("rev-parse", "HEAD").strip().decode()

    def config(self, **updates):
        config = scale.load_config()
        config.update(updates)
        return config

    def analyze(self, **kwargs):
        return scale.analyze(self.repo, kwargs.pop("ref", "HEAD"),
                             kwargs.pop("start", START), kwargs.pop("end", END),
                             self.config(**kwargs))

    def test_hand_counted_churn_tree_exclusion_and_test_separation(self):
        self.write("src/main.py", b"one\ntwo\n")
        self.write("tests/test_main.py", b"check\n")
        self.write("build/generated.py", b"ignore\nignore\nignore\n")
        self.write("notes.md", b"hello\n")
        self.commit()
        self.write("src/main.py", b"one\nthree\nfour")
        self.write("tests/test_main.py", b"check\nmore\n")
        self.write("build/generated.py", b"new\n")
        self.commit()
        report = self.analyze(exclude_path_globs=["build/**"], test_path_globs=["**/tests/**"])
        self.assertEqual(report.commits, 2)
        code, test = report.buckets["non_test"], report.buckets["test"]
        self.assertEqual((code.added, code.deleted, code.lines, code.text_files), (5, 1, 4, 2))
        self.assertEqual((test.added, test.deleted, test.lines, test.text_files), (2, 0, 2, 1))
        self.assertEqual(report.excluded_files, 1)

    def test_out_of_order_dates_and_exact_half_open_boundaries(self):
        self.write("a", b"one\n")
        self.commit("2026-01-02T00:00:00Z")  # included lower boundary
        self.commit("2026-01-01T00:00:00Z")  # old child must not hide its parent
        self.commit("2026-01-04T23:59:59Z")
        self.commit("2026-01-05T00:00:00Z")  # excluded upper boundary
        report = self.analyze()
        self.assertEqual(report.commits, 2)
        self.assertEqual(report.buckets["non_test"].added, 1)
        equivalent = self.analyze(start="2026-01-01T17:00:00-07:00",
                                  end="2026-01-04T17:00:00-07:00")
        self.assertEqual(scale.render(report), scale.render(equivalent))

    def test_rename_spaces_unicode_and_cross_category(self):
        original = "源 目录/original name.py"
        self.write(original, b"".join(("line %d\n" % i).encode() for i in range(20)))
        self.commit("2026-01-01T00:00:00Z")
        target = self.repo / "tests" / "renamed file.py"
        target.parent.mkdir()
        (self.repo / original).rename(target)
        self.commit()
        pure = self.analyze(test_path_globs=["tests/**"])
        self.assertEqual((pure.buckets["test"].added, pure.buckets["test"].deleted), (0, 0))
        self.assertEqual(pure.buckets["test"].lines, 20)
        moved = self.repo / "new name.py"
        target.rename(moved)
        moved.write_bytes(moved.read_bytes().replace(b"line 0\n", b"changed\n"))
        self.commit()
        report = self.analyze(test_path_globs=["tests/**"])
        self.assertEqual((report.buckets["non_test"].added, report.buckets["non_test"].deleted), (1, 0))
        self.assertEqual((report.buckets["test"].added, report.buckets["test"].deleted), (0, 1))

    def test_binary_undecodable_crlf_empty_and_no_final_newline(self):
        self.write("binary.dat", b"hello\0world\n")
        self.write("raw.dat", b"\xff\n\xfe")
        self.write("crlf.txt", b"a\r\n\r\n")
        self.write("empty.txt", b"")
        self.write("tail.txt", b"tail")
        self.commit()
        row = self.analyze().buckets["non_test"]
        self.assertEqual((row.lines, row.text_files, row.binary_files, row.binary_changes), (5, 4, 1, 1))
        self.assertEqual((row.added, row.deleted), (5, 0))

    def test_empty_window_fixed_ref_and_reproducible_cli(self):
        self.write("first.txt", b"first\n")
        fixed = self.commit()
        self.write("second.txt", b"second\n")
        self.commit()
        report = self.analyze(ref=fixed, start="2027-01-01T00:00:00Z", end="2027-01-02T00:00:00Z")
        self.assertEqual((report.commits, report.buckets["non_test"].added, report.buckets["non_test"].lines), (0, 0, 1))
        command = [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--ref", fixed,
                   "--start", START, "--end", END]
        before = self.git("status", "--porcelain")
        first = subprocess.check_output(command)
        self.assertEqual(first, subprocess.check_output(command))
        self.assertNotIn(str(self.repo).encode(), first)
        self.assertEqual(before, self.git("status", "--porcelain"))
        self.assertIn(fixed.encode(), first)

    def test_unstaged_attributes_do_not_change_fixed_tree_results(self):
        self.write("file.txt", b"one\ntwo\n")
        self.commit()
        expected = scale.render(self.analyze())
        self.write(".gitattributes", b"*.txt -diff\n")
        self.assertEqual(scale.render(self.analyze()), expected)

    def test_local_attributes_drivers_and_inherited_git_environment_are_isolated(self):
        self.write("file.txt", b"one\ntwo\n")
        self.write(".gitattributes", b"*.txt diff=localdriver\n")
        self.commit()
        expected = scale.render(self.analyze())
        self.git("config", "diff.localdriver.binary", "true")
        self.git("config", "diff.localdriver.command", "must-not-run")
        self.assertEqual(scale.render(self.analyze()), expected)
        self.write(".git/info/attributes", b"*.txt -diff\n")
        before = {p.relative_to(self.repo): p.read_bytes()
                  for p in self.repo.rglob("*") if p.is_file()}
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.repo / "nonexistent"),
                                          "GIT_CONFIG_COUNT": "1",
                                          "GIT_CONFIG_KEY_0": "diff.localdriver.binary",
                                          "GIT_CONFIG_VALUE_0": "true"}):
            self.assertEqual(scale.render(self.analyze()), expected)
        after = {p.relative_to(self.repo): p.read_bytes()
                 for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_shallow_repository_is_explicitly_rejected(self):
        self.write("file", b"old\n")
        self.commit("2026-01-01T00:00:00Z")
        self.write("file", b"new\n")
        head = self.commit()
        # This is Git's actual shallow boundary metadata, not a mocked response.
        self.write(".git/shallow", (head + "\n").encode("ascii"))
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository").strip(), b"true")
        with self.assertRaisesRegex(scale.ScaleError, "shallow repositories"):
            self.analyze()

    def test_sha256_object_store_works_in_isolated_metadata(self):
        self.repo = self.repo / "sha256"
        self.repo.mkdir()
        self.git("init", "-q", "--object-format=sha256", "--initial-branch=main")
        self.git("config", "user.name", "Scale Test")
        self.git("config", "user.email", "scale@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("file", b"one\n")
        self.commit()
        report = self.analyze()
        self.assertEqual(len(report.commit), 64)
        self.assertEqual((report.commits, report.buckets["non_test"].added,
                          report.buckets["non_test"].lines), (1, 1, 1))

    def test_merge_policy_is_first_parent_when_enabled(self):
        self.write("base", b"base\n")
        self.commit()
        self.git("checkout", "-qb", "side")
        self.write("side", b"side\n")
        self.commit()
        self.git("checkout", "-q", "main")
        self.write("main", b"main\n")
        self.commit()
        self.git("merge", "--no-ff", "-qm", "merge", "side", date="2026-01-03T12:00:00Z")
        default = self.analyze()
        merged = self.analyze(include_merges=True)
        self.assertEqual((default.commits, default.buckets["non_test"].added), (3, 3))
        self.assertEqual((merged.commits, merged.buckets["non_test"].added), (4, 4))
        self.assertEqual(merged.buckets["non_test"].lines, 3)

    def test_symlink_gitlink_are_excluded_from_tree_and_numstat(self):
        self.write("regular.txt", b"one\ntwo\n")
        parent = self.commit("2026-01-01T00:00:00Z")
        target_oid = self.git("hash-object", "-w", "--stdin", input=b"regular.txt").strip().decode()
        self.git("update-index", "--add", "--cacheinfo", "120000," + target_oid + ",link")
        self.git("update-index", "--add", "--cacheinfo", "160000," + parent + ",submodule")
        self.git("commit", "-qm", "special entries", date="2026-01-03T12:00:00Z")
        report = self.analyze()
        row = report.buckets["non_test"]
        self.assertEqual((report.symlinks, report.gitlinks, row.lines), (1, 1, 2))
        self.assertEqual((row.added, row.deleted), (0, 0))

    def test_extension_rules_and_root_recursive_globs(self):
        for name in ("a.PY", "test_root.py", "dir/test_nested.py", "generated.py", "readme.md"):
            self.write(name, b"line\n")
        self.commit()
        report = self.analyze(text_extensions=[".py"], exclude_path_globs=["generated.py"],
                              test_path_globs=["**/test_*.py"])
        self.assertEqual(report.buckets["non_test"].lines, 1)
        self.assertEqual(report.buckets["test"].lines, 2)
        excluded = self.analyze(exclude_extensions=[".py"])
        self.assertEqual(excluded.buckets["non_test"].lines, 1)
        self.assertTrue(scale.glob_regex("a/**/b").fullmatch("a/b"))
        self.assertFalse(scale.glob_regex("a/*").fullmatch("a/b/c"))

    def test_bad_ref_empty_repository_dates_and_configuration(self):
        with self.assertRaises(scale.ScaleError):
            self.analyze()
        self.commit()
        with self.assertRaises(scale.ScaleError):
            self.analyze(ref="--all")
        with self.assertRaises(scale.ScaleError):
            self.analyze(start="2026-01-01")
        with self.assertRaises(scale.ScaleError):
            self.analyze(start=END, end=START)
        path = self.repo / "config.json"
        for bad in ({"unknown": []}, {"include_merges": 1}, {"test_path_globs": "tests/**"},
                    {"text_extensions": ["py"]}):
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(scale.ScaleError):
                scale.load_config(path)
        path.write_text(json.dumps({"exclude_extensions": [".PNG", ".png"]}), encoding="utf-8")
        self.assertEqual(scale.load_config(path)["exclude_extensions"], [".png"])


if __name__ == "__main__":
    unittest.main()
