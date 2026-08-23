"""チャンネル統合（_merge_channel）と別名解決（_resolve_webhook_target）のテスト

統合は通知履歴を UPDATE で移し替える不可逆な操作なので、モックではなく実際に SQL を
通して確かめる。本番は MySQL だが、ここでは同じ SQLAlchemy モデルを in-memory SQLite に
作って get_session を差し替える（database.py の engine は import 時に接続しないため、
MySQL が無くてもこのファイルは動く）。
"""

import os
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "ci_signaly")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import notification_prefs  # noqa: E402
from database import (  # noqa: E402
    Base,
    Channel,
    ChannelAlias,
    Notification,
    NotificationSetting,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


class ChannelMergeTestBase(unittest.TestCase):
    def setUp(self):
        # StaticPool で単一接続を使い回さないと、in-memory DB が接続ごとに作り直される。
        # check_same_thread=False は asyncio.to_thread 経由の呼び出しのため。
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        patches = [
            patch.object(main, "get_session", self._session),
            patch.object(notification_prefs, "get_session", self._session),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _session(self) -> Session:
        return Session(self.engine)

    # ── 準備用ヘルパー ────────────────────────────────────────────────────

    def add_channel(self, name: str) -> str:
        channel_id = str(uuid.uuid4())
        with self._session() as session:
            session.add(
                Channel(id=channel_id, name=name, sort_order=0, created_at=NOW, updated_at=NOW)
            )
            session.commit()
        return channel_id

    def add_notification(self, channel: str, title: str, source=None) -> str:
        notif_id = str(uuid.uuid4())
        with self._session() as session:
            session.add(
                Notification(
                    id=notif_id,
                    channel=channel,
                    title=title,
                    message="",
                    level="info",
                    timestamp=NOW,
                    source=source,
                )
            )
            session.commit()
        return notif_id

    def notification(self, notif_id: str) -> Notification:
        with self._session() as session:
            return session.query(Notification).filter(Notification.id == notif_id).one()


class MergeChannelTest(ChannelMergeTestBase):
    def setUp(self):
        super().setUp()
        self.origin_id = self.add_channel("ci-signaly")
        self.target_id = self.add_channel("CI")
        self.plain = self.add_notification("ci-signaly", "デプロイ 成功")
        self.tagged = self.add_notification("ci-signaly", "CI 失敗", source="Signaly")
        self.other = self.add_notification("CI", "既存の通知", source="car-care")

    def test_moves_history_to_target(self):
        result = main._merge_channel(self.origin_id, self.target_id, None)
        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["channel"], "CI")
        self.assertEqual(self.notification(self.plain).channel, "CI")
        self.assertEqual(self.notification(self.tagged).channel, "CI")

    def test_fills_missing_source_with_origin_name(self):
        main._merge_channel(self.origin_id, self.target_id, None)
        self.assertEqual(self.notification(self.plain).source, "ci-signaly")

    def test_keeps_existing_source(self):
        main._merge_channel(self.origin_id, self.target_id, None)
        self.assertEqual(self.notification(self.tagged).source, "Signaly")

    def test_explicit_source_label(self):
        main._merge_channel(self.origin_id, self.target_id, "signaly")
        self.assertEqual(self.notification(self.plain).source, "signaly")

    def test_does_not_touch_other_channels(self):
        main._merge_channel(self.origin_id, self.target_id, None)
        self.assertEqual(self.notification(self.other).source, "car-care")

    def test_origin_channel_is_removed(self):
        main._merge_channel(self.origin_id, self.target_id, None)
        with self._session() as session:
            self.assertIsNone(
                session.query(Channel).filter(Channel.id == self.origin_id).first()
            )

    def test_old_webhook_url_still_works(self):
        """統合しても旧チャンネルIDへの POST が統合先へ届くこと（差し替え不要）"""
        main._merge_channel(self.origin_id, self.target_id, "signaly")
        self.assertEqual(
            main._resolve_webhook_target(self.origin_id), ("CI", "signaly")
        )

    def test_unknown_id_resolves_to_none(self):
        self.assertIsNone(main._resolve_webhook_target("no-such-id"))

    def test_live_channel_resolves_without_source(self):
        self.assertEqual(main._resolve_webhook_target(self.target_id), ("CI", None))

    def test_notification_setting_is_removed(self):
        with self._session() as session:
            session.add(
                NotificationSetting(
                    email="me@example.com",
                    target_type="channel",
                    target_id=self.origin_id,
                    enabled=False,
                    updated_at=NOW,
                )
            )
            session.commit()

        main._merge_channel(self.origin_id, self.target_id, None)

        with self._session() as session:
            self.assertEqual(
                session.query(NotificationSetting)
                .filter(NotificationSetting.target_id == self.origin_id)
                .count(),
                0,
            )

    def test_chained_merge_repoints_alias(self):
        """A→B のあと B→C と統合しても、A の旧URLが C へ届くこと"""
        final_id = self.add_channel("通知")
        main._merge_channel(self.origin_id, self.target_id, "signaly")
        main._merge_channel(self.target_id, final_id, "ci")
        self.assertEqual(main._resolve_webhook_target(self.origin_id)[0], "通知")
        self.assertEqual(main._resolve_webhook_target(self.target_id)[0], "通知")

    def test_merge_into_itself_rejected(self):
        with self.assertRaises(ValueError):
            main._merge_channel(self.origin_id, self.origin_id, None)

    def test_unknown_channel_rejected(self):
        with self.assertRaises(LookupError):
            main._merge_channel(self.origin_id, "no-such-id", None)

    def test_no_alias_row_left_behind_for_unknown(self):
        with self.assertRaises(LookupError):
            main._merge_channel("no-such-id", self.target_id, None)
        with self._session() as session:
            self.assertEqual(session.query(ChannelAlias).count(), 0)


class ChannelSourcesTest(ChannelMergeTestBase):
    def test_counts_by_source_desc(self):
        self.add_channel("CI")
        self.add_notification("CI", "a", source="signaly")
        self.add_notification("CI", "b", source="car-care")
        self.add_notification("CI", "c", source="car-care")
        self.add_notification("CI", "d")

        sources = main._fetch_channel_sources("CI")
        self.assertEqual(
            sources,
            [
                {"name": "car-care", "count": 2},
                {"name": "-", "count": 1},
                {"name": "signaly", "count": 1},
            ],
        )

    def test_source_filter_in_history(self):
        self.add_channel("CI")
        self.add_notification("CI", "a", source="signaly")
        self.add_notification("CI", "b")

        logs, _ = main._fetch_history("CI", 100, source="signaly")
        self.assertEqual([log["title"] for log in logs], ["a"])

        # "-" は「送信元が未設定」を表す擬似的な名前
        logs, _ = main._fetch_history("CI", 100, source=main.SOURCE_NONE)
        self.assertEqual([log["title"] for log in logs], ["b"])

    def test_source_filter_in_search(self):
        self.add_channel("CI")
        self.add_notification("CI", "デプロイ 成功", source="signaly")
        self.add_notification("CI", "デプロイ 失敗", source="car-care")

        results = main._search_notifications("デプロイ", 100, "CI", "car-care")
        self.assertEqual([r["title"] for r in results], ["デプロイ 失敗"])


class MergeEndpointTest(ChannelMergeTestBase):
    def setUp(self):
        super().setUp()
        main.app.dependency_overrides[auth.require_auth] = lambda: "me@example.com"
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.origin_id = self.add_channel("ci-signaly")
        self.target_id = self.add_channel("CI")
        self.notif = self.add_notification("ci-signaly", "デプロイ 成功")

    def test_merge(self):
        res = self.client.post(
            f"/api/channels/{self.origin_id}/merge",
            json={"target_channel_id": self.target_id, "source": "signaly"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["moved"], 1)
        self.assertEqual(self.notification(self.notif).source, "signaly")

    def test_merge_into_itself_is_400(self):
        res = self.client.post(
            f"/api/channels/{self.origin_id}/merge",
            json={"target_channel_id": self.origin_id},
        )
        self.assertEqual(res.status_code, 400)

    def test_unknown_target_is_404(self):
        res = self.client.post(
            f"/api/channels/{self.origin_id}/merge",
            json={"target_channel_id": "no-such-id"},
        )
        self.assertEqual(res.status_code, 404)

    def test_sources_endpoint(self):
        res = self.client.get(f"/api/channels/{self.target_id}/sources")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["sources"], [])


class WebhookSourceTest(ChannelMergeTestBase):
    """送信側を変えずに送信元が記録されること（共通化の前提）"""

    def setUp(self):
        super().setUp()
        push = patch.object(main, "send_push_notifications", lambda entry: None)
        push.start()
        self.addCleanup(push.stop)
        self.client = TestClient(main.app)
        self.origin_id = self.add_channel("ci-signaly")
        self.target_id = self.add_channel("CI")

    def latest(self) -> Notification:
        with self._session() as session:
            return (
                session.query(Notification)
                .order_by(Notification.timestamp.desc())
                .first()
            )

    def post_ci_payload(self, channel_id, headers=None):
        # .github/scripts/signaly-notify.sh が実際に送る形
        return self.client.post(
            f"/webhook/{channel_id}",
            headers=headers or {},
            json={
                "title": "✅ [Signaly] デプロイ 成功",
                "color": "#57f287",
                "fields": [
                    {"name": "App", "value": "Signaly", "inline": True},
                    {"name": "Repository", "value": "`guchi-apps/signaly`", "inline": True},
                ],
            },
        )

    def test_source_from_app_field(self):
        res = self.post_ci_payload(self.target_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.latest().source, "Signaly")

    def test_header_overrides_payload(self):
        # HTTP ヘッダーは ASCII しか運べないため、日本語を使いたい場合は ?source= を使う
        self.post_ci_payload(self.target_id, {"X-Signaly-Source": "manual"})
        self.assertEqual(self.latest().source, "manual")

    def test_query_param_source(self):
        self.client.post(f"/webhook/{self.target_id}?source=バックアップ", json={"message": "x"})
        self.assertEqual(self.latest().source, "バックアップ")

    def test_merged_channel_keeps_receiving(self):
        """統合後も旧チャンネルIDへの POST が統合先へ届き、送信元が付くこと"""
        main._merge_channel(self.origin_id, self.target_id, "signaly")

        res = self.client.post(f"/webhook/{self.origin_id}", json={"message": "no fields"})
        self.assertEqual(res.status_code, 200)
        entry = self.latest()
        self.assertEqual(entry.channel, "CI")
        self.assertEqual(entry.source, "signaly")

    def test_unknown_channel_is_404(self):
        res = self.client.post("/webhook/no-such-id", json={"message": "x"})
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
