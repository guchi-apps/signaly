"""デプロイ時マイグレーション（migrate_db.py）と DDL 用エンジンの選択のテスト（#183）。

本番のアプリ用 DB ユーザーは CRUD 権限しか持たないため、DDL は必ず
`DB_ADMIN_USER` / `DB_ADMIN_PASSWORD` のマイグレーション専用ユーザーで流す必要がある。
ここではその切り替えと、`.env` の読み込みが環境変数を上書きしないことを確かめる。
MySQL には接続しない（create_engine は遅延接続で、URL の組み立てだけを見る）。
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB_NAME", "ci_signaly")

import database  # noqa: E402
import migrate_db  # noqa: E402


class DdlEngineTest(unittest.TestCase):
    def test_falls_back_to_app_engine_without_admin_credentials(self):
        """DB_ADMIN_USER が無ければアプリ用エンジンを使う（ローカル向けのフォールバック）。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DB_ADMIN_USER", None)
            os.environ.pop("DB_ADMIN_PASSWORD", None)
            bind, is_admin = database.ddl_engine()
        self.assertIs(bind, database.engine)
        self.assertFalse(is_admin)

    def test_uses_admin_credentials_when_provided(self):
        """DB_ADMIN_USER があれば、その資格情報の別エンジンを返す。"""
        with patch.dict(os.environ, {"DB_ADMIN_USER": "migrator", "DB_ADMIN_PASSWORD": "pw"}):
            bind, is_admin = database.ddl_engine()
        self.assertTrue(is_admin)
        self.assertIsNot(bind, database.engine)
        self.assertEqual(bind.url.username, "migrator")
        self.assertEqual(bind.url.password, "pw")
        self.assertEqual(bind.url.database, os.environ["DB_NAME"])
        bind.dispose()

    def test_password_with_url_special_characters(self):
        """記号入りのパスワードでも URL が壊れない（本番のパスワードは自動生成）。"""
        with patch.dict(os.environ, {"DB_ADMIN_USER": "mig@ator", "DB_ADMIN_PASSWORD": "p@ss:w/rd?#"}):
            bind, _ = database.ddl_engine()
        self.assertEqual(bind.url.username, "mig@ator")
        self.assertEqual(bind.url.password, "p@ss:w/rd?#")
        bind.dispose()

    def test_init_db_uses_ddl_engine_and_disposes_it(self):
        """init_db は DDL 用エンジンで create_all し、使い終えたら破棄する。"""
        fake = MagicMock()
        with patch.object(database, "ddl_engine", return_value=(fake, True)), \
                patch.object(database.Base.metadata, "create_all") as create_all, \
                patch.object(database, "_migrate_add_columns") as migrate:
            database.init_db()
        create_all.assert_called_once_with(bind=fake)
        migrate.assert_called_once_with(fake)
        fake.dispose.assert_called_once_with()


class InitDbCreatesTablesTest(unittest.TestCase):
    """init_db が実際にスキーマを作れることを SQLite に通して確かめる。

    #183 で落ちたのは `channel_aliases` が新規テーブルだったため。モデルを足したときに
    create_all の対象へ入っているかを、戻り値ではなく実際のテーブル一覧で見る。
    """

    def test_creates_every_table_including_channel_aliases(self):
        from sqlalchemy import create_engine, inspect
        from sqlalchemy.pool import StaticPool

        sqlite_engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        with patch.object(database, "ddl_engine", return_value=(sqlite_engine, False)):
            database.init_db()

        tables = set(inspect(sqlite_engine).get_table_names())
        self.assertIn("channel_aliases", tables)
        self.assertEqual(set(database.Base.metadata.tables) - tables, set())
        sqlite_engine.dispose()


class LoadEnvFileTest(unittest.TestCase):
    def _write(self, tmp: str, body: str) -> Path:
        path = Path(tmp) / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_reads_key_value_lines(self):
        with TemporaryDirectory() as tmp:
            path = self._write(tmp, "DB_USER=app\n# コメント\n\nDB_PORT=3306\n")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DB_USER", None)
                os.environ.pop("DB_PORT", None)
                migrate_db.load_env_file(path)
                self.assertEqual(os.environ["DB_USER"], "app")
                self.assertEqual(os.environ["DB_PORT"], "3306")

    def test_does_not_override_existing_environment(self):
        """デプロイが渡した DB_ADMIN_* を .env の値で上書きしない。"""
        with TemporaryDirectory() as tmp:
            path = self._write(tmp, "DB_USER=from_env_file\n")
            with patch.dict(os.environ, {"DB_USER": "from_shell"}):
                migrate_db.load_env_file(path)
                self.assertEqual(os.environ["DB_USER"], "from_shell")

    def test_value_may_contain_equals_and_spaces(self):
        with TemporaryDirectory() as tmp:
            path = self._write(tmp, "ALLOWED_EMAILS=a@example.com, b@example.com\nSECRET_KEY=ab=cd==\n")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ALLOWED_EMAILS", None)
                os.environ.pop("SECRET_KEY", None)
                migrate_db.load_env_file(path)
                self.assertEqual(os.environ["ALLOWED_EMAILS"], "a@example.com, b@example.com")
                self.assertEqual(os.environ["SECRET_KEY"], "ab=cd==")

    def test_missing_file_is_not_an_error(self):
        with TemporaryDirectory() as tmp:
            migrate_db.load_env_file(Path(tmp) / "does-not-exist")


class GrantHintTest(unittest.TestCase):
    def test_hint_tells_which_user_was_used(self):
        without_admin = migrate_db._grant_hint("app_signaly", using_admin=False)
        self.assertIn("DB_ADMIN_USER / DB_ADMIN_PASSWORD が渡されていません", without_admin)
        self.assertIn("GRANT", without_admin)
        self.assertIn("app_signaly", without_admin)

        with_admin = migrate_db._grant_hint("app_signaly", using_admin=True)
        self.assertIn("この DB への DDL 権限がありません", with_admin)


class AppStartupTest(unittest.TestCase):
    def test_lifespan_does_not_run_ddl(self):
        """アプリ起動時に DDL を流さない（本番のアプリ用ユーザーは権限を持たない）。"""
        import main  # noqa: PLC0415  DB_NAME を設定した後で import する

        self.assertFalse(hasattr(main, "init_db"))
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertNotIn("init_db()", source)


if __name__ == "__main__":
    unittest.main()
