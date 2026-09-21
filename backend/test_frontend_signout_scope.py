"""ログアウトが他アプリのセッションを巻き込まないことの回帰テスト（#263）。

Supabase プロジェクトは他アプリと共有している。`supabase.auth.signOut()` の既定 scope は
`global` で、引数なしで呼ぶと同じユーザーの他アプリ・他端末の refresh token まで失効する。

フロントエンドにはビルドも JS のテスト基盤も無い（Node を使わない方針）ため、
ソースを読んで scope の指定を固定する。実際の Supabase へは繋がない。
"""

import re
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# `.auth.signOut(...)` の呼び出しと、その引数
SIGN_OUT_CALL = re.compile(r"\.auth\.signOut\(([^)]*)\)")


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def _sign_out_calls(source: str) -> list[str]:
    """supabase.auth.signOut(...) の引数を（空白を詰めて）返す。"""
    return [re.sub(r"\s+", "", m) for m in SIGN_OUT_CALL.findall(source)]


class SignOutScopeTest(unittest.TestCase):
    def test_auth_js_signs_out_with_local_scope(self):
        calls = _sign_out_calls(_read("auth.js"))
        self.assertEqual(
            calls,
            ["{scope:'local'}"],
            "supabase.auth.signOut は scope: 'local' を明示して1か所だけで呼ぶ"
            "（引数なしは global になり、他アプリのセッションまで失効する）",
        )

    def test_no_other_file_calls_supabase_sign_out_directly(self):
        # 403 時の破棄・通常ログアウトは SignalyAuth.signOut を通す。
        # 別の場所で直接呼ぶと local 指定が外れた経路が生まれる。
        for path in sorted(FRONTEND.rglob("*")):
            if path.suffix not in {".js", ".html"} or path.name == "auth.js":
                continue
            with self.subTest(file=path.relative_to(FRONTEND).as_posix()):
                self.assertEqual(
                    _sign_out_calls(path.read_text(encoding="utf-8")),
                    [],
                    "supabase.auth.signOut を直接呼ばず SignalyAuth.signOut を通すこと",
                )

    def test_global_scope_is_never_requested(self):
        for path in sorted(FRONTEND.rglob("*")):
            if path.suffix not in {".js", ".html"}:
                continue
            with self.subTest(file=path.relative_to(FRONTEND).as_posix()):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    r"scope\s*:\s*['\"]global['\"]",
                )


if __name__ == "__main__":
    unittest.main()
