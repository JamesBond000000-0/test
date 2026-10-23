"""discord_bot のコマンド権限チェックの回帰テスト。

実行: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

for _k in list(os.environ):
    if _k.lower().endswith("_proxy"):
        del os.environ[_k]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twitch_chat_downloader import discord_bot as db


def fake_interaction(user_id=100, guild=None, perms=None):
    user = SimpleNamespace(id=user_id, guild_permissions=perms)
    return SimpleNamespace(user=user, guild=guild)


def fake_guild(owner_id=999):
    return SimpleNamespace(owner_id=owner_id)


class OperatorCheckTest(unittest.TestCase):
    def test_env_listed_user_is_operator_even_in_dm(self):
        with patch.dict(os.environ, {"BOT_OPERATOR_IDS": "42, 100"}):
            ix = fake_interaction(user_id=100, guild=None)
            self.assertTrue(db._is_chat_operator(ix))

    def test_guild_owner_is_operator(self):
        with patch.dict(os.environ, {"BOT_OPERATOR_IDS": ""}):
            ix = fake_interaction(user_id=999, guild=fake_guild(owner_id=999))
            self.assertTrue(db._is_chat_operator(ix))

    def test_administrator_is_operator(self):
        perms = SimpleNamespace(administrator=True, manage_guild=False)
        with patch.dict(os.environ, {"BOT_OPERATOR_IDS": ""}):
            ix = fake_interaction(user_id=100, guild=fake_guild(), perms=perms)
            self.assertTrue(db._is_chat_operator(ix))

    def test_manage_guild_is_operator(self):
        perms = SimpleNamespace(administrator=False, manage_guild=True)
        with patch.dict(os.environ, {"BOT_OPERATOR_IDS": ""}):
            ix = fake_interaction(user_id=100, guild=fake_guild(), perms=perms)
            self.assertTrue(db._is_chat_operator(ix))

    def test_plain_member_is_not_operator(self):
        perms = SimpleNamespace(administrator=False, manage_guild=False)
        with patch.dict(os.environ, {"BOT_OPERATOR_IDS": ""}):
            ix = fake_interaction(user_id=100, guild=fake_guild(), perms=perms)
            self.assertFalse(db._is_chat_operator(ix))

    def test_dm_non_listed_is_not_operator(self):
        with patch.dict(os.environ, {"BOT_OPERATOR_IDS": ""}):
            ix = fake_interaction(user_id=100, guild=None)
            self.assertFalse(db._is_chat_operator(ix))

    def test_operator_only_names_exist_in_command_tree(self):
        all_names = {c.qualified_name for c in db.chat_group.walk_commands()}
        self.assertTrue(db.OPERATOR_ONLY_COMMANDS <= all_names)
        for expected in ("chat repair", "chat logs redownload", "chat migrate"):
            self.assertIn(expected, db.OPERATOR_ONLY_COMMANDS)
        for read_only in ("chat list", "chat status", "chat help"):
            self.assertNotIn(read_only, db.OPERATOR_ONLY_COMMANDS)


if __name__ == "__main__":
    unittest.main()
