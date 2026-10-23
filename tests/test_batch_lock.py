"""バッチ排他ロック / トラックリストロックの回帰テスト。

実行: python -m unittest discover -s tests -v
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

for _k in list(os.environ):
    if _k.lower().endswith("_proxy"):
        del os.environ[_k]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twitch_chat_downloader.discord_bot import TwitchChatBot


class BatchLockTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = TwitchChatBot()

    async def test_locks_exist(self):
        self.assertFalse(self.bot._batch_lock.locked())
        self.assertFalse(self.bot._track_list_lock.locked())

    async def test_run_batch_rejected_while_locked(self):
        called = False

        async def body(progress):
            nonlocal called
            called = True
            return {"ok": True}

        async with self.bot._batch_lock:
            result = await self.bot._run_batch(
                None, title="t", body=body, status_msg=None, channel=None
            )
        self.assertTrue(result.get("busy"))
        self.assertFalse(called)

    async def test_run_batch_runs_when_free(self):
        async def body(progress):
            progress.tick("step")
            return {"ok": True, "marker": 1}

        result = await self.bot._run_batch(
            None, title="t", body=body, status_msg=None, channel=None
        )
        self.assertFalse(result.get("busy", False))
        self.assertEqual(result.get("marker"), 1)

    async def test_track_list_wrapper_waits_for_lock(self):
        async with self.bot._track_list_lock:
            task = asyncio.create_task(
                self.bot._process_add_logins(None, ["someone"], None)
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        # ロック解放後は impl が走り始める (logger 未設定のため例外で終わる)
        with self.assertRaises(Exception):
            await asyncio.wait_for(task, timeout=5)


if __name__ == "__main__":
    unittest.main()
