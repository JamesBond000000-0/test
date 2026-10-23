"""chat_logger のメタデータ往復と DB アクセサの回帰テスト。

実行: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# httpx が環境の proxy 設定を読むため、テストでは無効化する
# (no_proxy の "[::1]" 形式が httpx の URLPattern 生成で落ちる環境がある)
for _k in list(os.environ):
    if _k.lower().endswith("_proxy"):
        del os.environ[_k]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twitch_chat_downloader.chat_logger import ChatLogger, ChatLoggerDB


class MetadataRoundtripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = ChatLogger(data_dir=self.tmp.name, enable_emotes=False)

    def tearDown(self):
        self.logger.db.close()
        self.tmp.cleanup()

    def test_roundtrip_preserves_stream_start_end(self):
        info = {
            "vod_id": "1234567890",
            "streamer_login": "someone",
            "streamer_display_name": "Someone",
            "stream_title": "title",
            "stream_date": "2026-10-20",
            "stream_start": "2026-10-20T12:00:00Z",
            "stream_end": "2026-10-20T15:30:00Z",
            "stream_duration_seconds": 12600,
            "total_comments": 42,
            "total_parts": 1,
            "emotes_providers": "twitch",
            "is_partial": False,
        }
        parsed = self.logger.parse_metadata_message(
            self.logger.build_metadata_message(info)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["stream_start"], "2026-10-20T12:00:00Z")
        self.assertEqual(parsed["stream_end"], "2026-10-20T15:30:00Z")
        self.assertEqual(parsed["vod_id"], "1234567890")
        self.assertEqual(parsed["total_comments"], 42)

    def test_get_upload_roundtrip(self):
        db = ChatLoggerDB(str(Path(self.tmp.name) / "sub" / "chat_log.db"))
        try:
            self.assertIsNone(db.get_upload("999"))
            db.record_upload("999", {
                "streamer_login": "someone",
                "stream_start": "2026-10-20T12:00:00Z",
                "stream_end": "2026-10-20T13:00:00Z",
                "discord_message_ids": [111, 222],
            })
            row = db.get_upload("999")
            self.assertIsNotNone(row)
            self.assertEqual(row["stream_start"], "2026-10-20T12:00:00Z")
            self.assertEqual(row["discord_message_ids"], [111, 222])
            self.assertTrue(row["uploaded_at"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
