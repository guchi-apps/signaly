"""SECRET_KEY が空のまま本番・トンネル環境で起動しないことのテスト（#282）

`_signer()` は SECRET_KEY が空だと固定鍵へ黙ってフォールバックする。
APP_URL が https（= SESSION_COOKIE_SECURE）のときにこれが起きないよう、
起動時（lifespan）で例外を出して落とすことを確認する。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402


class ValidateSecretKeyConfigTest(unittest.TestCase):
    def test_raises_when_https_and_secret_key_empty(self):
        with (
            patch.object(auth, "SESSION_COOKIE_SECURE", True),
            patch.object(auth, "SECRET_KEY", ""),
        ):
            with self.assertRaises(RuntimeError):
                auth.validate_secret_key_config()

    def test_ok_when_https_and_secret_key_set(self):
        with (
            patch.object(auth, "SESSION_COOKIE_SECURE", True),
            patch.object(auth, "SECRET_KEY", "a-real-secret"),
        ):
            auth.validate_secret_key_config()  # 例外が出なければ成功

    def test_ok_when_not_https_and_secret_key_empty(self):
        """ローカル開発（http）では固定鍵フォールバックを許容する。"""
        with (
            patch.object(auth, "SESSION_COOKIE_SECURE", False),
            patch.object(auth, "SECRET_KEY", ""),
        ):
            auth.validate_secret_key_config()  # 例外が出なければ成功


class LifespanStartupTest(unittest.TestCase):
    """`with TestClient(...)` でのみ lifespan（起動処理）が実際に走る。"""

    def test_startup_fails_when_https_and_secret_key_empty(self):
        with (
            patch.object(auth, "SESSION_COOKIE_SECURE", True),
            patch.object(auth, "SECRET_KEY", ""),
        ):
            with self.assertRaises(Exception):
                with TestClient(main.app):
                    pass

    def test_startup_succeeds_when_secret_key_set(self):
        with (
            patch.object(auth, "SESSION_COOKIE_SECURE", True),
            patch.object(auth, "SECRET_KEY", "a-real-secret"),
        ):
            with TestClient(main.app):
                pass


if __name__ == "__main__":
    unittest.main()
