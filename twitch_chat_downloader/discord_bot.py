"""
Discord Bot for archiving Twitch chat logs using Native Slash Commands (`/chat`).

Commands:
  /chat download <streamer> [vod_id]  - Download chats to archive and record metadata
  /chat track add/addfile/remove/show/scan   - Tracking list management
  /chat logs download/days/scan       - Daily chat logs (logs.zonian.dev); `scan` batch-runs
                                        the whole tracking list (same list as track scan).
                                        `dest:` picks the archive channel (logs|archive|<id>)
  /chat logs redownload               - Force re-download ignoring the dedup DB (purges old
                                        entries first; use to fix wrong "no logs" records)
  /chat migrate <category_ids_or_channel_ids> - Migrate old data to the dedup channel
  /chat repair [targets] [excludes]  - Scan and auto-repair incomplete chat files
  /chat status / list / sync / help  - Other
"""

from __future__ import annotations

import asyncio
import contextvars
import io as io_module
import json
import os
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .chat_logger import ChatLogger, BOT_UPLOAD_LIMIT
from .gql_client import VODUnavailableError
from . import logs_api
from .logs_api import (
    LogsAPIError,
    LogsEmptyMismatchError,
    ZonianLogsClient,
    build_day_document,
    compress_and_split,
    day_is_complete,
    day_jst_range,
    day_jst_range_text,
    filter_messages_by_user,
    get_safety_margin_hours,
    make_base_name,
    make_log_id,
)


# ---- Constants ----

CATEGORY_NAME = "Twitch Archives"             # 自動生成されるカテゴリー名
TRACKER_CHANNEL_NAME = "twitch-chat-tracker"  # トラッキングリスト管理用
ARCHIVE_CHANNEL_NAME = "twitch-chat-archives"  # 保存用（Embed & ファイルのみの美しい本棚）
DEDUP_CHANNEL_NAME = "twitch-chat-dedup"      # 重複判定記録用（Botの隠しJSONデータベース）
LOGS_ARCHIVE_CHANNEL_NAME = "twitch-logs-archives"  # デイリーログ(zonian)保存用本棚
TRACK_LIST_PREFIX = "📋 **Twitch配信者トラッキングリスト**"

# 「記録済みなのに0件が返ってきた日」の再検証スケジュール (詳細は logs_api.py 参照)。
# - 最後の確認から EMPTY_RECHECK_DAYS 日未満は再DLしない (サーバー負荷対策)
# - 連続で EMPTY_ACCEPT_AFTER 回空が続いたら、初めて「ログなし」確定にして重複DBへ登録
#   (それまでは登録しない = いつデータが出現しても保存できる)
try:
    EMPTY_RECHECK_DAYS = max(float(os.environ.get("LOGS_EMPTY_RECHECK_DAYS", 3.0)), 0.0)
except (TypeError, ValueError):
    EMPTY_RECHECK_DAYS = 3.0
try:
    EMPTY_ACCEPT_AFTER = max(int(os.environ.get("LOGS_EMPTY_ACCEPT_AFTER", 5)), 2)
except (TypeError, ValueError):
    EMPTY_ACCEPT_AFTER = 5

JST = logs_api.JST

DESCRIPTION = (
    "Twitch Chat Logger Bot - Downloads Twitch VOD chat logs with emotes "
    "and organizes them into dedicated channels under a category.\n\n"
    "**コマンド一覧 (スラッシュコマンド `/` で利用可能):**\n"
    "`/chat download <streamer>` - 古いVODから全部DL(重複回避)\n"
    "`/chat download <streamer> [vod_id]` - 指定したVODのみDL\n"
    "`/chat track add <streamers>` / `addfile` - 追加(ｶﾝﾏ区切り/ﾌｧｲﾙ)\n"
    "`/chat track remove <streamer>` - トラッキングリストから削除\n"
    "`/chat track show` - トラッキングリスト表示\n"
    "`/chat track scan` - 登録配信者の全VODを一括スキャンDL\n"
    "`/chat migrate <ids>` - 旧チャンネル内の動画を新データベースへポインタ移行\n"
    "`/chat repair [targets] [excludes]` - 欠落パーツのVODを検出し自動修復DL\n"
    "`/chat logs download <streamer>` - デイリーチャットログ(zonian)を1日ずつDL (`force:True` で再DL)\n"
    "`/chat logs redownload <streamer>` - 重複DBの記録を消去して強制再DL (誤った「ログなし」登録の修正用)\n"
    "`/chat logs days <streamer>` - DL可能日数・保存状況を表示\n"
    "`/chat logs scan` - トラッキングリスト全員のデイリーログを一括DL (`force:True` で再DL)\n"
    "`/chat logs download|scan dest:archive` - 保存先をVOD本棚に同居 (チャンネル数上限対策)\n"
    "`/chat list [streamer]` - アップロード済みVOD一覧\n"
    "`/chat status` - Botステータス確認\n"
    "`/chat sync` - データベース手動同期\n"
    "`/chat help` - ヘルプを表示\n\n"
    f"📌 **整理機能:** ログ本棚は `#{ARCHIVE_CHANNEL_NAME}`、回避用金庫は `#{DEDUP_CHANNEL_NAME}` に保存されます\n"
    f"📌 **デイリーログ:** zonianの1日1ファイルは `#{LOGS_ARCHIVE_CHANNEL_NAME}` に保存 (確定済みの日のみ)\n"
    f"📌 **保存先の変更:** `dest:archive` でVOD本棚 `#{ARCHIVE_CHANNEL_NAME}` に同居保存 / "
    f"`dest:<チャンネルID>` で任意のチャンネルへ (1カテゴリー最大50チャンネル対策)\n"
    f"📌 **データ整合性:** 履歴スキャンの遡りリミットは完全にオフ（無制限）です"
)


# ---- Helpers ----

# --------------------------------------------------------------------------- #
# 長時間バッチ (track scan / repair) の停滞監視と Discord へのログ出力
# --------------------------------------------------------------------------- #

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# 進捗の報告が止まってから N 秒経ったら Discord に警告 / さらに M 秒で中断
STALL_WARN_SECONDS = _env_float("BATCH_STALL_WARN_SECONDS", 120.0)
STALL_ABORT_SECONDS = _env_float("BATCH_STALL_ABORT_SECONDS", 900.0)
STALL_CHECK_INTERVAL = _env_float("BATCH_STALL_CHECK_SECONDS", 5.0)
# True にするとエラーが無くても毎回ログファイルを添付する
BATCH_LOG_ALWAYS = _env_bool("BATCH_LOG_ALWAYS", False)
# 人ごと / VODごとの間隔 (レート制限対策。速くしたいときは 0.5 程度でも可)
BATCH_ITEM_GAP_SECONDS = _env_float("BATCH_ITEM_GAP_SECONDS", 3.0)
BATCH_LOG_MAX_LINES = int(_env_float("BATCH_LOG_MAX_LINES", 800))
# DL中のステータス編集間隔 (秒)。長いVODだと編集連発でレート制限に当たるため間引く
DL_PROGRESS_THROTTLE_SECONDS = _env_float("DL_PROGRESS_THROTTLE_SECONDS", 3.0)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M:%SZ")


class BatchProgress:
    """
    バッチ処理の進捗と実行ログをまとめる小さな共有オブジェクト。

    - 各ステップから tick() を呼ぶ -> idle_seconds() がリセットされる
    - 監視タスク (_watch_batch_stall) が idle を見て警告 / 中断を要求する
    - lines に全ログを ring buffer で保持し、最後に .log として Discord へ送る

    ワーカーテーマ (asyncio.to_thread) からも tick() できる (contextvars は
    to_thread にコピーされるため、_ACTIVE_PROGRESS も中で見える)。
    """

    def __init__(self, title: str, max_lines: int = BATCH_LOG_MAX_LINES):
        self.title = title
        self.slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", title).strip("_").lower() or "batch"
        self.started = time.time()
        self.step = "(開始)"
        self.step_started = self.started
        self.updated = self.started
        self.seq = 0
        self.warned_seq = -1
        self.abort_requested = False
        self.finished = False
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.errors: list[str] = []
        self._last_line_ts = 0.0

    # ---- 進捗報告 ----

    def tick(self, step: str) -> None:
        now = time.time()
        self.seq += 1
        self.step = step
        self.step_started = now
        self.updated = now
        self._append(step)

    def throttle_tick(self, step: str, min_interval: float = 5.0) -> None:
        """高頻度で叩く進捗用 (Discord/ログを埋め尽くさないよう間引く)。"""
        now = time.time()
        self.updated = now  # idle は必ずリセットする
        if now - self._last_line_ts < min_interval:
            return
        self._last_line_ts = now
        self.seq += 1
        self.step = step
        self.step_started = now
        self._append(step)

    def note(self, text: str) -> None:
        self._append(text)

    def error(self, header: str, exc: Optional[BaseException] = None, tb: Optional[str] = None) -> None:
        if tb is None:
            tb = traceback.format_exc() if exc is None else "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        tb = (tb or "").strip()
        if tb in ("NoneType: None", ""):
            tb = f"{type(exc).__name__}: {exc}" if exc is not None else ""
        self.errors.append(f"{header}\n{tb}")
        self._append(f"X {header}")
        for line in tb.splitlines()[-40:]:
            self.lines.append(f"    {line}")

    def _append(self, text: str) -> None:
        line = f"[{_stamp()}] #{self.seq} {text}"
        self.lines.append(line)
        print(f"[{self.title}] {line}", file=sys.stderr)

    # ---- 監視・ログ生成 ----

    def idle_seconds(self) -> float:
        return time.time() - self.updated

    def step_seconds(self) -> float:
        return time.time() - self.step_started

    def elapsed_text(self) -> str:
        return _format_duration(int(time.time() - self.started))

    def render(self, aborted: bool = False) -> str:
        head = [
            f"=== {self.title} ===",
            f"generated: {_stamp()}",
            f"elapsed: {self.elapsed_text()}",
            f"steps: {self.seq}",
            f"errors: {len(self.errors)}",
            f"current step: {self.step} (running {int(self.step_seconds())}s, idle {int(self.idle_seconds())}s)",
            f"aborted: {aborted}",
            "",
            "--- log ---",
        ]
        body = list(self.lines)
        parts = ["\n".join(head + body)]
        if self.errors:
            parts.append("\n\n--- tracebacks (%d) ---\n" % len(self.errors) + "\n\n".join(self.errors))
        text = "".join(parts)
        return text[:1_800_000]

    def message_summary(self, aborted: bool = False, limit: int = 1900) -> str:
        """Discord 本文に載せる要約 (2000文字制限に収める)。"""
        lines = [
            f"**{self.title}** {'⛔ 中断' if aborted else '❌ エラー'}",
            f"経過 {self.elapsed_text()} / ステップ {self.seq} / エラー {len(self.errors)}",
            f"実行中: `{self.step[:120]}` ({int(self.step_seconds())}s)",
        ]
        tail = list(self.lines)[-12:]
        block = "\n".join(tail) if tail else "(ログ無し)"
        text = "\n".join(lines) + "\n```\n" + block + "\n```"
        if len(text) > limit:
            text = text[:limit - 4] + "\n```"
        return text


# 実行中のバッチ (scan / repair) とその中の _process_vod を結ぶ
_ACTIVE_PROGRESS: contextvars.ContextVar[Optional[BatchProgress]] = contextvars.ContextVar(
    "active_batch_progress", default=None
)


async def _notify(interaction: Optional[discord.Interaction], text: str) -> None:
    """
    バッチ処理の速報をチャンネルへ送る (送信失敗で本処理を止めない)。

    インタラクションのトークンは 15 分で失効するので、通常のチャンネル送信を
    優先する。track scan のように長時間走る処理ではこれが効く。
    """
    channel = getattr(interaction, "channel", None) if interaction else None
    body = text[:2000]
    try:
        if channel is not None:
            await channel.send(body)
        elif interaction is not None:
            await interaction.followup.send(body)
    except Exception as e:
        print(f"[!] 通知送信に失敗 ({type(e).__name__}: {e}): {body[:120]}", file=sys.stderr)


def _batch_tick(step: str) -> None:
    """進行中バッチがあれば進捗を報告する (無ければ何もしない)。"""
    prog = _ACTIVE_PROGRESS.get()
    if prog is not None:
        prog.tick(step)


def _batch_throttle_tick(step: str) -> None:
    prog = _ACTIVE_PROGRESS.get()
    if prog is not None:
        prog.throttle_tick(step)


def _batch_note(text: str) -> None:
    prog = _ACTIVE_PROGRESS.get()
    if prog is not None:
        prog.note(text)


def _batch_error(header: str, exc: Optional[BaseException] = None, tb: Optional[str] = None) -> None:
    prog = _ACTIVE_PROGRESS.get()
    if prog is not None:
        prog.error(header, exc, tb)


def _format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h{m:02d}m"
    elif m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _format_datetime(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        jst = timezone(timedelta(hours=9))
        return dt.astimezone(jst).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        s = iso_str.replace("Z", "").replace("+00:00", "")
        if "T" in s:
            return s[:19].replace("T", " ")
        return s[:10]


def _init_logger(data_dir: str | None = None) -> ChatLogger:
    if data_dir:
        path = Path(data_dir).expanduser().resolve()
    else:
        path = Path("~/.twitch_chat_logger").expanduser().resolve()
    return ChatLogger(data_dir=str(path), enable_emotes=True, max_upload_size=BOT_UPLOAD_LIMIT)


def _find_metadata_in_content(content: str) -> dict | None:
    if not content:
        return None

    try:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            potential_json = content[start:end+1]
            meta = json.loads(potential_json)
            if isinstance(meta, dict) and meta.get("t") == "chat" and meta.get("vod"):
                return {
                    "vod_id": str(meta["vod"]),
                    "streamer_login": meta.get("s", ""),
                    "streamer_display_name": meta.get("sd", ""),
                    "stream_title": meta.get("title", ""),
                    "stream_date": meta.get("date", ""),
                    "stream_start": meta.get("start", ""),
                    "stream_end": meta.get("end", ""),
                    "stream_duration_seconds": meta.get("dur", 0),
                    "total_comments": meta.get("n", 0),
                    "total_parts": meta.get("p", 1),
                    "emotes_providers": meta.get("e", ""),
                    "is_partial": meta.get("partial", 0) == 1,
                }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                meta = json.loads(line)
                if isinstance(meta, dict) and meta.get("t") == "chat" and meta.get("vod"):
                    return {
                        "vod_id": str(meta["vod"]),
                        "streamer_login": meta.get("s", ""),
                        "streamer_display_name": meta.get("sd", ""),
                        "stream_title": meta.get("title", ""),
                        "stream_date": meta.get("date", ""),
                        "stream_start": meta.get("start", ""),
                        "stream_end": meta.get("end", ""),
                        "stream_duration_seconds": meta.get("dur", 0),
                        "total_comments": meta.get("n", 0),
                        "total_parts": meta.get("p", 1),
                        "emotes_providers": meta.get("e", ""),
                        "is_partial": meta.get("partial", 0) == 1,
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    return None


def _find_log_metadata_in_content(content: str) -> dict | None:
    """
    メッセージ本文からデイリーログの重複回避メタデータJSONを抽出する。
    形式: {"t":"logs","v":1,"ch":"orslok","u":"","d":"2026-08-27","n":479,"p":1,...}
    """
    if not content:
        return None
    candidates = []

    try:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(content[start:end + 1])
    except Exception:
        pass

    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)

    for candidate in candidates:
        try:
            meta = json.loads(candidate)
            if (
                isinstance(meta, dict)
                and meta.get("t") == "logs"
                and meta.get("ch")
                and meta.get("d")
            ):
                return {
                    "log_id": make_log_id(str(meta["ch"]), meta.get("u", ""), str(meta["d"])),
                    "channel_login": str(meta["ch"]),
                    "channel_display_name": meta.get("sd", "") or str(meta["ch"]),
                    "user_filter": meta.get("u", "") or "",
                    "log_date": str(meta["d"]),
                    "message_count": meta.get("n", 0),
                    "total_parts": meta.get("p", 0),
                    "compressed_size_bytes": meta.get("z", 0),
                    "is_empty": meta.get("n", 0) == 0,
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return None


# ---- Bot class ----

class TwitchChatBot(commands.Bot):
    """Discord bot for archiving Twitch chat logs using Slash Commands."""

    def __init__(self, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(
            command_prefix="/",
            intents=intents,
            help_command=None,
            **kwargs,
        )

        self.logger: Optional[ChatLogger] = None
        self._bot_start_time = time.time()
        self._scan_cache: dict[str, set[str]] = {}
        self._track_msg_cache: dict[str, int] = {}
        self._logs_scan_cache: dict[str, set[str]] = {}
        self._zonian: Optional[ZonianLogsClient] = None

    @property
    def zonian(self) -> ZonianLogsClient:
        """logs.zonian.dev クライアント (遅延初期化)"""
        if self._zonian is None:
            self._zonian = ZonianLogsClient()
        return self._zonian

    async def setup_hook(self):
        """スラッシュコマンドをDiscord APIに同期登録"""
        print("[~] Syncing slash commands with Discord...")
        await self.tree.sync()
        print("[✓] Slash commands synced successfully!")

    async def on_ready(self):
        print(f"[✓] Logged in as {self.user} (ID: {self.user.id})")
        max_mb = BOT_UPLOAD_LIMIT // (1024*1024)
        print(f"[✓] Bot上限: {max_mb}MB (8MB固定)")
        print(f"[✓] Connected to {len(self.guilds)} guild(s)")
        for guild in self.guilds:
            print(f"    - {guild.name} (ID: {guild.id})")
            await self._get_or_create_tracker_channel(guild)
            await self._get_or_create_archive_channel(guild)
            await self._get_or_create_dedup_channel(guild)
            await self._get_or_create_logs_archive_channel(guild)
        print("[✓] Slash commands ready! Type `/chat` in Discord to see commands.")

    # ---- Dedicated Categories & Channels ----

    async def _get_or_create_category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        if not guild:
            return None
        for cat in guild.categories:
            if cat.name.lower() == CATEGORY_NAME.lower():
                return cat
        try:
            if guild.me.guild_permissions.manage_channels:
                category = await guild.create_category(name=CATEGORY_NAME)
                print(f"[✓] Created Discord Category: {CATEGORY_NAME}")
                return category
            else:
                print(f"[!] Missing 'Manage Channels' permission to create category '{CATEGORY_NAME}'")
                return None
        except Exception as e:
            print(f"[!] Failed to create category '{CATEGORY_NAME}': {e}")
            return None

    async def _get_or_create_archive_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        category = await self._get_or_create_category(guild)
        if not category:
            return None

        for ch in category.text_channels:
            if ch.name == ARCHIVE_CHANNEL_NAME:
                return ch

        try:
            if guild.me.guild_permissions.manage_channels:
                channel = await guild.create_text_channel(
                    name=ARCHIVE_CHANNEL_NAME,
                    category=category,
                    topic="Twitchチャットログ保管庫（Zstd圧縮ファイルおよび美しいEmbedカードのみ）",
                )
                print(f"[✓] Created dedicated archive channel: #{ARCHIVE_CHANNEL_NAME}")
                return channel
            else:
                print(f"[!] Missing 'Manage Channels' permission to create archive channel")
                return None
        except Exception as e:
            print(f"[!] Failed to create archive channel: {e}")
            return None

    async def _get_or_create_dedup_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        category = await self._get_or_create_category(guild)
        if not category:
            return None

        for ch in category.text_channels:
            if ch.name == DEDUP_CHANNEL_NAME:
                return ch

        try:
            if guild.me.guild_permissions.manage_channels:
                channel = await guild.create_text_channel(
                    name=DEDUP_CHANNEL_NAME,
                    category=category,
                    topic="Twitch Chat Logger - 重複判定データベース用（編集・削除厳禁）",
                )
                print(f"[✓] Created dedicated dedup channel: #{DEDUP_CHANNEL_NAME}")
                return channel
            else:
                print(f"[!] Missing 'Manage Channels' permission to create dedup channel")
                return None
        except Exception as e:
            print(f"[!] Failed to create dedup channel: {e}")
            return None

    async def _get_or_create_logs_archive_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """デイリーチャットログ (logs.zonian.dev) 保存用チャンネル"""
        if not guild:
            return None
        category = await self._get_or_create_category(guild)
        if not category:
            return None

        for ch in category.text_channels:
            if ch.name == LOGS_ARCHIVE_CHANNEL_NAME:
                return ch

        try:
            if guild.me.guild_permissions.manage_channels:
                channel = await guild.create_text_channel(
                    name=LOGS_ARCHIVE_CHANNEL_NAME,
                    category=category,
                    topic="Twitchデイリーチャットログ保管庫（logs.zonian.dev / Zstd圧縮 / 1日1ファイル）",
                )
                print(f"[✓] Created logs archive channel: #{LOGS_ARCHIVE_CHANNEL_NAME}")
                return channel
            else:
                print(f"[!] Missing 'Manage Channels' permission to create logs archive channel")
                return None
        except Exception as e:
            print(f"[!] Failed to create logs archive channel: {e}")
            return None

    async def _get_or_create_tracker_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        category = await self._get_or_create_category(guild)
        if not category:
            return None

        for ch in category.text_channels:
            if ch.name == TRACKER_CHANNEL_NAME:
                return ch

        try:
            if guild.me.guild_permissions.manage_channels:
                channel = await guild.create_text_channel(
                    name=TRACKER_CHANNEL_NAME,
                    category=category,
                    topic="Twitch Chat Logger - トラッキングデータ管理専用。書き込み禁止。",
                )
                print(f"[✓] Created tracker control channel: #{TRACKER_CHANNEL_NAME}")
                return channel
            else:
                print(f"[!] Missing 'Manage Channels' permission to create tracker control channel")
                return None
        except Exception as e:
            print(f"[!] Failed to create tracker control channel: {e}")
            return None

    # ---- デイリーログの保存先チャンネル解決 ----

    async def _resolve_logs_dest_channel(
        self,
        guild: discord.Guild,
        dest: Optional[str] = None,
    ) -> tuple[Optional[discord.TextChannel], str]:
        """
        デイリーチャットログの保存先チャンネルを解決する。

        dest (コマンド引数 > 環境変数 LOGS_DEST_CHANNEL > "logs"):
          "logs" / "auto"          ... 専用チャンネル #twitch-logs-archives。
                                       作成できない場合は VOD本棚 へ自動フォールバック
          "archive" / "vod" / "same" ... VOD本棚 #twitch-chat-archives に同居保存
          数値                      ... そのチャンネルIDのテキストチャンネルに保存

        Discord は 1カテゴリーあたり最大50チャンネル (サーバー全体で500) なので、
        配信者ごとのチャンネルが増えると専用チャンネルを作れなくなる。その場合でも
        既存の本棚に同居させればDLを継続できるようにする。

        戻り値: (channel, note)  note はDiscordへ出す補足文 (無ければ "")
        """
        pref = (dest or os.environ.get("LOGS_DEST_CHANNEL") or "logs").strip().lower()

        # 1) チャンネルID直指定
        if pref.isdigit():
            ch = guild.get_channel(int(pref))
            # カテゴリー/ボイス等には投稿できないので弾く (TextChannel / Thread はOK)
            postable = (
                ch is not None
                and getattr(ch, "send", None) is not None
                and not isinstance(ch, (discord.CategoryChannel, discord.VoiceChannel, discord.StageChannel))
            )
            if postable:
                return ch, f"📌 保存先: 指定チャンネル {ch.mention}"
            return None, (
                f"❌ チャンネルID `{pref}` に保存できません "
                f"(このサーバーのテキストチャンネルIDを指定してください)"
            )

        # 2) VOD本棚に同居
        if pref in ("archive", "archives", "vod", "same", "vods"):
            ch = await self._get_or_create_archive_channel(guild)
            if ch:
                return ch, f"📌 保存先: VOD本棚 {ch.mention} に同居保存します"
            return None, "❌ VOD本棚チャンネルを取得・作成できませんでした。"

        # 3) 専用チャンネル (既定)。作れなければ VOD本棚 へフォールバック
        ch = await self._get_or_create_logs_archive_channel(guild)
        if ch:
            return ch, ""

        fb = await self._get_or_create_archive_channel(guild)
        if fb:
            print(
                f"[!] #{LOGS_ARCHIVE_CHANNEL_NAME} を作成できないため #{fb.name} へフォールバックします",
                file=sys.stderr,
            )
            return fb, (
                f"⚠️ 専用チャンネル `#{LOGS_ARCHIVE_CHANNEL_NAME}` を作成できませんでした "
                f"(1カテゴリー最大50チャンネル / 権限不足の可能性)\n"
                f"   → VOD本棚 {fb.mention} に同居保存します。"
                f"今後も同居でよければ `dest:archive` を指定してください"
            )
        return None, (
            "❌ 保存先チャンネルを取得・作成できませんでした。\n"
            "   原因: カテゴリーのチャンネル数上限 (1カテゴリー最大50) / Botの管理権限不足\n"
            "   対処: `dest:archive` でVOD本棚に同居させるか、`dest:<チャンネルID>` を指定してください"
        )

    # ---- Channel dedup scanning ----

    async def _scan_channel_for_uploads(
        self, channel: discord.TextChannel, force_refresh: bool = False,
    ) -> set[str]:
        cid = str(channel.id)
        if not force_refresh and cid in self._scan_cache:
            return self._scan_cache[cid]

        vod_ids: set[str] = set()
        log_ids: set[str] = set()
        count = 0
        print(f"[~] Scanning #{channel.name} ({channel.guild.name}) history (limit=None)...")

        try:
            async for msg in channel.history(limit=None):
                meta = _find_metadata_in_content(msg.content)
                if meta:
                    vod_ids.add(meta["vod_id"])
                    count += 1
                    if not self.logger.db.is_uploaded(meta["vod_id"]):
                        self.logger.db.record_upload(meta["vod_id"], {
                            "streamer_login": meta["streamer_login"],
                            "streamer_display_name": meta["streamer_display_name"],
                            "stream_title": meta.get("stream_title") or "",
                            "stream_date": meta.get("stream_date") or "",
                            "stream_start": meta.get("stream_start") or "",
                            "stream_end": meta.get("stream_end") or "",
                            "stream_duration_seconds": meta.get("stream_duration_seconds", 0),
                            "total_comments": meta.get("total_comments", 0),
                            "total_parts": meta.get("total_parts", 1),
                            "compressed_size_bytes": 0,
                            "discord_channel_id": cid,
                            "discord_message_ids": [str(msg.id)],
                            "emotes_providers": meta.get("emotes_providers", ""),
                            "is_partial": meta.get("is_partial", False),
                        })
                    continue

                # デイリーログ (zonian) の重複回避メタデータ
                log_meta = _find_log_metadata_in_content(msg.content)
                if log_meta:
                    log_ids.add(log_meta["log_id"])
                    if not self.logger.db.is_log_uploaded(log_meta["log_id"]):
                        self.logger.db.record_log_upload(log_meta["log_id"], {
                            "channel_login": log_meta["channel_login"],
                            "channel_display_name": log_meta["channel_display_name"],
                            "user_filter": log_meta["user_filter"],
                            "log_date": log_meta["log_date"],
                            "message_count": log_meta["message_count"],
                            "total_parts": log_meta["total_parts"],
                            "compressed_size_bytes": log_meta["compressed_size_bytes"],
                            "discord_channel_id": cid,
                            "discord_message_ids": [str(msg.id)],
                            "is_empty": log_meta["is_empty"],
                        })
        except discord.Forbidden:
            print(f"[!] Missing permission to read #{channel.name} history")
            return vod_ids

        self._scan_cache[cid] = vod_ids
        self._logs_scan_cache[cid] = log_ids
        print(f"[✓] Scanned #{channel.name}: {len(vod_ids)} unique VODs, {len(log_ids)} daily logs ({count} messages)")
        return vod_ids

    async def _is_log_uploaded_in_channel(self, channel: discord.TextChannel, log_id: str) -> bool:
        if self.logger.db.is_log_uploaded(log_id):
            return True
        cid = str(channel.id)
        if cid in self._logs_scan_cache:
            return log_id in self._logs_scan_cache[cid]
        await self._scan_channel_for_uploads(channel)
        return log_id in self._logs_scan_cache.get(cid, set())

    async def _is_vod_uploaded_in_channel(self, channel: discord.TextChannel, vod_id: str) -> bool:
        cid = str(channel.id)
        if self.logger.db.is_uploaded(vod_id):
            if not self.logger.db.is_partial(vod_id):
                return True
        if cid in self._scan_cache:
            return vod_id in self._scan_cache[cid]
        uploaded = await self._scan_channel_for_uploads(channel)
        return vod_id in uploaded

    async def _sync_channel_to_db(self, channel: discord.TextChannel) -> int:
        await self._scan_channel_for_uploads(channel, force_refresh=True)
        cid = str(channel.id)
        uploads = self.logger.db.get_uploads()
        return sum(1 for u in uploads if u.get("discord_channel_id") == cid)

    # ---- Batch stall monitor + Discord log shipping ----

    async def _watch_batch_stall(
        self,
        interaction: Optional[discord.Interaction],
        progress: BatchProgress,
        task: "asyncio.Task[dict]",
        status_msg: Optional[discord.Message] = None,
        *,
        warn_after: Optional[float] = None,
        abort_after: Optional[float] = None,
    ) -> None:
        warn_after = STALL_WARN_SECONDS if warn_after is None else warn_after
        abort_after = STALL_ABORT_SECONDS if abort_after is None else abort_after
        """
        進捗報告 (progress.tick) が止まっているかを監視するバックグラウンドタスク。

        - warn_after 秒 応答なし -> ステータスメッセージに警告だけ出す (処理は続行)
        - abort_after 秒 応答なし -> progress.abort_requested を立てて本体を中断させる

        本体は asyncio.to_thread 経由なので、この監視は本体が重たい圧縮で CPU を
        占有していても必ず回るのは重要 (従来は本体がループを占有してしまい、
        凍結したように見えていた)。
        """
        while not progress.finished:
            await asyncio.sleep(STALL_CHECK_INTERVAL)
            if progress.finished:
                return
            idle = progress.idle_seconds()

            if abort_after > 0 and idle >= abort_after:
                progress.abort_requested = True
                progress.note(
                    f"⛔ {int(idle)}s 進捗なし (実行中: {progress.step}) -> バッチ中断を要求"
                )
                await self._update_status(
                    interaction, status_msg,
                    f"⛔ {int(idle)}秒 応答なし (実行中: `{progress.step[:100]}`)\n"
                    f"   中断します... ログを Discord に出力します"
                )
                task.cancel()
                return

            if warn_after > 0 and idle >= warn_after and progress.warned_seq != progress.seq:
                progress.warned_seq = progress.seq
                await self._update_status(
                    interaction, status_msg,
                    f"⚠️ {int(idle)}秒 進捗なし... 実行中: `{progress.step[:100]}`\n"
                    f"   経過 {progress.elapsed_text()} (続行中 / {int(abort_after)}s で中断)"
                )

    async def _send_batch_log(
        self,
        interaction: Optional[discord.Interaction],
        progress: BatchProgress,
        *,
        channel: Optional[discord.abc.Messageable] = None,
        aborted: bool = False,
    ) -> Optional[discord.Message]:
        """
        実行ログ (.log) を Discord に投稿する。

        インタラクションのトークンは 15 分で失効するので、可能なら通常の
        チャンネル送信 (interaction.channel) を使う。失敗してもログを stdout に
        全量出力して失わないようにする。
        """
        text = progress.render(aborted=aborted)
        filename = f"{progress.slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.log"
        body = progress.message_summary(aborted=aborted)
        sent: Optional[discord.Message] = None

        target = channel or (getattr(interaction, "channel", None) if interaction else None)
        try:
            file = discord.File(io_module.BytesIO(text.encode("utf-8")), filename=filename)
            if target is not None:
                sent = await target.send(content=body, file=file)
            elif interaction is not None:
                sent = await interaction.followup.send(content=body, file=file)
            else:
                raise RuntimeError("送信先のチャンネルがありません")
        except Exception as e:  # 容量超過・権限・失効トークン etc.
            print(f"[!] {progress.title}: ログ添付に失敗 ({e}) -> 本文のみ送信します", file=sys.stderr)
            try:
                if target is not None:
                    sent = await target.send(content=f"{body}\n```\n{filename}: 添付失敗 ({e})\n```")
                elif interaction is not None:
                    sent = await interaction.followup.send(content=body)
            except Exception as e2:
                print(f"[!] {progress.title}: ログ送信自体に失敗 ({e2})", file=sys.stderr)

        # Colab コンソールにも全文 (スクロールで消えても見られるように)
        print(f"===== {progress.title} log =====\n{text}", file=sys.stderr)
        return sent

    async def _run_batch(
        self,
        interaction: Optional[discord.Interaction],
        *,
        title: str,
        body: Callable[[BatchProgress], Awaitable[dict]],
        status_msg: Optional[discord.Message] = None,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> dict:
        """
        バッチ本体 (body) を停滞監視付きで実行する。

        body は (progress) を受け取り dict を返す。step ごとに progress.tick() を
        呼ぶこと。バッチ全体が死ぬか中断された場合は、 accumulated ログを
        Discord に .log で投稿する。個々のエラーは body 内で progress.error() に
        溜めれば最後にまとめて添付される。
        """
        progress = BatchProgress(title)
        token = _ACTIVE_PROGRESS.set(progress)
        task = asyncio.create_task(self._guard_batch_body(body, progress))
        watcher = asyncio.create_task(
            self._watch_batch_stall(interaction, progress, task, status_msg)
        )
        aborted = False
        result: dict = {}
        try:
            result = await task or {}
        except asyncio.CancelledError:
            if not progress.abort_requested:
                raise  # シャットダウン等の外部キャンセルは素直に伝播させる
            aborted = True
            result = {"ok": False, "aborted": True}
            progress.error(f"⛔ {int(progress.idle_seconds())}s 進捗が止まったため中断")
        finally:
            progress.finished = True
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            _ACTIVE_PROGRESS.reset(token)

        if aborted or progress.errors or BATCH_LOG_ALWAYS:
            await self._send_batch_log(
                interaction, progress, channel=channel, aborted=aborted,
            )

        result.setdefault("progress", progress)
        result["aborted"] = aborted or result.get("aborted", False)
        return result

    async def _guard_batch_body(
        self, body: Callable[[BatchProgress], Awaitable[dict]], progress: BatchProgress,
    ) -> dict:
        """バッチ本体の想定外例外を捕まえてログに残す (Discord に見える形で)。"""
        try:
            return await body(progress) or {}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            progress.error(f"❌ バッチ処理が中断されました: {type(e).__name__}: {e}", e)
            return {"ok": False, "fatal": True, "error": str(e)}

    # ---- Status Message Safe Updater (Handles 15-min Token Expiration) ----

    async def _update_status(
        self,
        interaction: discord.Interaction,
        msg: Optional[discord.Message],
        text: str,
    ) -> Optional[discord.Message]:
        """
        メッセージ編集時のエラー（15分後のトークン失効を含む）を安全にハンドリング。
        編集できない場合はチャンネルへの新規通常メッセージ送信にフォールバックします。
        """
        text_safe = text[:2000]

        # 既存メッセージの編集を試みる
        if msg:
            try:
                await msg.edit(content=text_safe)
                return msg
            except (discord.HTTPException, discord.NotFound):
                # 15分経過による401 Unauthorizedエラー等の場合、フォールバックへ進行
                pass

        # メッセージが無い、あるいは編集失敗時はインタラクション/チャンネル経由で送信
        if interaction is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(text_safe)
                    return await interaction.original_response()
                else:
                    try:
                        return await interaction.edit_original_response(content=text_safe)
                    except (discord.HTTPException, discord.NotFound):
                        pass
            except Exception:
                pass

            # 最終フォールバック: チャンネルへの直接新規投稿
            if interaction.channel:
                try:
                    return await interaction.channel.send(text_safe)
                except Exception:
                    pass

        return None

    # ---- VOD processing ----

    async def _process_vod(
        self, interaction: discord.Interaction, vod_id: str,
        streamer_login: str, streamer_display: str, stream_title: str,
        dest_channel: discord.TextChannel,
        dedup_channel: discord.TextChannel,
        trim_begin: float | None = None, trim_end: float | None = None,
        status_msg=None, vod_index: str = "",
    ) -> bool:
        logger = self.logger
        start_time = time.time()
        loop = asyncio.get_running_loop()

        try:
            prefix = f"{vod_index} " if vod_index else ""
            status_msg = await self._update_status(interaction, status_msg, f"{prefix}⬇️ `{vod_id}` ﾁｬｯﾄDL開始...")
            _batch_tick(f"{vod_id} ﾁｬｯﾄDL開始")

            last_update = [time.time()]

            def progress_updater(latest_offset, end_offset):
                # download_and_prepare はワーカースレッドで走るため、ここでは
                # asyncio.create_task() が使えない (実行中ループが無く RuntimeError になる)。
                # ループ側へ積み替える。
                now = time.time()
                if now - last_update[0] < DL_PROGRESS_THROTTLE_SECONDS:
                    return
                last_update[0] = now
                elapsed = now - start_time
                if end_offset and end_offset > 0:
                    pct = min(latest_offset / end_offset * 100, 99)
                    remain = (end_offset - latest_offset) / max(latest_offset, 1) * elapsed if latest_offset > 0 else 0
                    text = (
                        f"{prefix}⬇️ `{vod_id}` DL中... {pct:.0f}% "
                        f"({_format_duration(int(elapsed))}経過 / "
                        f"残り約{_format_duration(int(remain))})"
                    )
                else:
                    text = (
                        f"{prefix}⬇️ `{vod_id}` DL中... "
                        f"({_format_duration(int(elapsed))}経過)"
                    )
                _batch_throttle_tick(f"{vod_id} DL中 offset={int(latest_offset)}s")
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._update_status(interaction, status_msg, text), loop
                    )
                except Exception:
                    pass

            # 重要: DL(httpx) + zstd level 22 圧縮 + 分割は数分かかる同期処理。
            # Bot のイベントループで直接実行するとハートビートごと止まり、
            # 「途中でスタックした」ように見える (Discord 側は応答無しで失敗扱い)。
            #なので必ず別スレッドで走らせる。
            metadata, is_partial = await asyncio.to_thread(
                logger.download_and_prepare,
                vod_id, streamer_login=streamer_login,
                trim_beginning=trim_begin, trim_ending=trim_end,
                progress_callback=progress_updater,
            )
            _batch_tick(
                f"{vod_id} DL完了 {metadata.get('total_comments', 0):,}件 "
                f"partial={is_partial} ({time.time() - start_time:.0f}s)"
            )

            metadata["streamer_display_name"] = (
                streamer_display or metadata.get("streamer_display_name", streamer_login)
            )
            metadata["streamer_login"] = streamer_login
            metadata["stream_title"] = stream_title or metadata.get("stream_title") or ""

            parts = metadata.get("parts", [])
            total_comments = metadata.get("total_comments", 0)
            elapsed_dl = time.time() - start_time

            if not parts and total_comments > 0:
                await self._update_status(interaction, status_msg, f"{prefix}⚠️ `{vod_id}` ﾁｬｯﾄ空")
                return False

            total_size_mb = sum(p["size"] for p in parts) / (1024 * 1024) if parts else 0.0

            dl_status = "⚠️部分" if is_partial else "✅"
            part_info = f" {len(parts)}分割" if len(parts) > 1 else ""
            
            status_text = f"{prefix}{dl_status} `{vod_id}` DL完了 "
            if total_comments == 0:
                status_text += f"(チャットなし)"
            else:
                status_text += f"({total_comments:,}ｺﾒﾝﾄ / {_format_duration(int(elapsed_dl))}){part_info}"
            
            status_msg = await self._update_status(interaction, status_msg, f"{status_text}\n📤 UP開始 -> #{dest_channel.name}...")
            _batch_tick(f"{vod_id} UP開始 ({len(parts)}part / {total_size_mb:.1f}MB)")

            message_ids = await self._send_file_parts_adaptive(
                interaction, metadata, channel=dest_channel, dedup_channel=dedup_channel, is_partial=is_partial,
                progress_prefix=prefix, status_msg=status_msg)
            _batch_tick(f"{vod_id} UP完了 ({len(message_ids or [])}msg)")

            if message_ids:
                db_meta = {
                    "streamer_id": metadata.get("streamer_id", ""),
                    "streamer_login": streamer_login,
                    "streamer_display_name": streamer_display,
                    "stream_title": stream_title or metadata.get("stream_title") or "",
                    "stream_date": metadata.get("stream_date", ""),
                    "stream_start": metadata.get("stream_start", ""),
                    "stream_end": metadata.get("stream_end", ""),
                    "stream_duration_seconds": metadata.get("stream_duration_seconds", 0),
                    "total_comments": total_comments,
                    "total_parts": len(parts),
                    "compressed_size_bytes": metadata.get("compressed_size_bytes", 0),
                    "discord_channel_id": str(dedup_channel.id),
                    "discord_message_ids": message_ids,
                    "emotes_providers": metadata.get("emotes_providers", ""),
                    "is_partial": is_partial,
                }
                logger.db.record_upload(vod_id, db_meta)

                cid = str(dedup_channel.id)
                if cid not in self._scan_cache:
                    self._scan_cache[cid] = set()
                self._scan_cache[cid].add(vod_id)

                icon = "⚠️" if is_partial else "✅"
                done_info = "完了!"
                if total_comments == 0:
                    done_info = "完了! (チャットなし、重複防止DB登録済)"
                else:
                    done_info = f"完了! {total_comments:,}ｺﾒﾝﾄ / {len(message_ids)-1}file / {total_size_mb:.1f}MB"

                await self._update_status(
                    interaction,
                    status_msg,
                    f"{prefix}{icon} `{vod_id}` {done_info}\n"
                    f"📂 保存先: {dest_channel.mention}"
                )
                return not is_partial
            else:
                _batch_note(f"❌ {vod_id} UP失敗 (添付を1件も送れませんでした)")
                await self._update_status(interaction, status_msg, f"{prefix}❌ `{vod_id}` UP失敗")
                return False

        except VODUnavailableError as e:
            # 削除済み / 期限切れ / 非公開の VOD: 失敗ではなく「スキップ」として明示
            print(f"[-] Skip {vod_id}: {e}", file=sys.stderr)
            _batch_note(f"⚠️ {vod_id} 取得不可 -> スキップ: {e}")
            await self._update_status(
                interaction, status_msg,
                f"{prefix}⚠️ `{vod_id}` 取得不可 (削除/期限切れ/非公開) -> スキップ"
            )
            return False
        except ValueError as e:
            _batch_note(f"⚠️ {vod_id} {e}")
            await self._update_status(interaction, status_msg, f"⚠️ `{vod_id}` {e}")
            return False
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] Error {vod_id}: {tb}", file=sys.stderr)
            # 実行中バッチがあればログに traceback を残す (_run_batch が最後に .log で投稿)
            _batch_error(f"❌ {vod_id} 処理中にエラー ({type(e).__name__})", e, tb)
            await self._update_status(
                interaction, status_msg,
                f"❌ `{vod_id}` {type(e).__name__}: {str(e)[:400]}\n"
                f"```\n{tb[-900:]}\n```\n(詳細は添付ログへ)"
            )
            return False

    async def _send_file_parts_adaptive(
        self, interaction: discord.Interaction, metadata: dict,
        channel: discord.TextChannel = None,
        dedup_channel: discord.TextChannel = None,
        is_partial: bool = False,
        max_retries: int = 3,
        progress_prefix: str = "", status_msg=None,
    ) -> list[str]:
        channel = channel or interaction.channel
        parts_to_send = list(metadata.get("parts", []))
        original_parts_count = len(parts_to_send)
        message_ids = []
        failed_parts = []
        thumb_url = metadata.get("thumbnail_url", "") or None
        profile_img = metadata.get("streamer_profile_image", "") or None
        split_attempts = 0

        first_archive_msg = None

        if original_parts_count == 0:
            embed = discord.Embed(
                title="📼 Twitch Chat Log (チャットなし)",
                description=(
                    f"**VOD:** {metadata['vod_id']}\n"
                    f"**配信者:** {metadata['streamer_display_name']} ({metadata['streamer_login']})\n"
                    f"**タイトル:** {(metadata.get('stream_title') or '')[:200]}\n"
                    f"**コメント数:** 0件 (チャットメッセージなし)\n"
                ),
                color=0x9146FF,
            )
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            if profile_img:
                embed.set_author(
                    name=metadata['streamer_display_name'],
                    url=f"https://www.twitch.tv/{metadata['streamer_login']}",
                    icon_url=profile_img,
                )
            embed.set_footer(text="Twitch Chat Logger")
            embed.timestamp = datetime.now()

            content_text = f"📼 **[チャットなし]** **{metadata['streamer_display_name']}** のチャットログ"
            try:
                first_archive_msg = await channel.send(content=content_text, embed=embed)
                message_ids.append(str(first_archive_msg.id))
            except Exception as e:
                print(f"[!] Failed to send no-comments embed to archives channel: {e}")
                return []

            if dedup_channel:
                try:
                    jm = json.dumps({
                        "t": "chat", "vod": metadata["vod_id"],
                        "s": metadata["streamer_login"], "sd": metadata["streamer_display_name"],
                        "title": (metadata.get("stream_title") or "")[:200],
                        "date": metadata["stream_date"],
                        "start": metadata.get("stream_start", ""),
                        "end": metadata.get("stream_end", ""),
                        "dur": metadata.get("stream_duration_seconds", 0),
                        "n": 0, "p": 0,
                        "e": "",
                        "partial": 1 if is_partial else 0,
                    }, ensure_ascii=False)
                    dedup_content = (
                        f"🔑 **[重複回避データ]** **{metadata['streamer_display_name']}** - `{metadata['vod_id']}`\n"
                        f"├ タイトル: 「{(metadata.get('stream_title') or '')[:100]}」\n"
                        f"└ コメント数: 0件\n"
                        f"{jm}"
                    )
                    dedup_msg = await dedup_channel.send(content=dedup_content)
                    message_ids.insert(0, str(dedup_msg.id))

                    embed.add_field(name="データ連携", value=f"🔗 [生JSONメタデータを確認する]({dedup_msg.jump_url})", inline=False)
                    await first_archive_msg.edit(embed=embed)
                except Exception as e:
                    print(f"[!] Failed to finalize metadata for empty comments: {e}")

            return message_ids

        while parts_to_send:
            part = parts_to_send.pop(0)
            part_num = part["part"]
            total_num = part["total"]

            sent_count = len(message_ids)
            total_to_send = original_parts_count + split_attempts
            _batch_tick(
                f"UP {sent_count}/{total_to_send}part ({part['size']/1024/1024:.1f}MB)"
            )
            status_msg = await self._update_status(interaction, status_msg,
                f"{progress_prefix}📤 UP中... {sent_count}/{total_to_send}file"
                f" ({part['size']/1024/1024:.1f}MB)")

            is_first = (len(message_ids) == 0) and part_num == 1
            content_with_meta = None

            if is_first:
                embed = discord.Embed(
                    title="📼 Twitch Chat Log",
                    description=(
                        f"**VOD:** {metadata['vod_id']}\n"
                        f"**配信者:** {metadata['streamer_display_name']} ({metadata['streamer_login']})\n"
                        f"**タイトル:** {(metadata.get('stream_title') or '')[:200]}\n"
                        f"**コメント数:** {metadata['total_comments']:,}"
                    ),
                    color=0x9146FF,
                )
                if thumb_url:
                    embed.set_thumbnail(url=thumb_url)
                if profile_img:
                    embed.set_author(
                        name=metadata['streamer_display_name'],
                        url=f"https://www.twitch.tv/{metadata['streamer_login']}",
                        icon_url=profile_img,
                    )
                s = _format_datetime(metadata.get("stream_start", ""))
                e = _format_datetime(metadata.get("stream_end", ""))
                d = metadata.get("stream_duration_seconds", 0)
                if s:
                    embed.add_field(name="配信開始", value=s, inline=True)
                if e and d > 0:
                    embed.add_field(name="配信時間", value=f"{s}～{e} ({_format_duration(d)})", inline=False)

                extras = []
                if metadata.get("emotes_providers"):
                    extras.append(f"絵文字: {metadata['emotes_providers']}")
                extras.append(f"圧縮: {metadata['compression']} (level 22) | {metadata['compressed_size_bytes']/1024:.1f}KB")
                if original_parts_count > 1:
                    extras.append(f"分割済み: {part_num}/{total_num}")
                if part["part"] != part["total"] and part["total"] > 0:
                    extras.append(f"分割済み: {part_num}/{total_num}")
                if is_partial:
                    extras.append("⚠️ 部分DL")
                if split_attempts > 0:
                    extras.append(f"🔄 再分割(試行{split_attempts}回)")
                if extras:
                    embed.add_field(name="詳細", value=" | ".join(extras), inline=False)
                embed.set_footer(text="Twitch Chat Logger")
                embed.timestamp = datetime.now()

                status_label = "⚠️ **[部分保存]**" if is_partial else "📼 **[保存完了]**"
                title_trimmed = (metadata.get("stream_title") or "")[:80]
                if len(metadata.get("stream_title") or "") > 80:
                    title_trimmed += "..."
                
                content_with_meta = (
                    f"{status_label} **{metadata['streamer_display_name']}** のチャットログ\n"
                    f"└ 「{title_trimmed}」 (コメント: {metadata['total_comments']:,}件)"
                )

            sent = False
            file_obj = discord.File(io_module.BytesIO(part["data"]), filename=part["name"])

            for attempt in range(max_retries + 1):
                try:
                    if is_first:
                        first_archive_msg = await channel.send(content=content_with_meta, embed=embed, file=file_obj)
                        message_ids.append(str(first_archive_msg.id))
                    else:
                        meta_text = f"**📼 {metadata['vod_id']} - Part {part['part']}/{metadata['total_parts']}**"
                        msg = await channel.send(content=meta_text, file=file_obj)
                        message_ids.append(str(msg.id))
                    sent = True
                    break
                except discord.HTTPException as e:
                    err_str = str(e)
                    if "Request entity too large" in err_str or "413" in err_str:
                        if attempt < max_retries:
                            split_attempts += 1
                            new_parts = self._split_part_further(part, max(attempt + 1, 2))
                            if len(new_parts) > 1:
                                await channel.send(
                                    f"🔄 Part {part_num} ({len(part['data'])/1024/1024:.1f}MB) "
                                    f"が大きすぎます→{len(new_parts)}個に再分割"
                                )
                                new_parts.reverse()
                                for np in new_parts:
                                    parts_to_send.insert(0, np)
                                sent = True
                                break
                            else:
                                await channel.send(f"❌ Part {part_num} 分割不可({len(part['data'])/1024/1024:.1f}MB). スキップ")
                                failed_parts.append(part)
                                sent = True
                                break
                        else:
                            await channel.send(f"❌ Part {part_num} 上限超過({len(part['data'])/1024/1024:.1f}MB). リトライ上限到達")
                            failed_parts.append(part)
                            sent = True
                            break
                    elif "rate" in err_str.lower() or "429" in err_str:
                        wait_time = 5.0 * (attempt + 1)
                        _batch_tick(f"⏳ ﾚｰﾄ制限 {wait_time:.0f}s 待機 (UP)")
                        await channel.send(f"⏳ ﾚｰﾄ制限: {wait_time:.0f}秒待機...")
                        await asyncio.sleep(wait_time)
                    else:
                        if attempt < max_retries:
                            await asyncio.sleep(3.0 * (attempt + 1))
                        else:
                            await channel.send(f"⚠️ Part {part_num} UP失敗({attempt+1}回): {e}")
                            failed_parts.append(part)
                            sent = True
                            break

            if not sent:
                failed_parts.append(part)

            if sent and parts_to_send:
                await asyncio.sleep(0.5)

        if failed_parts:
            await channel.send(f"⚠️ {len(failed_parts)}/{original_parts_count}パーツのアップロードに失敗しました。金庫DBへの登録を中止します。")
            return []

        if dedup_channel and first_archive_msg:
            try:
                status_label = "⚠️ **[部分保存]**" if is_partial else "🔑 **[重複回避データ]**"
                jm = json.dumps({
                    "t": "chat", "vod": metadata["vod_id"],
                    "s": metadata["streamer_login"], "sd": metadata["streamer_display_name"],
                    "title": (metadata.get("stream_title") or "")[:200],
                    "date": metadata["stream_date"],
                    "start": metadata.get("stream_start", ""),
                    "end": metadata.get("stream_end", ""),
                    "dur": metadata.get("stream_duration_seconds", 0),
                    "n": metadata["total_comments"], "p": original_parts_count,
                    "e": metadata.get("emotes_providers", ""),
                    "partial": 1 if is_partial else 0,
                }, ensure_ascii=False)

                dedup_content = (
                    f"{status_label} **{metadata['streamer_display_name']}** - `{metadata['vod_id']}`\n"
                    f"├ タイトル: 「{(metadata.get('stream_title') or '')[:100]}」\n"
                    f"├ コメント数: {metadata['total_comments']:,}件\n"
                    f"└ パート数: {original_parts_count}件\n"
                    f"{jm}"
                )
                
                dedup_msg = await dedup_channel.send(content=dedup_content)
                message_ids.insert(0, str(dedup_msg.id))

                embed.add_field(name="データ連携", value=f"🔗 [生JSONメタデータを確認する]({dedup_msg.jump_url})", inline=False)
                await first_archive_msg.edit(embed=embed)

            except Exception as e:
                print(f"[!] Failed to post metadata to dedup channel (transaction incomplete): {e}")
                return []

        return message_ids

    def _split_part_further(self, part: dict, factor: int = 2) -> list[dict]:
        data = part["data"]
        if len(data) < 100 * 1024:
            return [part]

        chunk_size = max(len(data) // factor, 50 * 1024)
        total_parts = (len(data) + chunk_size - 1) // chunk_size
        if total_parts <= 1:
            return [part]

        new_parts = []
        base_name = part["name"].rsplit(".", 2)[0]
        if ".part" in base_name:
            base_name = base_name.rsplit(".part", 1)[0]

        for i in range(total_parts):
            chunk = data[i * chunk_size:(i + 1) * chunk_size]
            new_parts.append({
                "name": f"{base_name}.sub{i + 1}_{total_parts}.zst",
                "data": chunk,
                "size": len(chunk),
                "part": i + 1,
                "total": total_parts,
            })

        return new_parts

    # ---- Daily Chat Logs (logs.zonian.dev) ----

    def _decide_empty_recheck(self, log_id: str, channel_login: str, day_str: str) -> dict:
        """
        「サーバーの記録日一覧にあるのに0件が返ってきた」日の扱いを決める。

        戻り値: {"accept": bool, "note": str}
          accept=True  ... 空の再確認が十分繰り返された -> 「ログなし」確定にしてよい
          accept=False ... まだ確定しない -> 重複DBに登録せず次回以降に再検証する
        """
        db = self.logger.db
        row = db.get_empty_check(log_id)
        if row is not None:
            try:
                last = datetime.fromisoformat(row["last_checked_at"])
                age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
            except (KeyError, TypeError, ValueError):
                age_days = EMPTY_RECHECK_DAYS
            if age_days < EMPTY_RECHECK_DAYS:
                return {
                    "accept": False,
                    "note": (
                        f"最近({age_days:.1f}日前)に確認済のため今回はスキップ "
                        f"({EMPTY_RECHECK_DAYS:g}日ごとに再チェック)"
                    ),
                }

        attempts = db.record_empty_check(log_id, channel_login, day_str)
        if attempts >= EMPTY_ACCEPT_AFTER:
            db.remove_empty_check(log_id)
            return {
                "accept": True,
                "note": f"空の再確認が{attempts}回連続 -> 「ログなし」確定として登録します",
            }
        return {
            "accept": False,
            "note": (
                f"「ログなし」とは登録せず次回以降に再検証します "
                f"(空の確認 {attempts}/{EMPTY_ACCEPT_AFTER}回・{EMPTY_RECHECK_DAYS:g}日ごと)"
            ),
        }

    async def _purge_log_records(
        self,
        dedup_channel: discord.TextChannel,
        dest_channel: discord.TextChannel,
        log_id: str,
    ) -> int:
        """
        強制再DLの前に古い記録を消去する (誤った「ログなし」登録の修正用)。

        - ローカル重複DBの行を削除
        - インメモリキャッシュから削除
        - 紐づくDiscordメッセージ (重複金庫の🔑エントリ / 本棚の📭・📅エントリ) を削除

        戻り値: 削除できたDiscordメッセージ数
        """
        deleted = 0

        row = self.logger.db.get_log_upload(log_id)
        msg_ids = list((row or {}).get("discord_message_ids") or [])
        self.logger.db.remove_log_upload(log_id)
        self.logger.db.remove_empty_check(log_id)

        cid = str(dedup_channel.id)
        cache = self._logs_scan_cache.get(cid)
        if cache is not None:
            cache.discard(log_id)

        # 探索候補チャンネル: 重複金庫 -> 保存先 -> ローカルDBに記録されたチャンネル
        candidates: list[discord.TextChannel] = [dedup_channel, dest_channel]
        recorded_cid = (row or {}).get("discord_channel_id")
        if recorded_cid:
            try:
                ch = self.get_channel(int(recorded_cid))
                if ch is not None and ch not in candidates:
                    candidates.append(ch)
            except (TypeError, ValueError):
                pass

        for mid in msg_ids:
            try:
                mid_int = int(mid)
            except (TypeError, ValueError):
                continue
            for ch in candidates:
                try:
                    msg = await ch.fetch_message(mid_int)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                except Exception:
                    break
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(0.3)  # レート制限回避
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    print(f"[!] purge delete failed ({mid}): {e}")
                break

        return deleted

    async def _process_day_log(
        self,
        interaction: discord.Interaction,
        channel_login: str,
        channel_display: str,
        channel_id: str,
        day,
        dest_channel: discord.TextChannel,
        dedup_channel: discord.TextChannel,
        user_filter: Optional[str] = None,
        status_msg=None,
        day_index: str = "",
        profile_img: Optional[str] = None,
    ) -> bool:
        """1日分(UTC)のデイリーチャットログをDL→圧縮→アップロード→重複登録する。"""
        start_time = time.time()
        prefix = f"{day_index} " if day_index else ""
        day_str = f"{day:%Y-%m-%d}"
        log_id = make_log_id(channel_login, user_filter, day_str)
        u_label = f" (user: {user_filter})" if user_filter else ""

        try:
            # 「空の再検証待ち」の日: 前回チェックから再検証日が来ていなければ、
            # サーバーに問い合わせず次回まで待機する (負荷対策)。
            ec = self.logger.db.get_empty_check(log_id)
            if ec is not None:
                try:
                    last = datetime.fromisoformat(ec["last_checked_at"])
                    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
                except (KeyError, TypeError, ValueError):
                    age_days = EMPTY_RECHECK_DAYS  # 壊れていたら即再チェック
                if age_days < EMPTY_RECHECK_DAYS:
                    attempts = int(ec.get("attempts") or 0)
                    await self._update_status(
                        interaction, status_msg,
                        f"{prefix}⏳ `{channel_login}` {day_str} 空の再検証待ち "
                        f"(確認{attempts}回/{EMPTY_ACCEPT_AFTER}回・再チェックは{EMPTY_RECHECK_DAYS:g}日ごと)"
                    )
                    return False

            status_msg = await self._update_status(
                interaction, status_msg,
                f"{prefix}⬇️ `{channel_login}` {day_str}{u_label} ﾛｸﾞDL開始..."
            )

            # 1日分のチャットを取得 (イベントループをブロックしないようスレッドへ)。
            # 記録済み日付なのに空が返ってきた場合は、fetch_day の中で再検証
            # (channelidルートへのフォールバック / 待機後の再取得) が行われ、
            # それでも空なら LogsEmptyMismatchError になる (= サーバー側の一時的不調疑い)。
            # progress_cb: バッチ実行中は巨大ファイルのDL/待機中も進捗を報告し続け、
            # 停滞監視 (ストールモニター) が誤って中断しないようにする。
            empty_mismatch_note = ""
            try:
                raw_messages = await asyncio.to_thread(
                    self.zonian.fetch_day, channel_login, day,
                    channel_id=channel_id, recorded_day=True,
                    progress_cb=_batch_tick,
                )
            except LogsEmptyMismatchError as mismatch:
                verdict = self._decide_empty_recheck(log_id, channel_login, day_str)
                if not verdict["accept"]:
                    # まだ「ログなし」確定にしない -> 重複DBに登録せず次回の再検証に回す
                    await self._update_status(
                        interaction, status_msg,
                        f"{prefix}⚠️ `{channel_login}` {day_str} {mismatch}\n"
                        f"   └ {verdict['note']}"
                    )
                    return False
                raw_messages = []
                empty_mismatch_note = "\n**備考:** 再検証で複数回0件が続いたため「ログなし」として確定"

            if user_filter:
                messages = filter_messages_by_user(raw_messages, user_filter)
            else:
                messages = raw_messages

            message_count = len(messages)
            parts: list[dict] = []

            if message_count > 0:
                document = build_day_document(
                    channel_login, channel_id, channel_display, day, messages,
                    user_filter=user_filter,
                )
                base_name = make_base_name(channel_login, day, user_filter)
                parts = await asyncio.to_thread(
                    compress_and_split, document, base_name,
                    BOT_UPLOAD_LIMIT, _batch_tick,
                )

            total_size = sum(p["size"] for p in parts)
            elapsed = time.time() - start_time

            if message_count == 0:
                # 0件の日も重複回避DBに登録する (VODの「チャットなし」と同じ方式)。
                # ただし「記録済みなのに空」の再検証で確定した場合のみ。
                # 検証未確定の空は上の LogsEmptyMismatchError 側でスキップ済み。
                embed = discord.Embed(
                    title="📅 Twitch Daily Chat Log (0件)",
                    description=(
                        f"**チャンネル:** {channel_display} ({channel_login})\n"
                        f"**日付:** {day_str} (UTC)\n"
                        f"**JST範囲:** {day_jst_range_text(day)}\n"
                        f"**メッセージ数:** 0件{u_label}\n"
                        f"**ソース:** {logs_api.LOG_SOURCE_NAME}"
                        f"{empty_mismatch_note}"
                    ),
                    color=0x9146FF,
                )
                embed.set_footer(text="Twitch Chat Logger")
                embed.timestamp = datetime.now()
                archive_msg = await dest_channel.send(
                    content=f"📭 **[ログなし]** **{channel_display}** のデイリーログ {day_str} (UTC){u_label}",
                    embed=embed,
                )
                message_ids = [str(archive_msg.id)]
                dedup_msg_id = await self._post_log_dedup_entry(
                    dedup_channel, channel_login, channel_display, channel_id,
                    day, user_filter, 0, 0, 0, archive_msg,
                )
                if dedup_msg_id:
                    message_ids.insert(0, dedup_msg_id)
            else:
                status_msg = await self._update_status(
                    interaction, status_msg,
                    f"{prefix}⬇️ `{channel_login}` {day_str} DL完了 "
                    f"({message_count:,}件 / {_format_duration(int(elapsed))})\n"
                    f"{prefix}📤 UP開始 -> #{dest_channel.name}..."
                )

                embed = discord.Embed(
                    title="📅 Twitch Daily Chat Log",
                    description=(
                        f"**チャンネル:** {channel_display} ({channel_login})\n"
                        f"**日付:** {day_str} (UTC)\n"
                        f"**JST範囲:** {day_jst_range_text(day)}\n"
                        f"**メッセージ数:** {message_count:,}{u_label}"
                    ),
                    color=0x9146FF,
                )
                if profile_img:
                    embed.set_author(
                        name=channel_display,
                        url=f"https://www.twitch.tv/{channel_login}",
                        icon_url=profile_img,
                    )
                details = [
                    f"圧縮: zstd (level {logs_api.get_zstd_level()}) | {total_size/1024:.1f}KB",
                    f"ソース: {logs_api.LOG_SOURCE_NAME}",
                ]
                if len(parts) > 1:
                    details.append(f"分割済み: {len(parts)}ファイル")
                embed.add_field(name="詳細", value=" | ".join(details), inline=False)
                embed.set_footer(text="Twitch Chat Logger")
                embed.timestamp = datetime.now()

                content_with_label = (
                    f"📅 **[保存完了]** **{channel_display}** のデイリーチャットログ\n"
                    f"└ {day_str} (UTC) / {message_count:,}件{u_label}"
                )

                message_ids = await self._send_log_parts(
                    dest_channel, parts, embed, content_with_label,
                )
                if not message_ids:
                    await self._update_status(
                        interaction, status_msg,
                        f"{prefix}❌ `{channel_login}` {day_str} UP失敗"
                    )
                    return False

                dedup_msg_id = await self._post_log_dedup_entry(
                    dedup_channel, channel_login, channel_display, channel_id,
                    day, user_filter, message_count, len(parts), total_size,
                )
                if dedup_msg_id:
                    message_ids.insert(0, dedup_msg_id)

            # ローカルDBに記録 + スキャンキャッシュ更新
            # (データが存在するDLが成功したので、空再検証の途中経過が残っていれば消す)
            self.logger.db.remove_empty_check(log_id)
            self.logger.db.record_log_upload(log_id, {
                "channel_login": channel_login,
                "channel_display_name": channel_display,
                "user_filter": user_filter or "",
                "log_date": day_str,
                "message_count": message_count,
                "total_parts": len(parts),
                "compressed_size_bytes": total_size,
                "discord_channel_id": str(dedup_channel.id),
                "discord_message_ids": message_ids,
                "is_empty": message_count == 0,
            })
            cid = str(dedup_channel.id)
            self._logs_scan_cache.setdefault(cid, set()).add(log_id)

            done_text = (
                f"完了! 0件 (ログなし、重複防止DB登録済)"
                if message_count == 0
                else f"完了! {message_count:,}件 / {len(parts)}file / {total_size/1024:.1f}KB"
            )
            await self._update_status(
                interaction, status_msg,
                f"{prefix}✅ `{channel_login}` {day_str} {done_text}\n"
                f"📂 保存先: {dest_channel.mention}"
            )
            return True

        except LogsAPIError as e:
            await self._update_status(interaction, status_msg, f"{prefix}⚠️ `{channel_login}` {day_str} {e}")
            return False
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] logs-day error {channel_login} {day_str}: {tb}", file=sys.stderr)
            await self._update_status(interaction, status_msg, f"{prefix}❌ `{channel_login}` {day_str} {e}")
            return False

    async def _send_log_parts(
        self,
        channel: discord.TextChannel,
        parts: list[dict],
        embed: discord.Embed,
        content_with_label: str,
        max_retries: int = 3,
    ) -> list[str]:
        """デイリーログの圧縮ファイルをアップロードする (リトライ/レート制限対応)。"""
        message_ids: list[str] = []
        first_archive_msg = None

        for part_idx, part in enumerate(parts):
            sent = False
            for attempt in range(max_retries + 1):
                file_obj = discord.File(io_module.BytesIO(part["data"]), filename=part["name"])
                try:
                    if part["part"] == 1:
                        first_archive_msg = await channel.send(
                            content=content_with_label, embed=embed, file=file_obj,
                        )
                        message_ids.append(str(first_archive_msg.id))
                    else:
                        label = (
                            f"**📅 {part['name'].rsplit('.', 2)[0]} - Part {part['part']}/{part['total']}**"
                        )
                        msg = await channel.send(content=label, file=file_obj)
                        message_ids.append(str(msg.id))
                    sent = True
                    break
                except discord.HTTPException as e:
                    err_str = str(e)
                    if "rate" in err_str.lower() or "429" in err_str:
                        wait_time = 5.0 * (attempt + 1)
                        _batch_tick(f"⏳ ﾚｰﾄ制限 {wait_time:.0f}s 待機 (UP)")
                        await channel.send(f"⏳ ﾚｰﾄ制限: {wait_time:.0f}秒待機...")
                        await asyncio.sleep(wait_time)
                    elif attempt < max_retries:
                        await asyncio.sleep(3.0 * (attempt + 1))
                    else:
                        print(f"[!] logs part upload failed: {e}")
                        return []
            if sent and part_idx < len(parts) - 1:
                await asyncio.sleep(0.5)

        return message_ids

    async def _post_log_dedup_entry(
        self,
        dedup_channel: discord.TextChannel,
        channel_login: str,
        channel_display: str,
        channel_id: str,
        day,
        user_filter: Optional[str],
        message_count: int,
        total_parts: int,
        total_size: int,
        archive_msg=None,
    ) -> Optional[str]:
        """重複金庫チャンネルにJSONメタデータを投稿する (VODと同じ方式)。"""
        try:
            jm = json.dumps({
                "t": "logs", "v": 1,
                "ch": channel_login.lower(),
                "cid": str(channel_id or ""),
                "sd": channel_display,
                "u": (user_filter or "").lower(),
                "d": f"{day:%Y-%m-%d}",
                "n": message_count,
                "p": total_parts,
                "z": total_size,
                "src": "zonian",
            }, ensure_ascii=False)

            u_note = f" (user: {user_filter})" if user_filter else ""
            dedup_content = (
                f"🔑 **[重複回避データ]** **{channel_display}** - `{channel_login}` `{day:%Y-%m-%d}` (UTC){u_note}\n"
                f"├ 種別: デイリーログ (logs.zonian.dev)\n"
                f"├ メッセージ数: {message_count:,}件\n"
                f"└ パート数: {total_parts}件\n"
                f"{jm}"
            )
            dedup_msg = await dedup_channel.send(content=dedup_content)

            if archive_msg is not None:
                try:
                    embed = archive_msg.embeds[0] if archive_msg.embeds else None
                    if embed:
                        e = embed.to_dict()
                        e.setdefault("fields", []).append({
                            "name": "データ連携",
                            "value": f"🔗 [生JSONメタデータを確認する]({dedup_msg.jump_url})",
                            "inline": False,
                        })
                        from_discord_embed = discord.Embed.from_dict(e)
                        await archive_msg.edit(embed=from_discord_embed)
                except Exception as edit_err:
                    print(f"[!] Failed to link dedup entry: {edit_err}")

            return str(dedup_msg.id)
        except Exception as e:
            print(f"[!] Failed to post logs metadata to dedup channel: {e}")
            return None

    # ---- Track List Management Internal Methods ----

    async def _get_track_message(self, channel: discord.TextChannel) -> discord.Message:
        cid = str(channel.id)
        if cid in self._track_msg_cache:
            cached_id = self._track_msg_cache[cid]
            try:
                msg = await channel.fetch_message(cached_id)
                if msg.content.startswith(TRACK_LIST_PREFIX):
                    return msg
            except Exception:
                pass

        best_msg = None
        async for msg in channel.history(limit=None):
            if msg.author.id == self.user.id:
                if msg.content.startswith(TRACK_LIST_PREFIX):
                    lst = self._extract_track_list_from_text(msg.content)
                    if lst:
                        self._track_msg_cache[cid] = msg.id
                        return msg
                    if best_msg is None:
                        best_msg = msg
                elif "📦 **トラッキングデータ**" in msg.content:
                    lst = self._extract_track_list_from_text(msg.content)
                    if lst:
                        self._track_msg_cache[cid] = msg.id
                        return msg

        if best_msg:
            self._track_msg_cache[cid] = best_msg.id
            return best_msg

        initial = f"{TRACK_LIST_PREFIX}\n\n（まだ登録なし）\n" + json.dumps({"t":"track_list","v":1,"list":[]}, ensure_ascii=False)
        msg = await channel.send(initial)
        self._track_msg_cache[cid] = msg.id
        return msg

    async def _save_track_list(self, channel: discord.TextChannel, lst: list[dict]):
        cid = str(channel.id)
        if cid in self._track_msg_cache:
            del self._track_msg_cache[cid]

        msg = await self._get_track_message(channel)
        logins_only = [e["login"] for e in lst]

        display = f"{TRACK_LIST_PREFIX}\n\n"
        if not lst:
            display += "（まだ登録なし）"
        else:
            display += f"**登録数:** {len(lst)}人\n"
            for i, e in enumerate(lst[:10], 1):
                display += f"  {i}. `{e['login']}`\n"
            if len(lst) > 10:
                display += f"  ...他{len(lst)-10}人"

        single_tl = "[TL]" + "|".join(logins_only) + "[/TL]"
        full = display + "\n" + single_tl

        if len(full) < 1900:
            async for m in channel.history(limit=None):
                if m.author.id == self.user.id and "[TL]" in m.content:
                    try: await m.delete()
                    except: pass
            await msg.edit(content=full)
            return

        async for m in channel.history(limit=None):
            if m.author.id == self.user.id and "[TL]" in m.content:
                try: await m.delete()
                except: pass

        CHUNK_MAX = 1500
        chunks, cur = [], []
        for name in logins_only:
            test = "[TL]" + "|".join(cur + [name]) + "[/TL]"
            if len(test) > CHUNK_MAX and cur:
                chunks.append(cur)
                cur = [name]
            else:
                cur.append(name)
        if cur:
            chunks.append(cur)

        for i, chunk in enumerate(chunks):
            tl = "[TL]" + "|".join(chunk) + "[/TL]"
            header = f"📦 **トラッキングデータ**"
            if len(chunks) > 1:
                header += f" ({i+1}/{len(chunks)})"
            await channel.send(f"{header}\n{tl}")

    async def _load_track_list(self, channel: discord.TextChannel) -> list[dict]:
        all_names = []
        try:
            async for m in channel.history(limit=None):
                if m.author.id != self.user.id or "[TL]" not in m.content:
                    continue
                txt = m.content
                pos = 0
                while True:
                    start = txt.find("[TL]", pos)
                    if start < 0: break
                    end = txt.find("[/TL]", start)
                    if end < 0: break
                    block = txt[start+4:end]
                    if block:
                        names = block.split("|")
                        all_names.extend(n.strip() for n in names if n.strip())
                    pos = end + 5

            if all_names:
                seen = set()
                unique = []
                for n in all_names:
                    if n not in seen:
                        seen.add(n)
                        unique.append({"login": n})
                return unique

        except Exception as e:
            print(f"[TRACK] _load error: {e}")
        return []

    def _extract_track_list_from_text(self, text: str) -> list[dict]:
        if not text:
            return []
        for i in range(len(text)):
            if text[i] != "{": continue
            if i > 0 and text[i-1].isalnum(): continue
            try:
                d = json.loads(text[i:])
                if isinstance(d, dict) and d.get("t") == "track_list":
                    if d.get("v") == 2 and d.get("d"):
                        return [{"login": l} for l in d["d"]]
                    if d.get("list"):
                        return [{"login": e["login"]} for e in d["list"]]
            except Exception: pass
        return []

    async def _send_safe(self, interaction: discord.Interaction, text: str):
        max_len = 1900
        lines = text.split("\n")
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > max_len:
                if chunk:
                    await interaction.followup.send(chunk)
                chunk = line
            else:
                chunk = chunk + "\n" + line if chunk else line
        if chunk:
            await interaction.followup.send(chunk)

    async def _process_add_logins(self, interaction: discord.Interaction, logins: list, tracker_channel: discord.TextChannel, from_file: bool = False):
        lst = await self._load_track_list(tracker_channel)
        added, skipped, not_found, errors = [], [], [], []

        for login in logins:
            if any(e["login"] == login for e in lst):
                skipped.append(login)
                continue
            try:
                ui = self.logger.resolve_streamer(login)
            except Exception as e:
                errors.append((login, str(e)[:60]))
                continue
            if not ui:
                not_found.append(login)
                continue

            display = ui.get("displayName", login)
            uid = ui.get("id", "")
            lst.append({"login": login, "display_name": display, "user_id": uid, "added_at": datetime.now(timezone.utc).isoformat()})
            added.append(login)

        save_success = True
        if added:
            try:
                await self._save_track_list(tracker_channel, lst)
            except Exception as e:
                save_success = False
                errors.append(("_save", str(e)[:80]))
                await interaction.followup.send(f"⚠️ 保存失敗: {e}")
                for a in added:
                    lst = [e for e in lst if e["login"] != a]

        if not added and not skipped and not not_found and not errors:
            await interaction.followup.send("⚠️ 追加できる配信者がいませんでした")
            return

        msg_parts = []
        if added and save_success: msg_parts.append(f"✅ {len(added)}人追加")
        elif added and not save_success: msg_parts.append(f"❌ {len(added)}人追加失敗(保存ｴﾗｰ)")
        if skipped: msg_parts.append(f"⏭️ {len(skipped)}人重複(既に登録済)")
        if not_found: msg_parts.append(f"❌ {len(not_found)}人不明")
        if errors: msg_parts.append(f"⚠️ {len(errors)}人ｴﾗｰ")

        summary = " / ".join(msg_parts)
        await interaction.followup.send(f"{summary}\n📋 登録数: {len(lst)}人")

        if errors:
            for login, reason in errors[:20]:
                await interaction.followup.send(f"⚠️ `{login}` → {reason}")
        if not_found:
            chunks = [not_found[i:i+15] for i in range(0, len(not_found), 15)]
            for chunk in chunks:
                await interaction.followup.send("❌ Twitch APIで見つからない配信者: " + " ".join(f"`{n}`" for n in chunk))
        if skipped:
            chunks = [skipped[i:i+20] for i in range(0, len(skipped), 20)]
            for chunk in chunks:
                await interaction.followup.send("⏭️ 重複: " + " ".join(f"`{n}`" for n in chunk))
        if added and from_file:
            chunks = [added[i:i+15] for i in range(0, len(added), 15)]
            for chunk in chunks:
                await interaction.followup.send("✅ 追加完了: " + " ".join(f"`{a}`" for a in chunk))


# ---- Slash Commands Group Definitions ----

bot_instance: Optional[TwitchChatBot] = None

# Slash command group: `/chat`
chat_group = app_commands.Group(name="chat", description="Twitch Chat Logger Commands")
track_group = app_commands.Group(name="track", description="トラッキングリストの管理コマンド", parent=chat_group)
logs_group = app_commands.Group(name="logs", description="デイリーチャットログ (logs.zonian.dev) のDL・確認", parent=chat_group)


def _parse_utc_date_param(value: str, field_label: str):
    """'YYYY-MM-DD' 形式の日付パラメータを検証して date に変換する。"""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{field_label} は `YYYY-MM-DD` 形式で指定してください (例: 2026-08-27)")


# --- /chat download ---
@chat_group.command(name="download", description="Twitch配信のチャットログをダウンロードして保管庫へ保存")
@app_commands.describe(streamer="配信者のユーザー名 (Login ID)", vod_id="特定のVOD ID (省略した場合は古い順に未保存全件を取得)")
async def slash_download(interaction: discord.Interaction, streamer: str, vod_id: Optional[str] = None):
    if not interaction.guild:
        return await interaction.response.send_message("❌ このコマンドはDiscordサーバー内でのみ実行できます。", ephemeral=True)
    
    await interaction.response.defer()
    bot = bot_instance
    logger = bot.logger
    streamer_clean = streamer.lower().strip()

    dest_channel = await bot._get_or_create_archive_channel(interaction.guild)
    dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
    if not dest_channel or not dedup_channel:
        return await interaction.followup.send("❌ チャンネルの取得・作成に失敗しました。Botの管理権限を確認してください。")

    if vod_id:
        try:
            vi = logger.get_vod_info(vod_id)
            if vi and vi.get("streamer_login"):
                streamer_clean = vi["streamer_login"]
                display = vi.get("streamer_display", streamer_clean)
                title = vi.get("title", "")
            else:
                display, title = streamer_clean, ""

            if await bot._is_vod_uploaded_in_channel(dedup_channel, vod_id):
                if not logger.db.is_partial(vod_id):
                    return await interaction.followup.send(f"⏭️ `{vod_id}` 済(重複回避) -> {dest_channel.mention}")
                else:
                    await interaction.followup.send(f"🔄 `{vod_id}` 部分DL→再DL")
                    await asyncio.sleep(0.5)

            await bot._process_vod(interaction, vod_id, streamer_clean, display, title, dest_channel=dest_channel, dedup_channel=dedup_channel)
        except Exception as e:
            await interaction.followup.send(f"❌ `{vod_id}` 失敗: {e}")
        return

    try:
        ui = logger.resolve_streamer(streamer_clean)
        if not ui:
            return await interaction.followup.send(f"❌ `{streamer_clean}` 見つかりません")

        dn = ui.get("displayName", streamer_clean)
        uid = ui.get("id", "")
        await interaction.followup.send(f"🔍 `{dn}` の全VODを取得中...")

        all_vods = logger.twitch_api.get_all_vods(streamer_clean, max_total=100, user_id=uid)
        if not all_vods:
            return await interaction.followup.send(f"⚠️ `{dn}` にVODが見つかりません")

        new_vods, already, retry = [], 0, 0
        for v in all_vods:
            if await bot._is_vod_uploaded_in_channel(dedup_channel, v["id"]):
                if logger.db.is_partial(v["id"]):
                    v["_retry"] = True
                    new_vods.append(v)
                    retry += 1
                else:
                    already += 1
            else:
                new_vods.append(v)

        if not new_vods:
            return await interaction.followup.send(f"⏭️ 未DLのVODなし (全{len(all_vods)}件中{already}件済) -> {dest_channel.mention}")

        total_new = len(new_vods)
        oldest = _format_datetime(new_vods[0].get("created_at", ""))[:10]
        newest = _format_datetime(new_vods[-1].get("created_at", ""))[:10]

        ri = f"(うち{retry}件再試行)" if retry else ""
        msg = await interaction.followup.send(
            f"📋 `{dn}` 未DL {total_new}件発見 {ri}\n"
            f"   保管先: {dest_channel.mention}\n"
            f"   期間: {oldest} ～ {newest} (古い順にDL開始)"
        )

        ok_count = 0
        for i, v in enumerate(new_vods):
            vod_idx = f"[{i+1}/{total_new}]"
            if await bot._process_vod(interaction, v["id"], streamer_clean, dn, v.get("title", ""), dest_channel=dest_channel, dedup_channel=dedup_channel, status_msg=msg, vod_index=vod_idx):
                ok_count += 1
            if i < len(new_vods) - 1:
                await asyncio.sleep(2.5)

        await interaction.channel.send(
            f"📊 `{dn}` 完了: {ok_count}件正常 / {total_new}件中 / {already}件済ｽｷｯﾌﾟ\n"
            f"📂 チャンネル: {dest_channel.mention}"
        )
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[!] download error: {tb}", file=sys.stderr)
        await interaction.followup.send(f"❌ エラー: {e}")


# --- /chat logs download / redownload ---

async def _logs_download_flow(
    interaction: discord.Interaction,
    channel: str,
    user: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    limit: Optional[int],
    force: bool,
    dest: Optional[str],
) -> None:
    """
    /chat logs download と /chat logs redownload の共通本体。

    force=True の場合は重複チェックを無視して再DLする。さらに再DLの前に
    古い記録 (重複金庫の🔑エントリ / 本棚のアーカイブ / ローカルDB行) を
    消去するため、誤って「ログなし」登録されてしまった日の修正にも使える。
    """
    bot = bot_instance

    dest_channel, dest_note = await bot._resolve_logs_dest_channel(interaction.guild, dest)
    dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
    if not dest_channel or not dedup_channel:
        return await interaction.followup.send(
            dest_note or "❌ チャンネルの取得・作成に失敗しました。Botの管理権限を確認してください。"
        )

    channel_clean = channel.lower().strip().lstrip("@")
    user_clean = (user or "").strip().lstrip("@") or None

    try:
        d_from = _parse_utc_date_param(date_from, "date_from") if date_from else None
        d_to = _parse_utc_date_param(date_to, "date_to") if date_to else None
    except ValueError as e:
        return await interaction.followup.send(f"❌ {e}")

    if d_from and d_to and d_from > d_to:
        return await interaction.followup.send("❌ date_from が date_to より未来です。")
    if limit is not None and limit < 1:
        return await interaction.followup.send("❌ limit は1以上で指定してください。")

    # 表示名の解決 (Twitch API。失敗してもzonian側でlogin解決できるので続行)
    display = channel_clean
    profile_img = None
    try:
        ui = await asyncio.to_thread(bot.logger.resolve_streamer, channel_clean)
        if ui:
            display = ui.get("displayName") or channel_clean
            if ui.get("login"):
                channel_clean = ui["login"]
            profile_img = ui.get("profileImageURL") or None
    except Exception:
        pass

    u_label = f" / ﾕｰｻﾞｰ: `{user_clean}`" if user_clean else ""
    force_label = " / 🔁 強制再DLモード (旧記録は消去されます)" if force else ""
    note_line = f"{dest_note}\n" if dest_note else ""
    status_msg = await interaction.followup.send(
        f"{note_line}🔎 `{display}` のデイリーログ情報を取得中... (ソース: logs.zonian.dev){u_label}{force_label}"
    )

    try:
        info = await asyncio.to_thread(bot.zonian.get_available_days, channel_clean, user_clean)
    except LogsAPIError as e:
        return await bot._update_status(interaction, status_msg, f"❌ {e}")
    except Exception as e:
        return await bot._update_status(interaction, status_msg, f"❌ APIエラー: {e}")

    channel_login = info["channel_login"]
    channel_id = info["channel_id"]
    all_days = info["days"]

    # 「確実に1日が終わっている」日のみ対象 (UTC基準 + 安全マージン)
    now_utc = datetime.now(timezone.utc)
    margin_h = get_safety_margin_hours()
    complete_days = [d for d in all_days if day_is_complete(d, now=now_utc, margin_hours=margin_h)]
    incomplete_days = [d for d in all_days if d not in complete_days]

    in_range = complete_days
    if d_from:
        in_range = [d for d in in_range if d >= d_from]
    if d_to:
        in_range = [d for d in in_range if d <= d_to]

    pending: list = []
    skipped = 0
    for d in in_range:
        log_id = make_log_id(channel_login, user_clean, d.isoformat())
        if not force and await bot._is_log_uploaded_in_channel(dedup_channel, log_id):
            skipped += 1
        else:
            pending.append(d)

    if not pending:
        return await bot._update_status(
            interaction,
            status_msg,
            f"⏭️ `{display}` 未DLの日なし{u_label}\n"
            f"   記録済み: {len(all_days)}日中 保存済み{skipped}日 / "
            f"未終了(対象外){len(incomplete_days)}日\n"
            f"📂 保管先: {dest_channel.mention}"
        )

    if limit is not None:
        pending = pending[:limit]

    total_new = len(pending)
    oldest, newest = pending[0], pending[-1]
    head = "🔁 強制再DL" if force else "📋 未DL"
    status_msg = await bot._update_status(
        interaction,
        status_msg,
        f"{head} `{display}` 対象 {total_new}日{u_label}\n"
        f"   保管先: {dest_channel.mention}\n"
        f"   期間: {oldest:%Y-%m-%d} ～ {newest:%Y-%m-%d} (UTC / 古い順にDL開始)\n"
        f"   ⏭️ 保存済み{skipped}日 ｽｷｯﾌﾟ / 未終了{len(incomplete_days)}日 対象外"
    )

    ok_count = 0
    purged_total = 0
    for i, d in enumerate(pending):
        day_idx = f"[{i+1}/{total_new}]"
        if force:
            # 再DLの前に古い記録 (重複金庫/本棚/ローカルDB) を消去して重複を防ぐ
            log_id = make_log_id(channel_login, user_clean, d.isoformat())
            try:
                purged_total += await bot._purge_log_records(dedup_channel, dest_channel, log_id)
            except Exception as e:
                print(f"[!] purge failed for {log_id}: {e}", file=sys.stderr)
        if await bot._process_day_log(
            interaction, channel_login, display, channel_id, d,
            dest_channel=dest_channel, dedup_channel=dedup_channel,
            user_filter=user_clean, status_msg=status_msg, day_index=day_idx,
            profile_img=profile_img,
        ):
            ok_count += 1
        if i < len(pending) - 1:
            await asyncio.sleep(2.5)

    purge_note = f" / 🧹 旧記録{purged_total}件 消去" if force else ""
    await bot._update_status(
        interaction,
        status_msg,
        f"📊 `{display}` 完了: {ok_count}日成功 / {total_new}日中{u_label}{purge_note}\n"
        f"⏭️ 保存済み{skipped}日 ｽｷｯﾌﾟ / 未終了{len(incomplete_days)}日 対象外\n"
        f"📂 チャンネル: {dest_channel.mention}"
    )


@logs_group.command(name="download", description="デイリーチャットログ(zonian)を1日ずつDL→圧縮→保管 (確定済みの日のみ)")
@app_commands.describe(
    channel="配信者のユーザー名 (Login ID)",
    user="特定ユーザーの発言のみ抽出 (省略可)",
    date_from="開始日 YYYY-MM-DD (UTC基準・省略可)",
    date_to="終了日 YYYY-MM-DD (UTC基準・省略可)",
    limit="最大DL日数 (省略時: 全件)",
    force="重複チェックを無視して強制再DL (旧記録は消去 / 省略時: 無効)",
    dest="保存先 (logs=専用/archive=VOD本棚に同居/チャンネルID)",
)
async def slash_logs_download(
    interaction: discord.Interaction,
    channel: str,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    dest: Optional[str] = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("❌ このコマンドはDiscordサーバー内でのみ実行できます。", ephemeral=True)

    await interaction.response.defer()
    await _logs_download_flow(interaction, channel, user, date_from, date_to, limit, force, dest)


@logs_group.command(
    name="redownload",
    description="重複回避DBを無視して強制再DL (誤った「ログなし」登録の修正用・旧記録は消去されます)",
)
@app_commands.describe(
    channel="配信者のユーザー名 (Login ID)",
    user="特定ユーザーの発言のみ抽出 (省略可)",
    date_from="開始日 YYYY-MM-DD (UTC基準・省略可)",
    date_to="終了日 YYYY-MM-DD (UTC基準・省略可)",
    limit="最大再DL日数 (省略時: 期間内の全件)",
    dest="保存先 (logs=専用/archive=VOD本棚に同居/チャンネルID)",
)
async def slash_logs_redownload(
    interaction: discord.Interaction,
    channel: str,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
    dest: Optional[str] = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("❌ このコマンドはDiscordサーバー内でのみ実行できます。", ephemeral=True)

    await interaction.response.defer()
    await interaction.followup.send(
        "🔁 **強制再DLモード** - 重複回避DBの記録を消去して再ダウンロードします。\n"
        "└ 古い「ログなし」登録・アーカイブメッセージも消去の対象です。"
    )
    await _logs_download_flow(interaction, channel, user, date_from, date_to, limit, True, dest)


# --- /chat logs days ---
@logs_group.command(name="days", description="デイリーログの記録日数・保存状況・DL可能日を表示")
@app_commands.describe(channel="配信者のユーザー名 (Login ID)", user="特定ユーザーで絞り込む場合 (省略可)")
async def slash_logs_days(interaction: discord.Interaction, channel: str, user: Optional[str] = None):
    if not interaction.guild:
        return await interaction.response.send_message("❌ このコマンドはDiscordサーバー内でのみ実行できます。", ephemeral=True)

    await interaction.response.defer()
    bot = bot_instance
    dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
    if not dedup_channel:
        return await interaction.followup.send("❌ 重複金庫チャンネルの取得に失敗しました。")

    channel_clean = channel.lower().strip().lstrip("@")
    user_clean = (user or "").strip().lstrip("@") or None

    status_msg = await interaction.followup.send(f"🔎 `{channel_clean}` のログ情報を取得中...")

    try:
        info = await asyncio.to_thread(bot.zonian.get_available_days, channel_clean, user_clean)
    except LogsAPIError as e:
        return await bot._update_status(interaction, status_msg, f"❌ {e}")
    except Exception as e:
        return await bot._update_status(interaction, status_msg, f"❌ APIエラー: {e}")

    channel_login = info["channel_login"]
    display = channel_login
    try:
        ui = await asyncio.to_thread(bot.logger.resolve_streamer, channel_login)
        if ui:
            display = ui.get("displayName") or channel_login
    except Exception:
        pass

    # DB + 重複金庫チャンネルから保存済みlog_idを収集
    uploaded_ids = bot.logger.db.get_uploaded_log_ids(channel_login, user_clean)
    cid = str(dedup_channel.id)
    if cid not in bot._logs_scan_cache:
        await bot._scan_channel_for_uploads(dedup_channel)
    uploaded_ids |= bot._logs_scan_cache.get(cid, set())

    def is_uploaded(d) -> bool:
        return make_log_id(channel_login, user_clean, d.isoformat()) in uploaded_ids

    all_days = info["days"]
    now_utc = datetime.now(timezone.utc)
    margin_h = get_safety_margin_hours()
    complete = [d for d in all_days if day_is_complete(d, now=now_utc, margin_hours=margin_h)]
    incomplete = [d for d in all_days if d not in complete]
    uploaded = [d for d in complete if is_uploaded(d)]
    pending = [d for d in complete if not is_uploaded(d)]

    u_label = f" / ﾕｰｻﾞｰ: `{user_clean}`" if user_clean else ""
    since = info["since"]
    lines = [
        f"**📅 `{display}` のデイリーログ情報**{u_label}",
        f"ソース: {logs_api.LOG_SOURCE_NAME} (記録インスタンス: {len(info['instances'])}台)",
        f"記録済み: **{len(all_days)}日**" + (f" ({since:%Y-%m-%d}～)" if since else ""),
        f"保存済み: {len(uploaded)}日 / **未保存(確定済): {len(pending)}日** / 未終了(対象外): {len(incomplete)}日",
    ]

    if pending:
        next_days = ", ".join(f"`{d:%Y-%m-%d}`" for d in pending[:10])
        more = f" ...他{len(pending)-10}日" if len(pending) > 10 else ""
        lines.append(f"次回DL対象 (古い順): {next_days}{more}")

    if incomplete:
        latest = incomplete[-1]
        lines.append(
            f"⏳ 未終了: {', '.join(f'`{d:%Y-%m-%d}`' for d in incomplete[-3:])} "
            f"(UTC基準のため JST では {day_jst_range_text(latest)} まで)"
        )

    lines.append(
        f"ℹ️ 1日 = UTC 00:00〜23:59 (JST+9h) / 完了判定: UTC24時 + 安全マージン{margin_h:g}時間"
    )
    lines.append(f"📥 DL: `/chat logs download channel:{channel_login}`")

    await bot._update_status(interaction, status_msg, "\n".join(lines))


# --- /chat logs scan ---
async def _collect_pending_days(
    bot: "TwitchChatBot",
    dedup_channel: discord.TextChannel,
    channel_login: str,
    days: list,
    *,
    user_filter: Optional[str] = None,
    d_from=None,
    d_to=None,
    limit: Optional[int] = None,
    force: bool = False,
) -> tuple[list, int, int]:
    """
    記録済み日のうち「確定済み / 期間内 / 未保存」の日を古い順に返す。

    戻り値: (pending, skipped, incomplete)
      pending    ... DL対象の日 (limit 適用済み)
      skipped    ... 保存済みで飛ばした日数
      incomplete ... まだ1日が終わっていない (対象外) 日数
    """
    now_utc = datetime.now(timezone.utc)
    margin_h = get_safety_margin_hours()
    complete = [d for d in days if day_is_complete(d, now=now_utc, margin_hours=margin_h)]
    incomplete = len(days) - len(complete)

    in_range = [
        d for d in complete
        if (d_from is None or d >= d_from) and (d_to is None or d <= d_to)
    ]

    pending: list = []
    skipped = 0
    for d in in_range:
        log_id = make_log_id(channel_login, user_filter, d.isoformat())
        if not force and await bot._is_log_uploaded_in_channel(dedup_channel, log_id):
            skipped += 1
        else:
            pending.append(d)

    if limit is not None:
        pending = pending[:limit]
    return pending, skipped, incomplete


async def _logs_scan_body(
    bot: "TwitchChatBot",
    interaction: discord.Interaction,
    sm: Optional[discord.Message],
    entries: list[dict],
    dest_channel: discord.TextChannel,
    dedup_channel: discord.TextChannel,
    progress: BatchProgress,
    *,
    user_filter: Optional[str] = None,
    d_from=None,
    d_to=None,
    limit: Optional[int] = None,
    force: bool = False,
) -> dict:
    """
    /chat logs scan の本体。`/chat track scan` と同じ枠組み (停滞監視 + .log 添付) で、
    トラッキングリストに登録された配信者全員のデイリーチャットログ
    (logs.zonian.dev) の未保存分を古い順にDLする。

    - zonian API / Twitch API はいずれも同期 httpx なので to_thread で逃がす
    - ログが見つからない (未記録) 配信者は警告だけ出して次へ進む
    - 個別の失敗は progress.error() に溜めて続行し、最初の失敗時にログを投稿する
    """
    total_days, total_skip, total_err = 0, 0, 0
    no_new, not_found = 0, 0
    n = len(entries)

    # 金庫チャンネルの履歴を最初に1回だけ舐めておく。これをやらないと人ごとの
    # 重複判定のたびに history(limit=None) を読み直すことになる。
    if not force:
        progress.tick("重複金庫チャンネルの履歴をスキャン中")
        try:
            await bot._scan_channel_for_uploads(dedup_channel)
        except Exception as e:
            progress.note(f"⚠️ 金庫スキャン失敗 ({type(e).__name__}: {e}) -> ローカルDBのみで重複判定")

    for i, entry in enumerate(entries):
        login = str(entry.get("login", "")).lower().strip().lstrip("@")
        if i > 0:
            await asyncio.sleep(BATCH_ITEM_GAP_SECONDS)

        progress.tick(f"({i+1}/{n}) {login}: 配信者情報を解決中")
        sm = await bot._update_status(
            interaction, sm, f"🔄 ({i+1}/{n}) `{login}` のデイリーログを確認中..."
        )

        try:
            # 表示名の解決は飾りなので、失敗しても login のまま続行する
            display, profile_img = login, None
            ui: Optional[dict] = None
            try:
                ui = await asyncio.to_thread(bot.logger.resolve_streamer, login)
            except Exception:
                ui = None
            if ui:
                display = ui.get("displayName") or login
                profile_img = ui.get("profileImageURL") or None
                if ui.get("login"):
                    login = ui["login"]

            progress.tick(f"({i+1}/{n}) {login}: 記録日一覧を取得中")
            info = await asyncio.to_thread(bot.zonian.get_available_days, login, user_filter)
            channel_id = str(info.get("channel_id") or (ui or {}).get("id") or "")

            progress.tick(f"({i+1}/{n}) {login}: 重複金庫と突合 ({info['days_count']}日)")
            pending, skipped, incomplete = await _collect_pending_days(
                bot, dedup_channel, login, info["days"],
                user_filter=user_filter, d_from=d_from, d_to=d_to,
                limit=limit, force=force,
            )
            total_skip += skipped

            if not pending:
                progress.note(
                    f"{login}: 新ログなし (記録{info['days_count']}日 / "
                    f"保存済{skipped}日 / 未終了{incomplete}日)"
                )
                no_new += 1
                continue

            progress.note(
                f"{login}: 未DL {len(pending)}日発見 (記録{info['days_count']}日 / "
                f"保存済{skipped}日 / 未終了{incomplete}日)"
            )
            await _notify(
                interaction,
                f"📋 `{login}` ({display}): 未DL **{len(pending)}日** 発見 -> {dest_channel.mention}"
            )

            for j, d in enumerate(pending):
                progress.tick(
                    f"({i+1}/{n}) {login}: {d:%Y-%m-%d} ({j+1}/{len(pending)}) DL中"
                )
                if force:
                    # 強制再DL: 古い記録 (重複金庫/本棚/ローカルDB) を消去してから再取得
                    log_id = make_log_id(login, user_filter, d.isoformat())
                    try:
                        await bot._purge_log_records(dedup_channel, dest_channel, log_id)
                    except Exception as e:
                        progress.note(f"⚠️ {login} {d:%Y-%m-%d}: 旧記録の消去に失敗 ({type(e).__name__}: {e})")
                ok = await bot._process_day_log(
                    interaction, login, display, channel_id, d,
                    dest_channel=dest_channel, dedup_channel=dedup_channel,
                    user_filter=user_filter, status_msg=sm, day_index=f"[{i+1}/{n}]",
                    profile_img=profile_img,
                )
                if ok:
                    total_days += 1
                else:
                    total_err += 1
                if j < len(pending) - 1:
                    await asyncio.sleep(BATCH_ITEM_GAP_SECONDS)

        except LogsAPIError as e:
            # まだどのインスタンスにも記録されていない etc. -> スキップして次へ
            not_found += 1
            progress.note(f"⚠️ {login}: {e}")
            await _notify(interaction, f"⚠️ `{login}` をスキップ: {e}")
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] logs scan {login}: {tb}", file=sys.stderr)
            progress.error(f"❌ {login} 処理中にエラー ({type(e).__name__})", e, tb)
            total_err += 1
            await _notify(
                interaction,
                f"❌ `{login}` 処理中にエラーが発生しました: {type(e).__name__}: {str(e)[:400]}"
            )
            # 最初の失敗時点でログを全文投稿しておく (途中で止まっても読めるように)
            if len(progress.errors) == 1:
                await bot._send_batch_log(interaction, progress, channel=interaction.channel)

    return {
        "ok": total_days, "skip": total_skip, "err": total_err, "people": n,
        "no_new": no_new, "not_found": not_found,
        "status_msg": sm,
    }


@logs_group.command(name="scan", description="トラッキングリスト全員のデイリーチャットログ(zonian)を一括DL")
@app_commands.describe(
    user="特定ユーザーの発言のみ抽出 (省略可 / 全員に適用)",
    date_from="開始日 YYYY-MM-DD (UTC基準・省略可)",
    date_to="終了日 YYYY-MM-DD (UTC基準・省略可)",
    limit="1人あたりの最大DL日数 (省略時: 全件)",
    force="重複チェックを無視して強制再DL (旧記録は消去 / 省略時: 無効)",
    dest="保存先 (logs=専用/archive=VOD本棚に同居/チャンネルID)",
)
async def slash_logs_scan(
    interaction: discord.Interaction,
    user: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    dest: Optional[str] = None,
):
    try:
        if not interaction.guild: return
        await interaction.response.defer()
        bot = bot_instance
        tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
        lst = await bot._load_track_list(tracker_channel)
        if not lst:
            return await interaction.followup.send(
                "📭 リストが空です。先に `/chat track add` で登録してください。"
            )

        user_clean = (user or "").strip().lstrip("@") or None
        try:
            d_from = _parse_utc_date_param(date_from, "date_from") if date_from else None
            d_to = _parse_utc_date_param(date_to, "date_to") if date_to else None
        except ValueError as e:
            return await interaction.followup.send(f"❌ {e}")
        if d_from and d_to and d_from > d_to:
            return await interaction.followup.send("❌ date_from が date_to より未来です。")
        if limit is not None and limit < 1:
            return await interaction.followup.send("❌ limit は1以上で指定してください。")

        dest_channel, dest_note = await bot._resolve_logs_dest_channel(interaction.guild, dest)
        dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
        if not dest_channel or not dedup_channel:
            return await interaction.followup.send(
                dest_note or "❌ 必要なチャンネルを取得できませんでした。"
            )

        u_label = f" / ﾕｰｻﾞｰ: `{user_clean}`" if user_clean else ""
        r_label = ""
        if d_from or d_to:
            r_label = f" / 期間: {d_from or '…'} 〜 {d_to or '…'}"
        l_label = f" / 上限: 1人{limit}日" if limit is not None else ""

        note_line = f"{dest_note}\n" if dest_note else ""
        sm = await interaction.followup.send(
            f"{note_line}🔄 ﾃﾞｲﾘｰﾛｸﾞｽｷｬﾝ開始 ({len(lst)}人){u_label}{r_label}{l_label} 未DLの日を探します..."
        )

        started = time.time()
        result = await bot._run_batch(
            interaction,
            title=f"logs_scan_{interaction.guild.name}",
            status_msg=sm,
            channel=interaction.channel,
            body=lambda progress: _logs_scan_body(
                bot, interaction, sm, lst, dest_channel, dedup_channel, progress,
                user_filter=user_clean, d_from=d_from, d_to=d_to,
                limit=limit, force=force,
            ),
        )
        sm = result.get("status_msg", sm)

        if result.get("aborted"):
            await bot._update_status(
                interaction, sm,
                f"⛔ ﾃﾞｲﾘｰﾛｸﾞｽｷｬﾝ中断 ({_format_duration(int(time.time() - started))}経過)\n"
                f"   進捗が止まったため打ち切りました。原因は添付ログ (.log) を確認してください。\n"
                f"   よくある原因: 巨大な1日分の圧縮 / zonian API 無応答 / Discord API の長時間レート制限\n"
                f"🆕{result.get('ok', 0)}日DL済 | ⏭️{result.get('skip', 0)}日ｽｷｯﾌﾟ\n"
                f"   📂 本棚: {dest_channel.mention} | 重複金庫: {dedup_channel.mention}"
            )
            return

        skip_note = f" / ⚠️{result.get('not_found', 0)}人 記録なし" if result.get("not_found") else ""
        err_note = f" (詳細は添付ログ)" if result.get("err") else ""
        await bot._update_status(
            interaction, sm,
            f"✅ ﾃﾞｲﾘｰﾛｸﾞｽｷｬﾝ完了! ({_format_duration(int(time.time() - started))})\n"
            f"{result.get('people', len(lst))}人処理 / 新規なし{result.get('no_new', 0)}人{skip_note}\n"
            f"🆕{result.get('ok', 0)}日DL / ⏭️{result.get('skip', 0)}日ｽｷｯﾌﾟ / ❌{result.get('err', 0)}日ｴラー{err_note}\n"
            f"📂 本棚: {dest_channel.mention} | 重複金庫: {dedup_channel.mention}"
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 起動準備 (チャンネル取得 / ﾄﾗｯｸﾘｽﾄ読込) の失敗も Discord に出す。
        tb = traceback.format_exc()
        print(f"[!] logs scan (setup): {tb}", file=sys.stderr)
        await _notify(
            interaction,
            f"❌ ﾃﾞｲﾘｰﾛｸﾞｽｷｬﾝを開始できません: {type(e).__name__}: {str(e)[:400]}\n"
            f"```\n{tb[-900:]}\n```"
        )


# --- /chat track add ---
@track_group.command(name="add", description="トラッキングリストに配信者を追加")
@app_commands.describe(streamers="配信者ID（カンマ区切りで複数指定可能。例: streamer1, streamer2）")
async def slash_track_add(interaction: discord.Interaction, streamers: str):
    if not interaction.guild: return
    await interaction.response.defer()
    bot = bot_instance
    tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
    logins = [s.strip().lower() for s in streamers.replace("、", ",").split(",") if s.strip()]
    await bot._process_add_logins(interaction, logins, tracker_channel=tracker_channel)


# --- /chat track addfile ---
@track_group.command(name="addfile", description="配信者一覧が書かれたテキストファイル(.txt)から一括追加")
@app_commands.describe(file="配信者のLogin IDが改行/カンマで記載されたテキストファイル")
async def slash_track_addfile(interaction: discord.Interaction, file: discord.Attachment):
    if not interaction.guild: return
    await interaction.response.defer()
    bot = bot_instance
    tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
    try:
        raw_content = (await file.read()).decode("utf-8")
        logins = []
        for line in raw_content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            for name in line.replace("、", ",").split(","):
                name = name.strip().lower()
                if name: logins.append(name)
        if not logins:
            return await interaction.followup.send("❌ ファイルから有効な配信者名が見つかりませんでした。")
        await bot._process_add_logins(interaction, logins, tracker_channel=tracker_channel, from_file=True)
    except Exception as e:
        await interaction.followup.send(f"❌ ファイル読込エラー: {e}")


# --- /chat track remove ---
@track_group.command(name="remove", description="トラッキングリストから配信者を削除")
@app_commands.describe(streamer="削除したい配信者のLogin ID")
async def slash_track_remove(interaction: discord.Interaction, streamer: str):
    if not interaction.guild: return
    await interaction.response.defer()
    bot = bot_instance
    tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
    login = streamer.lower().strip()
    lst = await bot._load_track_list(tracker_channel)
    new_lst = [e for e in lst if e["login"] != login]
    if len(new_lst) == len(lst):
        return await interaction.followup.send(f"❌ `{login}` はトラッキングリストに未登録です。")
    await bot._save_track_list(tracker_channel, new_lst)
    await interaction.followup.send(f"🗑️ `{login}` を削除しました！ (残り{len(new_lst)}人)")


# --- /chat track show ---
@track_group.command(name="show", description="現在トラッキング中の配信者一覧を表示")
async def slash_track_show(interaction: discord.Interaction):
    if not interaction.guild: return
    await interaction.response.defer()
    bot = bot_instance
    tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
    lst = await bot._load_track_list(tracker_channel)
    if not lst:
        return await interaction.followup.send("📭 トラッキングリストは空です。`/chat track add` で登録してください。")
    
    chunks = [lst[i:i+20] for i in range(0, len(lst), 20)]
    await interaction.followup.send(f"**📋 トラッキングリスト:** 合計 {len(lst)}人")
    for chunk in chunks:
        lines = []
        for e in chunk:
            idx = lst.index(e) + 1
            lines.append(f"{idx}. `{e['login']}`")
        await bot._send_safe(interaction, "\n".join(lines))


# --- /chat track scan ---
async def _track_scan_body(
    bot: "TwitchChatBot",
    interaction: discord.Interaction,
    sm: Optional[discord.Message],
    entries: list[dict],
    dest_channel: discord.TextChannel,
    dedup_channel: discord.TextChannel,
    tracker_channel: discord.TextChannel,
    progress: BatchProgress,
) -> dict:
    """
    /chat track scan の本体。progress を tick しながら順番に処理する。

    - 同期ブロッキング呼び出し (httpx 経由の Twitch GQL) は必ず to_thread で
      逃がす -> イベントループを占有しないのでハートビートも監視タスクも止まらない
    - 個別の失敗は progress.error() に traceback を溜めて続行し、最初の失敗時に
      ログ (.log) を Discord へ投稿する
    """
    total_new, total_skip, total_err = 0, 0, 0
    n = len(entries)

    for i, entry in enumerate(entries):
        login = entry["login"]
        if i > 0:
            await asyncio.sleep(BATCH_ITEM_GAP_SECONDS)

        progress.tick(f"({i+1}/{n}) {login}: 配信者情報を解決中")
        sm = await bot._update_status(interaction, sm, f"🔄 ({i+1}/{n}) `{login}` の情報を確認中...")

        try:
            user_id = entry.get("user_id", "")

            try:
                # resolve_streamer -> get_user_by_login -> 必要なら tracker 解決。
                # いずれも同期 httpx なのでスレッドへ逃がす。
                ui = await asyncio.to_thread(bot.logger.resolve_streamer, login)
            except Exception as api_err:
                if "rate" in str(api_err).lower() or "limit" in str(api_err).lower() or "GQL" in str(api_err):
                    wait_sec = 30
                    progress.note(f"⚠️ {login}: APIレート制限 -> {wait_sec}秒 待機して再試行")
                    sm = await bot._update_status(interaction, sm, f"⚠️ [APIレート制限回避] 待機中... **{wait_sec}秒間一時停止** します。")
                    await asyncio.sleep(wait_sec)
                    ui = await asyncio.to_thread(bot.logger.resolve_streamer, login)
                else:
                    raise api_err

            if not ui and user_id:
                ui = await asyncio.to_thread(bot.logger.resolve_streamer_by_id, user_id)
                if ui:
                    new_login = ui.get("login", "")
                    if new_login and new_login != login:
                        entry["login"] = new_login
                        entry["display_name"] = ui.get("displayName", new_login)
                        await bot._save_track_list(tracker_channel, entries)
                        login = new_login
            if not ui:
                progress.note(f"⚠️ {login}: 解決不能のためスキップ")
                await _notify(interaction, f"⚠️ `{login}` 解決不能のためスキップ")
                total_err += 1
                continue

            dn = ui.get("displayName", login)
            uid = ui.get("id", user_id)

            progress.tick(f"({i+1}/{n}) {login}: VOD一覧を取得中")
            try:
                all_vods = await asyncio.to_thread(
                    bot.logger.twitch_api.get_all_vods,
                    login=login, max_total=100, user_id=uid,
                )
            except Exception as api_err:
                if "rate" in str(api_err).lower() or "limit" in str(api_err).lower() or "GQL" in str(api_err):
                    wait_sec = 30
                    progress.note(f"⚠️ {login}: VOD一覧 APIレート制限 -> {wait_sec}秒 待機して再試行")
                    sm = await bot._update_status(interaction, sm, f"⚠️ [APIレート制限回避] 待機中... **{wait_sec}秒間一時停止** します。")
                    await asyncio.sleep(wait_sec)
                    all_vods = await asyncio.to_thread(
                        bot.logger.twitch_api.get_all_vods,
                        login=login, max_total=100, user_id=uid,
                    )
                else:
                    raise api_err

            progress.note(f"{login}: VOD {len(all_vods)}件取得 -> 金庫チャンネルと突合")
            progress.tick(f"({i+1}/{n}) {login}: 重複金庫と突合 ({len(all_vods)}件)")
            new_vods = [v for v in all_vods if not await bot._is_vod_uploaded_in_channel(dedup_channel, v["id"])]
            if not new_vods:
                progress.note(f"{login}: 新VODなし")
                total_skip += 1
                continue

            progress.note(f"{login}: 新VOD {len(new_vods)}件発見")
            await _notify(interaction, f"📋 `{login}` ({dn}): {len(new_vods)}件の新VODを発見 -> {dest_channel.mention}")
            for j, v in enumerate(new_vods):
                progress.tick(f"({i+1}/{n}) {login}: VOD {j+1}/{len(new_vods)} `{v['id']}` 処理中")
                if await bot._process_vod(interaction, v["id"], login, dn, v.get("title", ""), dest_channel=dest_channel, dedup_channel=dedup_channel, status_msg=sm, vod_index=f"[{i+1}/{n}]"):
                    total_new += 1
                await asyncio.sleep(BATCH_ITEM_GAP_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] track scan {login}: {tb}", file=sys.stderr)
            progress.error(f"❌ {login} 処理中にエラー ({type(e).__name__})", e, tb)
            total_err += 1
            await _notify(
                interaction,
                f"❌ `{login}` 処理中にエラーが発生しました: {type(e).__name__}: {str(e)[:400]}\n"
                f"```\n{tb[-900:]}\n```"
            )
            # 最初の失敗時点でログを全文投稿しておく (途中で止まっても読めるように)
            if len(progress.errors) == 1:
                await bot._send_batch_log(interaction, progress, channel=interaction.channel)

    return {
        "ok": total_new, "skip": total_skip, "err": total_err, "people": n,
        "status_msg": sm,
    }


@track_group.command(name="scan", description="トラッキングリストに登録されている全員の全未保存VODを一括自動取得")
async def slash_track_scan(interaction: discord.Interaction):
    try:
        if not interaction.guild: return
        await interaction.response.defer()
        bot = bot_instance
        tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
        lst = await bot._load_track_list(tracker_channel)
        if not lst:
            return await interaction.followup.send("📭 リストが空です。先に `/chat track add` で登録してください。")

        sm = await interaction.followup.send(f"🔄 ﾄﾗｯｷﾝｸﾞｽｷｬﾝ開始 ({len(lst)}人) 新規VODを探します...")

        dest_channel = await bot._get_or_create_archive_channel(interaction.guild)
        dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
        if not dest_channel or not dedup_channel:
            return await interaction.followup.send("❌ 必要なチャンネルを取得できませんでした。")

        started = time.time()
        result = await bot._run_batch(
            interaction,
            title=f"track_scan_{interaction.guild.name}",
            status_msg=sm,
            channel=interaction.channel,
            body=lambda progress: _track_scan_body(
                bot, interaction, sm, lst, dest_channel, dedup_channel,
                tracker_channel, progress,
            ),
        )
        sm = result.get("status_msg", sm)

        if result.get("aborted"):
            await bot._update_status(
                interaction, sm,
                f"⛔ ﾄﾗｯｷﾝｸﾞｽｷｬﾝ中断 ({_format_duration(int(time.time() - started))}経過)\n"
                f"   進捗が止まったため打ち切りました。原因は添付ログ (.log) を確認してください。\n"
                f"   よくある原因: 巨大VODの圧縮 / Twitch API 無応答 / Discord API の長時間レート制限\n"
                f"   📂 保管庫: {dest_channel.mention} | 重複金庫: {dedup_channel.mention}"
            )
            return

        err_note = f"\n❌{result['err']}件ｴラー (詳細は添付ログ)" if result.get("err") else ""
        await bot._update_status(
            interaction,
            sm,
            f"✅ ﾄﾗｯｷﾝｸﾞｽｷｬﾝ完了! ({_format_duration(int(time.time() - started))})\n"
            f"{result.get('people', len(lst))}人処理\n"
            f"🆕{result.get('ok', 0)}件DL / ⏭️{result.get('skip', 0)}件ｽｷｯﾌﾟ / ❌{result.get('err', 0)}件ｴラー{err_note}\n"
            f"📂 保管庫: {dest_channel.mention} | 重複金庫: {dedup_channel.mention}"
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 起動準備 (~チャンネル取得 / ﾄﾗｯｸﾘｽﾄ読込) の失敗も Discord に出す。
        # ここで握り潰すと Colab のログしか残らず「黙って止まった」ように見える。
        tb = traceback.format_exc()
        print(f"[!] track scan (setup): {tb}", file=sys.stderr)
        await _notify(
            interaction,
            f"❌ ﾄﾗｯｷﾝｸﾞｽｷｬﾝを開始できません: {type(e).__name__}: {str(e)[:400]}\n"
            f"```\n{tb[-900:]}\n```"
        )


# --- /chat repair ---
@chat_group.command(name="repair", description="不完全/欠落しているチャットログを自動検出し修復再取得")
@app_commands.describe(targets="対象 ('default'=本棚のみ, 'all'=サーバー全体, チャンネルID)", excludes="除外チャンネルID（カンマ区切り）")
async def slash_repair(interaction: discord.Interaction, targets: str = "default", excludes: str = ""):
    if not interaction.guild: return
    await interaction.response.defer()
    bot = bot_instance

    dest_channel = await bot._get_or_create_archive_channel(interaction.guild)
    dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
    tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)
    if not dest_channel or not dedup_channel or not tracker_channel:
        return await interaction.followup.send("❌ 必要なシステムチャンネルを取得できませんでした。")

    exclude_ids = set()
    if excludes:
        for x in excludes.replace("、", ",").split(","):
            if x.strip().isdigit(): exclude_ids.add(int(x.strip()))

    auto_exclude = {dedup_channel.id, tracker_channel.id}
    channels_to_scan = []

    targets_clean = targets.lower().strip()
    if targets_clean in ("default", "archives"):
        channels_to_scan.append(dest_channel)
    elif targets_clean == "all":
        for ch in interaction.guild.text_channels:
            if ch.id not in auto_exclude and ch.id not in exclude_ids:
                perms = ch.permissions_for(interaction.guild.me)
                if perms.read_messages and perms.read_message_history:
                    channels_to_scan.append(ch)
    else:
        id_list = [s.strip() for s in targets_clean.replace("、", ",").split(",") if s.strip()]
        for id_str in id_list:
            if not id_str.isdigit(): continue
            cid = int(id_str)
            channel = interaction.guild.get_channel(cid)
            if not channel: continue
            if isinstance(channel, discord.CategoryChannel):
                for tc in channel.text_channels:
                    if tc.id not in auto_exclude and tc.id not in exclude_ids:
                        channels_to_scan.append(tc)
            elif isinstance(channel, discord.TextChannel):
                if channel.id not in auto_exclude and channel.id not in exclude_ids:
                    channels_to_scan.append(channel)

    if not channels_to_scan:
        return await interaction.followup.send("❌ 走査対象のテキストチャンネルが見つかりませんでした。")

    status_msg = await interaction.followup.send("🔍 チャンネル整合性のチェック準備を開始します...")

    status_msg = await bot._update_status(interaction, status_msg, "📦 重複金庫内のメタデータを走査中 (limit=None)...")
    expected_vods = {}
    try:
        async for msg in dedup_channel.history(limit=None):
            meta = _find_metadata_in_content(msg.content)
            if meta:
                vod_id = meta["vod_id"]
                if vod_id not in expected_vods:
                    expected_vods[vod_id] = {
                        "total_parts": meta.get("total_parts", 1),
                        "streamer_login": meta.get("streamer_login", ""),
                        "streamer_display_name": meta.get("streamer_display_name", ""),
                        "stream_title": meta.get("stream_title", ""),
                        "is_partial": meta.get("is_partial", False),
                        "dedup_msg_id": msg.id
                    }
    except Exception as e:
        return await bot._update_status(interaction, status_msg, f"❌ 重複金庫のスキャンに失敗しました: {e}")

    actual_parts, archive_messages = {}, {}
    for idx, ch in enumerate(channels_to_scan, 1):
        status_msg = await bot._update_status(interaction, status_msg, f"📼 チャンネル走査中 ({idx}/{len(channels_to_scan)}): `#{ch.name}`...")
        try:
            async for msg in ch.history(limit=None):
                found_vod_id, part_num = None, None
                if msg.attachments:
                    for att in msg.attachments:
                        fname = att.filename
                        m_part = re.search(r"(\d+)_[a-zA-Z0-9_-]+\.part(\d+)(?:_(\d+))?\.json\.zst", fname)
                        if m_part:
                            found_vod_id, part_num = m_part.group(1), int(m_part.group(2))
                            break
                        m_sub = re.search(r"(\d+)_[a-zA-Z0-9_-]+\.sub(\d+)_(\d+)\.zst", fname)
                        if m_sub:
                            found_vod_id, part_num = m_sub.group(1), int(m_sub.group(2))
                            break
                        m_single = re.search(r"(\d+)_[a-zA-Z0-9_-]+\.json\.zst", fname)
                        if m_single:
                            found_vod_id, part_num = m_single.group(1), 1
                            break

                if not found_vod_id:
                    m_content = re.search(r"\*\*📼\s+(\d+)\s+-\s+Part\s+(\d+)/(\d+)\*\*", msg.content)
                    if m_content:
                        found_vod_id, part_num = m_content.group(1), int(m_content.group(2))
                    else:
                        if "📼 **[保存完了]**" in msg.content or "⚠️ **[部分保存]**" in msg.content or "📼 **[チャットなし]**" in msg.content:
                            m_id = re.search(r"`(\d+)`", msg.content)
                            if m_id: found_vod_id, part_num = m_id.group(1), 1

                if found_vod_id:
                    if found_vod_id not in actual_parts: actual_parts[found_vod_id] = set()
                    if part_num is not None: actual_parts[found_vod_id].add(part_num)
                    if found_vod_id not in archive_messages: archive_messages[found_vod_id] = []
                    archive_messages[found_vod_id].append((ch, msg))
        except Exception as e:
            print(f"[!] Error scanning channel {ch.name}: {e}")

    broken_vods = []
    for vod_id, info in expected_vods.items():
        total_parts = info["total_parts"]
        parts_we_have = actual_parts.get(vod_id, set())
        is_broken = False
        if total_parts > 0:
            if not set(range(1, total_parts + 1)).issubset(parts_we_have):
                is_broken = True
        else:
            if len(archive_messages.get(vod_id, [])) == 0:
                is_broken = True
        if is_broken:
            broken_vods.append((vod_id, info))

    if not broken_vods:
        return await bot._update_status(interaction, status_msg, "✨ 整合性チェック完了: 破損または不完全なログは見つかりませんでした。")

    status_msg = await bot._update_status(interaction, status_msg, f"⚠️ {len(broken_vods)} 件の不完全ログを検出。自動修復（再ダウンロード＆集約保存）を開始します...")

    result = await bot._run_batch(
        interaction,
        title=f"repair_{interaction.guild.name}",
        status_msg=status_msg,
        channel=interaction.channel,
        body=lambda progress: _repair_body(
            bot, interaction, status_msg, broken_vods, archive_messages,
            dest_channel, dedup_channel, progress,
        ),
    )
    status_msg = result.get("status_msg", status_msg)

    if result.get("aborted"):
        return await bot._update_status(
            interaction, status_msg,
            f"⛔ 修復プロセスを中断しました (進捗が止まったため)\n"
            f"   原因は Discord に投稿された添付ログ (.log) を確認してください。"
        )

    repaired_count = result.get("repaired", 0)
    await bot._update_status(
        interaction,
        status_msg,
        f"✅ 整合性修復プロセス完了しました！\n"
        f"  検査対象チャンネル数: {len(channels_to_scan)} 件\n"
        f"  検出不完全VOD数: {len(broken_vods)} 件\n"
        f"  修復完了数: {repaired_count} 件"
        + (f"\n  ❌ 失敗 {result.get('err', 0)} 件 (詳細は添付ログ)" if result.get("err") else "")
    )


async def _repair_body(
    bot: "TwitchChatBot",
    interaction: discord.Interaction,
    status_msg: Optional[discord.Message],
    broken_vods: list[tuple[str, dict]],
    archive_messages: dict,
    dest_channel: discord.TextChannel,
    dedup_channel: discord.TextChannel,
    progress: BatchProgress,
) -> dict:
    """/chat repair の再取得ループ。track scan と同じく監視 + ログ投稿付き。"""
    repaired_count = 0
    err_count = 0
    total = len(broken_vods)

    for idx, (vod_id, info) in enumerate(broken_vods, 1):
        prefix = f"🔧 [{idx}/{total}]"
        progress.tick(f"({idx}/{total}) {vod_id} 修復準備 ({info['streamer_login']})")
        status_msg = await bot._update_status(interaction, status_msg, f"{prefix} VOD `{vod_id}` ({info['streamer_display_name']}) の修復準備中...")

        for ch, old_msg in archive_messages.get(vod_id, []):
            try: await old_msg.delete()
            except Exception: pass
        try:
            dedup_msg = await dedup_channel.fetch_message(info["dedup_msg_id"])
            await dedup_msg.delete()
        except Exception: pass

        bot.logger.db.remove_upload(vod_id)
        if str(dedup_channel.id) in bot._scan_cache:
            bot._scan_cache[str(dedup_channel.id)].discard(vod_id)

        try:
            success = await bot._process_vod(
                interaction, vod_id=vod_id, streamer_login=info["streamer_login"],
                streamer_display=info["streamer_display_name"], stream_title=info["stream_title"],
                dest_channel=dest_channel, dedup_channel=dedup_channel, status_msg=status_msg, vod_index=prefix
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] repair {vod_id}: {tb}", file=sys.stderr)
            progress.error(f"❌ {vod_id} 修復中にエラー ({type(e).__name__})", e, tb)
            err_count += 1
            if len(progress.errors) == 1:
                await bot._send_batch_log(interaction, progress, channel=interaction.channel)
            success = False

        if success:
            repaired_count += 1
        await asyncio.sleep(2.5)

    return {"repaired": repaired_count, "err": err_count, "status_msg": status_msg}


# --- /chat migrate ---
@chat_group.command(name="migrate", description="過去の旧チャンネル内にある全メッセージのポインタを金庫DBへ一括移行")
@app_commands.describe(ids="カテゴリーIDまたはチャンネルID（カンマ区切り）")
async def slash_migrate(interaction: discord.Interaction, ids: str):
    if not interaction.guild: return
    await interaction.response.defer()
    bot = bot_instance

    dedup_channel = await bot._get_or_create_dedup_channel(interaction.guild)
    dest_channel = await bot._get_or_create_archive_channel(interaction.guild)
    tracker_channel = await bot._get_or_create_tracker_channel(interaction.guild)

    id_list = [s.strip() for s in ids.replace("、", ",").split(",") if s.strip()]
    status_msg = await interaction.followup.send("🔄 移行対象の旧チャンネルを検証中...")

    channels_to_scan = []
    for id_str in id_list:
        if not id_str.isdigit(): continue
        channel = interaction.guild.get_channel(int(id_str))
        if isinstance(channel, discord.CategoryChannel):
            channels_to_scan.extend(channel.text_channels)
        elif isinstance(channel, discord.TextChannel):
            channels_to_scan.append(channel)

    channels_to_scan = list(set(channels_to_scan))
    if not channels_to_scan:
        return await bot._update_status(interaction, status_msg, "❌ 有効な走査対象テキストチャンネルが見つかりませんでした。")

    status_msg = await bot._update_status(interaction, status_msg, f"🔍 合計 {len(channels_to_scan)} 個のチャンネルからデータ移行を開始します...")

    total_migrated, total_skipped = 0, 0
    for idx, old_channel in enumerate(channels_to_scan, 1):
        if old_channel.id in (dedup_channel.id, dest_channel.id, tracker_channel.id): continue
        status_msg = await bot._update_status(interaction, status_msg, f"🔄 ({idx}/{len(channels_to_scan)}) `#{old_channel.name}` の履歴をスキャン中...")

        try:
            async for msg in old_channel.history(limit=None):
                meta = _find_metadata_in_content(msg.content)
                if meta:
                    vod_id = meta["vod_id"]
                    if bot.logger.db.is_uploaded(vod_id) and not bot.logger.db.is_partial(vod_id):
                        total_skipped += 1
                        continue

                    meta_json = json.dumps({
                        "t": "chat", "vod": vod_id, "s": meta["streamer_login"],
                        "sd": meta["streamer_display_name"], "title": (meta.get("stream_title") or "")[:200],
                        "date": meta["stream_date"], "start": meta.get("stream_start", ""),
                        "end": meta.get("stream_end", ""), "dur": meta.get("stream_duration_seconds", 0),
                        "n": meta["total_comments"], "p": meta["total_parts"],
                        "e": meta.get("emotes_providers", ""), "partial": 1 if meta.get("is_partial") else 0,
                    }, ensure_ascii=False)

                    pointer_text = (
                        f"🔗 **[データ移行用ポインタ]** **{meta['streamer_display_name']}** のチャットログ\n"
                        f"├ VOD: `{vod_id}` | 元メッセージ: [旧メッセージを開く]({msg.jump_url})\n"
                        f"└ 「{(meta.get('stream_title') or '')[:100]}」 (コメント: {meta['total_comments']:,}件)\n"
                        f"{meta_json}"
                    )
                    await dedup_channel.send(content=pointer_text)

                    db_meta = {
                        "streamer_id": "", "streamer_login": meta["streamer_login"],
                        "streamer_display_name": meta["streamer_display_name"],
                        "stream_title": meta.get("stream_title") or "", "stream_date": meta["stream_date"],
                        "stream_start": meta.get("stream_start", ""), "stream_end": meta.get("stream_end", ""),
                        "stream_duration_seconds": meta.get("stream_duration_seconds", 0),
                        "total_comments": meta["total_comments"], "total_parts": meta["total_parts"],
                        "compressed_size_bytes": 0, "discord_channel_id": str(dedup_channel.id),
                        "discord_message_ids": [], "emotes_providers": meta.get("emotes_providers", ""),
                        "is_partial": meta.get("is_partial", False),
                    }
                    bot.logger.db.record_upload(vod_id, db_meta)
                    total_migrated += 1
                    await asyncio.sleep(0.3)
        except Exception as e:
            await interaction.channel.send(f"⚠️ `#{old_channel.name}` の移行中にエラー: {e}")

    await bot._update_status(
        interaction,
        status_msg,
        f"✅ 移行完了しました！\n"
        f"  走査チャンネル数: {len(channels_to_scan)} 件\n"
        f"  重複回避用金庫（{dedup_channel.mention}）への登録完了: {total_migrated} 件\n"
        f"  スキップ: {total_skipped} 件"
    )


# --- /chat list ---
@chat_group.command(name="list", description="保存済みVODログの一覧を表示")
@app_commands.describe(streamer="特定の配信者名で絞り込む場合指定 (省略可)")
async def slash_list(interaction: discord.Interaction, streamer: Optional[str] = None):
    await interaction.response.defer()
    bot = bot_instance
    await bot._scan_channel_for_uploads(interaction.channel)
    s_clean = streamer.lower().strip() if streamer else None
    uploads = bot.logger.db.get_uploads(streamer_login=s_clean, limit=50)
    if not uploads:
        return await interaction.followup.send("📭 アップロードされたVODが見つかりません。")
    
    lines = ["**📋 アップロード済みVOD:**"]
    for u in uploads[:25]:
        vid = u.get("vod_id", u.get("id", "?"))
        ds = u.get("stream_date", "")[:10] or "?"
        p = " ⚠️" if u.get("is_partial") else ""
        display = u.get("streamer_display_name") or u.get("streamer_login") or "?"
        lines.append(f"`{vid}`{p} | {display} | {ds} | {u.get('total_comments',0):,}ｺﾒﾝﾄ")
    await interaction.followup.send("\n".join(lines))


# --- /chat status ---
@chat_group.command(name="status", description="Botの動作状態およびデータベース状況を表示")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer()
    bot = bot_instance
    uptime = time.time() - bot._bot_start_time
    h, m = int(uptime // 3600), int((uptime % 3600) // 60)
    all_u = bot.logger.db.get_uploads()
    partial = sum(1 for u in all_u if u.get("is_partial"))
    all_logs = bot.logger.db.get_log_uploads(limit=1000000)
    text = (
        f"**🤖 Bot Status**\n"
        f"**稼働時間:** {h}h{m}m | **Ping:** {round(bot.latency*1000)}ms\n"
        f"**記録VOD数:** {len(all_u)}件 (部分{partial}) | **カテゴリ:** `{CATEGORY_NAME}`\n"
        f"**記録デイリーログ数:** {len(all_logs)}日分 (ソース: {logs_api.LOG_SOURCE_NAME})\n"
        f"**接続サーバー数:** {len(bot.guilds)} | **保存先パス:** `{bot.logger.data_dir}`\n"
        f"**Bot上限:** {BOT_UPLOAD_LIMIT//(1024*1024)}MB\n"
        f"**絵文字機能:** {'有効' if bot.logger.enable_emotes else '無効'}\n"
        f"**履歴スキャン:** 無制限 (limit=None)\n"
        f"**重複金庫:** `#{DEDUP_CHANNEL_NAME}` | **ログ本棚:** `#{LOGS_ARCHIVE_CHANNEL_NAME}`"
    )
    await interaction.followup.send(text)


# --- /chat sync ---
@chat_group.command(name="sync", description="現在のチャンネルのメッセージ履歴を読み込みDBを手動同期")
async def slash_sync(interaction: discord.Interaction):
    await interaction.response.defer()
    bot = bot_instance
    try:
        c = await bot._sync_channel_to_db(interaction.channel)
        await interaction.followup.send(f"✅ 同期完了！ {c} 件のデータを登録しました。")
    except Exception as e:
        await interaction.followup.send(f"❌ 同期エラー: {e}")


# --- /chat help ---
@chat_group.command(name="help", description="Botのヘルプを表示")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(DESCRIPTION, ephemeral=True)


# ---- Main entry point ----

def run_bot(token: str, data_dir: str | None = None):
    global bot_instance
    bot = TwitchChatBot()
    bot.logger = _init_logger(data_dir)
    bot_instance = bot

    # スラッシュコマンドグループを追加
    bot.tree.add_command(chat_group)

    try:
        bot.run(token, log_handler=None)
    except KeyboardInterrupt:
        print("\n[✓] Stopped")
    except discord.LoginFailure:
        print("[!] Invalid token", file=sys.stderr)
        sys.exit(1)
    finally:
        if bot.logger:
            bot.logger.close()