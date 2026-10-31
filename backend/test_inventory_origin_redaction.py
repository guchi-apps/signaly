"""scripts/inventory-signaly-senders.sh が origin URL の資格情報を出力しないことを固定する（#365）。"""
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "inventory-signaly-senders.sh")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class InventoryOriginRedactionTest(unittest.TestCase):
    def _run(self, origin):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "app")
            os.mkdir(repo)
            _git("init", "-q", cwd=repo)
            _git("remote", "add", "origin", origin, cwd=repo)
            with open(os.path.join(repo, "notify.sh"), "w") as f:
                f.write("signaly-notify\n")
            _git("add", ".", cwd=repo)
            _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x", cwd=repo)
            _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
            r = subprocess.run(["bash", SCRIPT, root], capture_output=True, text=True, check=True)
            return r.stdout + r.stderr

    def test_non_github_userinfo_is_not_printed(self):
        out = self._run("https://user:SECRETTOKEN@gitlab.example/group/repo.git")
        self.assertNotIn("SECRETTOKEN", out)
        self.assertNotIn("user:", out)
        self.assertIn("gitlab.example/group/repo", out)

    def test_query_token_is_not_printed(self):
        out = self._run("https://gitlab.example/group/repo.git?token=SECRETTOKEN")
        self.assertNotIn("SECRETTOKEN", out)

    def test_unencoded_delimiters_in_password(self):
        for o in ("https://user:pa#SECRETX@gitlab.example/g/r.git",
                  "https://user:pa?SECRETX@gitlab.example/g/r.git",
                  "https://user:pa/SECRETX@gitlab.example/g/r.git",
                  "ssh://user:SECRETX@host.example:2222/g/r.git",
                  "user:SECRETX@host.example:g/r.git"):
            out = self._run(o)
            self.assertNotIn("SECRETX", out, o)
            self.assertNotIn("user", out.replace("対象", ""), o)

    def test_github_still_normalized(self):
        out = self._run("https://SECRETTOKEN@github.com/owner/repo.git")
        self.assertNotIn("SECRETTOKEN", out)
        self.assertIn("== owner/repo ", out)


class InventoryUnresolvedDefaultBranchTest(unittest.TestCase):
    """origin/HEAD が無い checkout を 0 件判定へ混ぜない（#368）。"""

    def _run(self, branches, with_route=True, head_target=None):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "app")
            os.mkdir(repo)
            _git("init", "-q", cwd=repo)
            _git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=repo)
            with open(os.path.join(repo, "notify.sh"), "w") as f:
                f.write("signaly-notify\n" if with_route else "echo hi\n")
            _git("add", ".", cwd=repo)
            _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x", cwd=repo)
            for b in branches:
                _git("update-ref", f"refs/remotes/origin/{b}", "HEAD", cwd=repo)
            if head_target:
                _git("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/{head_target}", cwd=repo)
            r = subprocess.run(["bash", SCRIPT, root], capture_output=True, text=True, check=True)
            return r.stdout

    def test_master_only_is_inspected(self):
        out = self._run(["master"])
        self.assertIn("送信元が残っているリポジトリ: 1", out)
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 0", out)

    def test_single_unknown_branch_is_not_guessed(self):
        out = self._run(["feature-x"])
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 1", out)
        self.assertIn("停止ゲート未達", out)

    def test_stale_develop_and_main_are_unresolved(self):
        # 実際の既定が main で古い develop も残る構成。develop だけを見て 0 件にしない（#368 世代2）
        out = self._run(["develop", "main"])
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 1", out)
        self.assertIn("停止ゲート未達", out)
        self.assertNotIn("コード上の送信元は残っていない", out)

    def test_dangling_head_with_two_known_branches_is_unresolved(self):
        out = self._run(["develop", "main"], head_target="origin/gone")
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 1", out)

    def test_trunk_is_inspected(self):
        out = self._run(["trunk"])
        self.assertIn("送信元が残っているリポジトリ: 1", out)

    def test_dangling_origin_head_is_unresolved(self):
        out = self._run([], head_target="origin/gone")
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 1", out)
        self.assertNotIn("コード上の送信元は残っていない", out)

    def test_dangling_origin_head_falls_back_to_main(self):
        out = self._run(["main"], head_target="origin/gone")
        self.assertIn("送信元が残っているリポジトリ: 1", out)

    def test_unresolved_and_live_mixed(self):
        with tempfile.TemporaryDirectory() as root:
            for name, branch, route in (("a", "main", True), ("b", "x1", False)):
                repo = os.path.join(root, name)
                os.mkdir(repo)
                _git("init", "-q", cwd=repo)
                _git("remote", "add", "origin", f"https://github.com/o/{name}.git", cwd=repo)
                with open(os.path.join(repo, "f.sh"), "w") as f:
                    f.write("signaly-notify\n" if route else "x\n")
                _git("add", ".", cwd=repo)
                _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x", cwd=repo)
                _git("update-ref", f"refs/remotes/origin/{branch}", "HEAD", cwd=repo)
                if name == "b":
                    _git("update-ref", "refs/remotes/origin/x2", "HEAD", cwd=repo)
            out = subprocess.run(["bash", SCRIPT, root], capture_output=True, text=True, check=True).stdout
            self.assertIn("停止ゲート未達（送信元 1 件・未確認 1 件）", out)

    def test_ambiguous_branches_are_unresolved_not_zero(self):
        out = self._run(["alpha", "beta"])
        self.assertIn("送信元が残っているリポジトリ: 0", out)
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 1", out)
        self.assertIn("停止ゲート未達", out)
        self.assertNotIn("コード上の送信元は残っていない", out)

    def test_no_remote_branches_is_unresolved(self):
        out = self._run([])
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 1", out)
        self.assertIn("停止ゲート未達", out)

    def test_clean_resolved_repo_passes_gate(self):
        out = self._run(["main"], with_route=False)
        self.assertIn("未確認のリポジトリ（既定ブランチ不明）: 0", out)
        self.assertIn("コード上の送信元は残っていない", out)


if __name__ == "__main__":
    unittest.main()
