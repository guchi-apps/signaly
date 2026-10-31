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


if __name__ == "__main__":
    unittest.main()
