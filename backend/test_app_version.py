"""稼働版を返す GET /api/app-version のテスト（#348）

VPS の app-deploy-gate は再起動後にこの `sha` をデプロイしたコミットと突き合わせる（#352）。
認証の外にあること・キャッシュさせないこと・未設定なら null を返すことを固定する。
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = json.loads(
    (Path(__file__).parent.parent / "version.json").read_text()
)["version"]


class AppVersionTest(unittest.TestCase):
    def setUp(self):
        # lifespan（init_db・ハートビート）を走らせないよう with を使わない
        self.client = TestClient(main.app)

    def test_returns_version_and_sha_without_auth(self):
        with patch.dict(os.environ, {"APP_COMMIT_SHA": SHA}):
            res = self.client.get("/api/app-version")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"version": VERSION, "sha": SHA})

    def test_not_cached(self):
        with patch.dict(os.environ, {"APP_COMMIT_SHA": SHA}):
            res = self.client.get("/api/app-version")
        self.assertEqual(res.headers.get("cache-control"), "no-store")

    def test_sha_is_null_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "APP_COMMIT_SHA"}
        with patch.dict(os.environ, env, clear=True):
            res = self.client.get("/api/app-version")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["sha"])

    def test_sha_is_null_when_blank(self):
        with patch.dict(os.environ, {"APP_COMMIT_SHA": "  "}):
            res = self.client.get("/api/app-version")
        self.assertIsNone(res.json()["sha"])

    def test_not_shadowed_by_static_mount(self):
        # / の StaticFiles マウントより先に登録されていること（JSON が返る）
        res = self.client.get("/api/app-version")
        self.assertTrue(res.headers["content-type"].startswith("application/json"))


if __name__ == "__main__":
    unittest.main()
