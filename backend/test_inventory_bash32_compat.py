"""scripts/inventory-signaly-senders.sh が macOS 標準の Bash 3.2 で動く書き方であることを固定する（#372）。

Bash 3.2 には連想配列（declare -A）・mapfile/readarray が無く、set -u のもとで空配列の
"${arr[@]}" は unbound variable になる。BSD sed は \\t と `t;` を解釈しない。
"""
import os
import re
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "inventory-signaly-senders.sh")


def _code_lines():
    with open(SCRIPT, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.lstrip().startswith("#"):
                yield n, line


class InventoryBash32CompatTest(unittest.TestCase):
    def test_no_bash4_constructs(self):
        banned = [
            (r"\bdeclare\s+-[a-zA-Z]*A", "連想配列"),
            (r"\b(mapfile|readarray)\b", "mapfile/readarray"),
            (r"\blocal\s+-[a-zA-Z]*[An]", "local -A/-n"),
            (r"\$\{[A-Za-z_]+(\^\^|,,)\}", "大文字小文字変換"),
            (r"\|&", "|&"),
            (r"\bdeclare\s+-n\b", "nameref"),
            (r"\$\{[A-Za-z_]+,,?\}", "小文字変換"),
            (r"\$\{![A-Za-z_]+\[", "配列の添字展開 ${!arr[@]}"),
            (r"&>>|\bcoproc\b|\bdeclare\s+-g\b", "Bash 4 以降の構文"),
            # set -u で空配列を裸で展開すると Bash 3.2 は unbound variable で落ちる。${a[@]+"${a[@]}"} の形にする
            (r"(?<!\+)\"\$\{(order|refname|hits_ref|checkouts)\[@\]\}\"", "空配列の裸の展開"),
        ]
        for n, line in _code_lines():
            for pat, label in banned:
                self.assertIsNone(re.search(pat, line), f"{label} は Bash 3.2 に無い (L{n}): {line.strip()}")

    def test_no_bsd_sed_pitfalls(self):
        for n, line in _code_lines():
            line = re.sub(r"awk '[^']*'", "awk", line)  # awk は \\t を解釈できる
            if "sed" in line:
                self.assertNotIn("\\t", line, f"BSD sed は \\t を解釈しない (L{n})")
                self.assertIsNone(re.search(r"\bt;", line), f"BSD sed は `t;` のラベルを誤読する (L{n})")

    def test_empty_root_runs_under_set_u(self):
        # 実行側の bash は新しいので 3.2 の落ち方は再現できない。裸の展開は上の静的検査で防ぎ、ここは空でも完走する回帰の目安
        with tempfile.TemporaryDirectory() as root:
            r = subprocess.run(["bash", SCRIPT, root], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("対象リポジトリ（正規化後）: 0", r.stdout)

    def test_same_origin_checkouts_are_merged(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("a", "b"):
                repo = os.path.join(root, name)
                os.mkdir(repo)
                for args in (["init", "-q"], ["remote", "add", "origin", "https://github.com/o/r.git"]):
                    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
                with open(os.path.join(repo, "x.sh"), "w") as f:
                    f.write("signaly-notify\n")
                subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "-qm", "x"],
                               cwd=repo, check=True, capture_output=True)
                subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
            r = subprocess.run(["bash", SCRIPT, root], capture_output=True, text=True, check=True)
            self.assertIn("対象リポジトリ（正規化後）: 1", r.stdout)
            self.assertIn("送信元が残っているリポジトリ: 1", r.stdout)


if __name__ == "__main__":
    unittest.main()
