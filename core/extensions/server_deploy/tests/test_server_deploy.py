"""扩展 server_deploy:把工单台自己装到服务器上的脚本(install/update/pack/backup)。

这些用例原来在核心用例里(DeployScriptTests、DeployGateTests、ReviewInParallelTests 的 test_12b/12c),
随脚本一起搬进扩展目录。★本扩展没有 Python 钩子,用例开头仍显式打开它一次:确认它能被按名加载。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.tickets import extension_loader

HERE = Path(__file__).resolve().parents[1]
EXTENSION = "server_deploy"
# 只要这几个变量之一漏进测试子进程,CLI 就可能连上真服务器。
CHANNEL_VARIABLES = ("TICKET_REMOTE", "TICKET_TOKEN_FILE", "TICKET_CA_SHA256", "TICKET_ALLOW_STALE", "TICKET_ENV")


def clean_environment(tickets_root: Path | str, **extra: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in CHANNEL_VARIABLES}
    environment["TICKET_DESK_ROOT"] = str(tickets_root)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


def setUpModule() -> None:
    extension_loader.activate(EXTENSION)


def tearDownModule() -> None:
    extension_loader.deactivate(EXTENSION)


class DeployScriptTests(unittest.TestCase):
    def test_install_is_scoped_and_keeps_state_outside_app(self):
        script = (HERE / "install.sh").read_text(encoding="utf-8")
        # 安装目录只能落在安装根(默认 /srv)下面的独立子目录;账户名与服务名是变量,默认通用名。
        self.assertIn('INSTALL_ROOT="${INSTALL_ROOT:-/srv}"', script)
        self.assertIn('[[ "$INSTALL_DIR" == "$INSTALL_ROOT"/* && "$INSTALL_DIR" != "$INSTALL_ROOT" ]]', script)
        self.assertIn('SERVICE_NAME="${SERVICE_NAME:-ticket-desk}"', script)
        self.assertIn('SERVICE_USER="${SERVICE_USER:-ticket-desk}"', script)
        self.assertIn('/etc/systemd/system/$SERVICE_NAME.service', script)
        self.assertIn("--reopen-setup", script)
        self.assertIn("--rotate-token", script)
        self.assertIn("rsa:2048", script)
        self.assertIn('systemctl restart "$SERVICE_NAME.service"', script)
        self.assertNotIn("/opt/", script)
        self.assertNotIn("3306", script)
        self.assertNotIn("8080", script)

    def test_update_only_replaces_app_and_backup_keeps_fourteen_days(self):
        update = (HERE / "update.sh").read_text(encoding="utf-8")
        backup = (HERE / "backup.sh").read_text(encoding="utf-8")
        self.assertNotIn("$INSTALL_DIR/db", update)
        self.assertNotIn("$INSTALL_DIR/img", update)
        self.assertIn("0 3 * * *", backup)
        self.assertIn("-mtime +13", backup)
        self.assertIn("ticket.py\" dump", backup)

    def test_no_script_hardcodes_an_install_path_or_account(self):
        """四个脚本里写死的账户名、服务名、安装目录都换成了变量,默认值是通用名 ticket-desk。"""
        for name in ("install.sh", "update.sh", "pack.sh", "backup.sh"):
            text = (HERE / name).read_text(encoding="utf-8")
            with self.subTest(脚本=name):
                self.assertNotRegex(text, r"/srv/[A-Za-z]", "安装目录要走 $INSTALL_ROOT/$INSTALL_DIR,不许写死")
        for name in ("install.sh", "update.sh"):
            text = (HERE / name).read_text(encoding="utf-8")
            with self.subTest(默认名=name):
                self.assertIn('SERVICE_NAME="${SERVICE_NAME:-ticket-desk}"', text)
                self.assertIn('SERVICE_USER="${SERVICE_USER:-ticket-desk}"', text)



@unittest.skipUnless(shutil.which("bash"), "本机没有 bash,跑不了 update.sh 的闸")
class DeployGateTests(unittest.TestCase):
    """update.sh 的三道前置闸,真跑脚本验,不是 grep 源码字符串。

    2026-09-05 两次事故都是「上服前没有硬闸」:线上 python 没装 Pillow(探针只 grep 源码,
    没探过运行环境);pytest 退出码被 `| tail -1` 吞成绿,带红上服两回。
    这些闸只有真被拦过一次才算数,所以这里用打桩的 python3/node 把每一道单独按红。
    """

    UPDATE = HERE / "update.sh"

    PYTHON_STUB = """#!/usr/bin/env bash
# 打桩的 python3:各条探针的退出码由环境变量控制,好让三道闸能被单独按红。
if [[ "${1:-}" == "-" ]]; then cat >/dev/null; exit 0; fi   # 版本检查那段 heredoc
if [[ "${1:-}" == "-c" ]]; then
  case "${2:-}" in
    *"from PIL"*) exit "${STUB_PIL_RC:-0}" ;;
    *"import pytest"*) exit "${STUB_PYTEST_INSTALLED_RC:-0}" ;;
  esac
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "pytest" ]]; then
  # 闸②到底拿哪些参数跑的 pytest,记下来给 test_6b4 当证据(grep 源码证明不了真传了)。
  if [[ -n "${STUB_ARGV_LOG:-}" ]]; then printf '%s\n' "$@" > "$STUB_ARGV_LOG"; fi
  echo "${STUB_PYTEST_OUTPUT:-243 passed in 1.00s}"
  exit "${STUB_PYTEST_RC:-0}"
fi
exit 0
"""

    NODE_STUB = """#!/usr/bin/env bash
if [[ "${1:-}" == "--check" ]]; then
  if [[ "${STUB_NODE_RC:-0}" != "0" ]]; then
    echo "SyntaxError: Unexpected token in $2" >&2
  fi
  exit "${STUB_NODE_RC:-0}"
fi
exit 0
"""

    @staticmethod
    def posix(path: Path) -> str:
        return str(path).replace("\\", "/")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "src"
        (source / "tools" / "tickets" / "tests").mkdir(parents=True)
        (source / "tools" / "browser").mkdir(parents=True)
        (source / "tools" / "browser" / "tickets.js").write_text("const a = 1;\n", encoding="utf-8")
        self.source = source
        stubs = self.root / "stubs"
        stubs.mkdir()
        self.python_stub = stubs / "python3"
        self.python_stub.write_text(self.PYTHON_STUB, encoding="utf-8", newline="\n")
        self.node_stub = stubs / "node"
        self.node_stub.write_text(self.NODE_STUB, encoding="utf-8", newline="\n")
        for stub in (self.python_stub, self.node_stub):
            stub.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_update(self, *arguments: str, **stub: str) -> subprocess.CompletedProcess:
        # 起子进程一律从 clean_environment 派生:这条链一路上不该有任何通向真服务器的变量。
        overrides = {key: str(value) for key, value in stub.items()}
        overrides.setdefault("PYTHON_BIN", self.posix(self.python_stub))
        overrides.setdefault("NODE_BIN", self.posix(self.node_stub))
        # ★SERVICE_USER 必须指向一个一定不存在的账号。
        #   闸① 在 `command -v sudo` 且 `id "$SERVICE_USER"` 都成立时,会拿服务账号再 import 一次 PIL;
        #   服务器上 sudo 和服务账号都真在,于是它去 sudo 跑本用例摆在临时目录里的打桩解释器,
        #   服务账号读不到那个临时目录 → 闸① 直接拦 → stdout 全空、退 2,
        #   下面这五条(6b/6b2/6c/6c2/7a)全部红在「本该走到闸②③」的地方。本机没有 sudo,所以从没露过。
        #   固定成不存在的账号,这几条在哪台机器上都只验它们各自那道闸;
        #   服务账号那一支由 test_6a2/test_6a3 用打桩的 sudo/id 单独钉。
        overrides.setdefault("SERVICE_USER", f"没有这个服务账号-{os.getpid()}")
        environment = clean_environment(self.root / "tickets", **overrides)
        return subprocess.run(
            ["bash", self.posix(self.UPDATE), "--source", self.posix(self.source), *arguments],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )

    def test_6a_missing_pillow_is_blocked_before_anything_is_touched(self):
        """⑥-① 服务端 import 不到 Pillow 就拦:这一条就是 16 张带图 live 全被拦的那次。"""
        done = self.run_update("--check-only", STUB_PIL_RC="1")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 拦", done.stderr)
        self.assertIn("apt install python3-pil", done.stderr)
        self.assertNotIn("前置闸 ②", done.stdout)      # 第一道就停,不往下走
        self.assertNotIn("开始替换", done.stdout)

    def service_account_overrides(self, sudo_rc: int) -> dict[str, str]:
        """造一棵「服务器上的样子」:sudo 在、服务账号也在,sudo -n 那一趟的退出码由参数定。

        本机(开发机)既没有 sudo 也没有服务账号,闸① 永远走「那一次没跑」的分支;
        服务器上两样都真在,走的是另一支——2026-09-06 服务端 5 条红就出在这个分支差异上。
        用打桩的 sudo/id 把这一支在任何机器上都钉住。
        """
        stubs = self.root / f"pathstubs-{sudo_rc}"
        stubs.mkdir()
        (stubs / "sudo").write_text(f"#!/usr/bin/env bash\nexit {sudo_rc}\n", encoding="utf-8", newline="\n")
        (stubs / "id").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        for stub in (stubs / "sudo", stubs / "id"):
            stub.chmod(0o755)
        return {
            "PATH": self.posix(stubs) + os.pathsep + os.environ.get("PATH", ""),
            "SERVICE_USER": "ticket-desk",
        }

    def test_6a2_service_account_that_cannot_import_pillow_is_blocked(self):
        """⑥-① 当前用户能 import、服务账号不能,照样拦——线上跑服务的是服务账号。"""
        done = self.run_update("--check-only", **self.service_account_overrides(1))
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 拦", done.stderr)
        self.assertIn("服务账号 ticket-desk 不能", done.stderr)
        self.assertEqual("", done.stdout.strip())     # 拦在第一道,后两道一个字都不打

    def test_6a3_service_account_that_can_import_pillow_passes_and_is_named(self):
        """服务账号那一趟过了,过语里要点出它真跑过——否则没人分得清是过了还是没跑。"""
        done = self.run_update("--check-only", **self.service_account_overrides(0))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 过", done.stdout)
        self.assertIn("服务账号 ticket-desk 也可 import", done.stdout)
        self.assertIn("三闸全过；--check-only", done.stdout)

    def test_6b_red_pytest_is_blocked_with_the_real_exit_code(self):
        """⑥-② pytest 红就拦,而且拦语里带的是 pytest 自己的退出码,不是管道末节的 0。"""
        done = self.run_update("--check-only", STUB_PYTEST_RC="1", STUB_PYTEST_OUTPUT="1 failed, 242 passed")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 过", done.stdout)
        self.assertIn("前置闸 ② pytest … 拦（pytest 退出码 1）", done.stderr)
        self.assertIn("1 failed, 242 passed", done.stderr)  # 现场也打出来,不用再跑一遍
        self.assertNotIn("开始替换", done.stdout)

    def test_6b2_missing_pytest_is_named_not_silently_skipped(self):
        """没装 pytest 时必须明说,并指出 --skip-tests 才能跳——静默跳过正是带红上服的温床。"""
        done = self.run_update("--check-only", STUB_PYTEST_INSTALLED_RC="1")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("服务器没装 pytest,--skip-tests 才能跳过", done.stderr)
        skipped = self.run_update("--check-only", "--skip-tests", STUB_PYTEST_INSTALLED_RC="1")
        self.assertEqual(0, skipped.returncode, skipped.stdout + skipped.stderr)
        self.assertIn("前置闸 ② pytest … 跳过", skipped.stdout)

    def test_6b3_too_many_skips_is_treated_as_not_run_and_blocked(self):
        """跳过条数超过上限就按拦处理:pytest 自己退 0,闸照样不放——跳这么多等于没跑。

        R1 把「读 tools/ 以外文件」的用例改成了干净跳过;要是没有这道上限,
        哪天跳光了闸还是一路绿灯,今天这个坑(闸看起来在,其实从没真跑过)就换个形状再来一次。
        """
        listing = (
            "SKIPPED [1] tools/tickets/tests/test_ticket_system.py:1: "
            "上服包只含 tools/tickets 与 tools/browser 两个目录,读不到 review/ticket-system/MOVE-VERIFY.md\n"
            "SKIPPED [15] tools/tickets/tests/test_ticket_system.py:2: 另外十五条同理\n"
            "264 passed, 16 skipped in 1.00s"
        )
        done = self.run_update("--check-only", STUB_PYTEST_OUTPUT=listing)
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ② pytest … 拦（跳过 16 条,超过上限 15", done.stderr)
        self.assertIn("MOVE-VERIFY.md", done.stderr)          # 清单要真列出来,不能只报个数
        self.assertIn("另外十五条同理", done.stderr)
        self.assertNotIn("前置闸 ③", done.stdout)             # 拦在闸②,不往下走
        self.assertNotIn("开始替换", done.stdout)

    def test_6b3b_skips_at_the_limit_still_pass_and_the_count_is_printed(self):
        """上限之内照常放行,而且过语里必须报出跳了几条——不报数就等于没人看得见跳过。"""
        done = self.run_update("--check-only", STUB_PYTEST_OUTPUT="265 passed, 15 skipped in 1.00s")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ② pytest … 过（265 passed, 15 skipped in 1.00s;跳过 15 条,上限 15）", done.stdout)

    def test_6b4_gate_two_keeps_its_pytest_cache_out_of_the_package(self):
        """闸②跑 pytest 时缓存 provider 直接关掉,不在包目录里落 .pytest_cache。

        用 sudo 跑过一次之后,包里会留下 root 属主的 .pytest_cache,下一个普通账号写不进去,
        pytest 只打一条 PytestCacheWarning 就过去了——闸看着在跑,其实已经被自己上一趟的产物半瞎。
        这里不 grep 源码,而是把闸真传给 pytest 的 argv 记下来看。
        """
        log = self.root / "pytest-argv.txt"
        done = self.run_update("--check-only", STUB_ARGV_LOG=self.posix(log))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        argv = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(["-m", "pytest", "tools/tickets/tests", "-q", "-rs", "-p", "no:cacheprovider"], argv)
        self.assertFalse((self.source / ".pytest_cache").exists(), "闸②不许在包目录里留缓存")
        self.assertNotIn("cache_dir", self.UPDATE.read_text(encoding="utf-8"))

    def test_6c_broken_tickets_js_is_blocked(self):
        """⑥-③ tickets.js 语法坏了就拦:浏览器直接读它,没人替它编译。"""
        done = self.run_update("--check-only", STUB_NODE_RC="1")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ② pytest … 过", done.stdout)
        self.assertIn("前置闸 ③ node … 拦", done.stderr)
        self.assertNotIn("开始替换", done.stdout)

    def test_6c2_missing_node_is_named_and_skippable(self):
        done = self.run_update("--check-only", NODE_BIN=self.posix(self.root / "没有这个 node"))
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("服务器没装 node,--skip-node-check 才能跳过", done.stderr)
        skipped = self.run_update(
            "--check-only", "--skip-node-check", NODE_BIN=self.posix(self.root / "没有这个 node"),
        )
        self.assertEqual(0, skipped.returncode, skipped.stdout + skipped.stderr)
        self.assertIn("前置闸 ③ node … 跳过", skipped.stdout)

    def test_7a_all_three_pass_then_check_only_stops_before_replacing(self):
        """⑦ 三闸全过才有「三闸全过」这一行;--check-only 到此为止,一个字节都不动线上。"""
        done = self.run_update("--check-only")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 过", done.stdout)
        self.assertIn("前置闸 ② pytest … 过（243 passed in 1.00s;跳过 0 条,上限 15）", done.stdout)
        self.assertIn("前置闸 ③ node … 过", done.stdout)
        self.assertIn("三闸全过；--check-only", done.stdout)
        self.assertNotIn("开始替换", done.stdout)

    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "root 下会真去动 /srv,不跑这一条")
    def test_7b_replacement_phase_is_only_reached_after_all_three_gates(self):
        """⑦ 不加 --check-only 时,三闸全过之后才走到替换段——到那里因为不是 root 而失败,
        正好证明「先过闸、后 cp」这个次序是真的,而不是只写在注释里。"""
        install_dir = f"/srv/ticket-desk-gate-{os.getpid()}"
        done = self.run_update("--install-dir", install_dir)
        self.assertIn("三闸全过,开始替换。", done.stdout)
        self.assertNotEqual(0, done.returncode)          # 替换段本身在本机跑不通,这是预期的
        self.assertNotIn("前置闸", done.stderr)          # 失败不在任何一道闸上
        subprocess.run(["bash", "-c", f"rm -rf '{install_dir}'"], capture_output=True)

    def test_7c_bad_source_is_still_refused_before_the_gates(self):
        environment = clean_environment(self.root / "tickets", PYTHON_BIN=self.posix(self.python_stub))
        done = subprocess.run(
            ["bash", self.posix(self.UPDATE), "--source", self.posix(self.root / "不存在"), "--check-only"],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        self.assertEqual(2, done.returncode)
        self.assertIn("必须用 --source 指向工单台源码根目录", done.stderr)
        self.assertNotIn("前置闸", done.stdout)

    def test_7d_post_check_watches_the_port_not_a_log_line(self):
        """后置闸判的是「服务端口现在在听」;journal 是追加的,旧 READY 行照样会被 grep 到。"""
        script = self.UPDATE.read_text(encoding="utf-8")
        self.assertIn("ss -ltn", script)
        self.assertIn('grep -q ":$DESK_PORT "', script)
        self.assertIn('DESK_PORT="${DESK_PORT:-8443}"', script)
        self.assertIn('journalctl -u "$SERVICE_NAME.service" -n 20 --no-pager', script)
        self.assertIn("exit 3", script)
        self.assertNotIn("grep -q READY", script)
        # 退出码必须直接接在 pytest 那条命令后面,不能经过管道
        self.assertIn("TEST_RC=$?", script)
        self.assertIn("set -euo pipefail", script)
        self.assertNotIn("pytest tools/tickets/tests -q | tail", script)

class DeployRecordOrderTests(unittest.TestCase):
    """update.sh 自动建上服记录的次序,以及 pack.sh 把部署头写进包里(原核心用例 test_12b/12c)。"""

    def test_12b_the_script_only_records_after_the_port_gate(self):
        """★上服记录必须建在**后置闸之后**:它的意思是「线上真跑起来了」,不是「脚本走到这一行」。

        放到闸前面就成了纸糊的记录——端口没起来时脚本 exit 3,本来就走不到这里;
        真把它挪到前面,一次失败的上服也会留下一张「已上服」的单,而值面还会被写上新头。
        ★这一条只能钉源码顺序:update.sh 是 shell,没法在 pytest 里真跑一遍部署。
          局限写在这里,别把它当成行为验证——真判据是上服后线上那几条探针。
        """
        script = (HERE / "update.sh").read_text(encoding="utf-8")
        gate = script.index('ss -ltn 2>/dev/null | grep -q ":$DESK_PORT "')
        record = script.index('"op":"deploy-record"')
        self.assertLess(gate, record, "上服记录那段跑到端口后置闸前面去了")
        self.assertIn("后置检查:$DESK_PORT 已在听。", script[:record])
        # 建单失败不能让整条上服失败:代码已替换、服务已起来,这时候退非零会让人以为上服没成
        self.assertIn("不影响本次上服", script[record:])

    def test_12c_the_packer_puts_the_head_into_the_package(self):
        """★第一次真上服撞到的:`git archive` 打的包**没有 .git**,
        于是 update.sh 那句 `git rev-parse` 永远拿不到部署头,
        「READY 之后自动建上服记录」这条路**每次都走兜底、从不生效**。

        根治只有一条:打包那一刻就把提交号放进包里(pack.sh),update.sh 优先读它。
        ★这里连「pack.sh 自己要回验」也一起钉:那个脚本的第一版用了一个
          根本不存在的 git 选项、错误被 2>/dev/null 吞掉,却照样打印「已写进包内」——
          报成功、没做成、没人发现,正是本仓反复栽的那个形状。
        """
        pack = HERE / "pack.sh"
        update = HERE / "update.sh"
        pack_text = pack.read_text(encoding="utf-8")
        update_text = update.read_text(encoding="utf-8")
        # 打包器把头写进包内固定位置
        self.assertIn("--add-virtual-file=", pack_text)
        self.assertIn("extensions/server_deploy/DEPLOY_HEAD", pack_text)
        # ★打完要回验,而且验不过要**删掉半成品**——不能把一个没写进头的包留在盘上让人拿去上服
        self.assertIn('PACKED="$(tar xOf "$OUT" extensions/server_deploy/DEPLOY_HEAD', pack_text)
        self.assertIn('if [[ "$PACKED" != "$HEAD_SHORT" ]]', pack_text)
        self.assertIn('rm -f "$OUT"', pack_text)
        # 工作树脏要拦:打的是提交,未提交的改动进不了包,不拦就会「上服了但没生效」
        self.assertIn("有未提交的改动", pack_text)
        # update.sh 那边优先读包里的文件,git 只是最后兜底
        self.assertIn("$SOURCE/extensions/server_deploy/DEPLOY_HEAD", update_text)
        self.assertLess(update_text.index("$SOURCE/extensions/server_deploy/DEPLOY_HEAD"),
                        update_text.index("git rev-parse --short=9 HEAD"),
                        "包内 DEPLOY_HEAD 必须排在 git rev-parse 之前——后者在上服包里永远拿不到")
        # ★令牌也要自己找,靠调用者传第二个路径 = 每次上服都可能因为记错而少建一张记录
        #   (2026-09-08 实撞两次)。★但只能**从 systemd 的 --token-file 现读**,不许写死路径:
        #   写死就会出现指向数据库目录的字面,而本脚本一个字都不许碰它——
        #   那道闸(下面 test_update_only_replaces_app…)防的是「上服脚本误删数据库」,
        #   为了读个令牌把它放宽是本末倒置。
        self.assertIn("--token-file", update_text)
        self.assertNotIn("$INSTALL_DIR/db", update_text)
        self.assertLess(update_text.index("--token-file"),
                        update_text.index('if [[ -n "${TICKET_TOKEN:-}" ]]; then'),
                        "自己找令牌那一段必须排在用它之前")


if __name__ == "__main__":
    unittest.main()
