"""
Discord Bot for archiving Twitch chat logs using Native Slash Commands (`/chat`).

Commands:
  /chat download <streamer> [vod_id]  - Download chats to archive and record metadata
  /chat track add/addfile/remove/show/scan   - Tracking list management
  /chat migrate <category_ids_or_channel_ids> - Migrate old data to the dedup channel
  /chat repair [targets] [excludes]  - Scan and auto-repair incomplete chat files
  /chat status / list / sync / help  - Other
"""

from __future__ import annotations

import asyncio
import io as io_module
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .chat_logger import ChatLogger, BOT_UPLOAD_LIMIT


# ---- Constants ----

CATEGORY_NAME = "Twitch Archives"             # 自動生成されるカテゴリー名
TRACKER_CHANNEL_NAME = "twitch-chat-tracker"  # トラッキングリスト管理用
ARCHIVE_CHANNEL_NAME = "twitch-chat-archives"  # 保存用（Embed & ファイルのみの美しい本棚）
DEDUP_CHANNEL_NAME = "twitch-chat-dedup"      # 重複判定記録用（Botの隠しJSONデータベース）
TRACK_LIST_PREFIX = "📋 **Twitch配信者トラッキングリスト**"

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
    "`/chat list [streamer]` - アップロード済みVOD一覧\n"
    "`/chat status` - Botステータス確認\n"
    "`/chat sync` - データベース手動同期\n"
    "`/chat help` - ヘルプを表示\n\n"
    f"📌 **整理機能:** ログ本棚は `#{ARCHIVE_CHANNEL_NAME}`、回避用金庫は `#{DEDUP_CHANNEL_NAME}` に保存されます\n"
    f"📌 **データ整合性:** 履歴スキャンの遡りリミットは完全にオフ（無制限）です"
)


# ---- Helpers ----

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

    # ---- Channel dedup scanning ----

    async def _scan_channel_for_uploads(
        self, channel: discord.TextChannel, force_refresh: bool = False,
    ) -> set[str]:
        cid = str(channel.id)
        if not force_refresh and cid in self._scan_cache:
            return self._scan_cache[cid]

        vod_ids: set[str] = set()
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
        except discord.Forbidden:
            print(f"[!] Missing permission to read #{channel.name} history")
            return vod_ids

        self._scan_cache[cid] = vod_ids
        print(f"[✓] Scanned #{channel.name}: {len(vod_ids)} unique VODs ({count} messages)")
        return vod_ids

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

        try:
            prefix = f"{vod_index} " if vod_index else ""
            status_msg = await self._update_status(interaction, status_msg, f"{prefix}⬇️ `{vod_id}` ﾁｬｯﾄDL開始...")

            last_update = [time.time()]

            def progress_updater(latest_offset, end_offset):
                now = time.time()
                if now - last_update[0] < 3.0:
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
                try:
                    asyncio.create_task(self._update_status(interaction, status_msg, text))
                except Exception:
                    pass

            metadata, is_partial = logger.download_and_prepare(
                vod_id, streamer_login=streamer_login,
                trim_beginning=trim_begin, trim_ending=trim_end,
                progress_callback=progress_updater,
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

            message_ids = await self._send_file_parts_adaptive(
                interaction, metadata, channel=dest_channel, dedup_channel=dedup_channel, is_partial=is_partial,
                progress_prefix=prefix, status_msg=status_msg)

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
                await self._update_status(interaction, status_msg, f"{prefix}❌ `{vod_id}` UP失敗")
                return False

        except ValueError as e:
            await self._update_status(interaction, status_msg, f"⚠️ `{vod_id}` {e}")
            return False
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] Error {vod_id}: {tb}", file=sys.stderr)
            await self._update_status(interaction, status_msg, f"❌ `{vod_id}` {e}")
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
@track_group.command(name="scan", description="トラッキングリストに登録されている全員の全未保存VODを一括自動取得")
async def slash_track_scan(interaction: discord.Interaction):
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

    total_new, total_skip, total_err = 0, 0, 0
    for i, entry in enumerate(lst):
        login = entry["login"]
        if i > 0:
            await asyncio.sleep(3.0)

        try:
            sm = await bot._update_status(interaction, sm, f"🔄 ({i+1}/{len(lst)}) `{login}` の情報を確認中...")
            user_id = entry.get("user_id", "")
            
            ui = None
            try:
                ui = bot.logger.resolve_streamer(login)
            except Exception as api_err:
                if "rate" in str(api_err).lower() or "limit" in str(api_err).lower() or "GQL" in str(api_err):
                    wait_sec = 30
                    sm = await bot._update_status(interaction, sm, f"⚠️ [APIレート制限回避] 待機中... **{wait_sec}秒間一時停止** します。")
                    await asyncio.sleep(wait_sec)
                    ui = bot.logger.resolve_streamer(login)
                else:
                    raise api_err

            if not ui and user_id:
                ui = bot.logger.resolve_streamer_by_id(user_id)
                if ui:
                    new_login = ui.get("login", "")
                    if new_login and new_login != login:
                        entry["login"] = new_login
                        entry["display_name"] = ui.get("displayName", new_login)
                        await bot._save_track_list(tracker_channel, lst)
                        login = new_login
            if not ui:
                await interaction.channel.send(f"⚠️ `{login}` 解決不能のためスキップ")
                total_err += 1
                continue

            dn = ui.get("displayName", login)
            uid = ui.get("id", user_id)

            try:
                all_vods = bot.logger.twitch_api.get_all_vods(login, max_total=100, user_id=uid)
            except Exception as api_err:
                if "rate" in str(api_err).lower() or "limit" in str(api_err).lower() or "GQL" in str(api_err):
                    wait_sec = 30
                    sm = await bot._update_status(interaction, sm, f"⚠️ [APIレート制限回避] 待機中... **{wait_sec}秒間一時停止** します。")
                    await asyncio.sleep(wait_sec)
                    all_vods = bot.logger.twitch_api.get_all_vods(login, max_total=100, user_id=uid)
                else:
                    raise api_err

            new_vods = [v for v in all_vods if not await bot._is_vod_uploaded_in_channel(dedup_channel, v["id"])]
            if not new_vods:
                total_skip += 1
                continue

            await interaction.channel.send(f"📋 `{login}` ({dn}): {len(new_vods)}件の新VODを発見 -> {dest_channel.mention}")
            for v in new_vods:
                if await bot._process_vod(interaction, v["id"], login, dn, v.get("title", ""), dest_channel=dest_channel, dedup_channel=dedup_channel):
                    total_new += 1
                await asyncio.sleep(3.0)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[!] track scan {login}: {tb}", file=sys.stderr)
            await interaction.channel.send(f"❌ `{login}` 処理中にエラーが発生しました: {e}")
            total_err += 1

    await bot._update_status(
        interaction,
        sm,
        f"✅ ﾄﾗｯｷﾝｸﾞｽｷｬﾝ完了!\n"
        f"{len(lst)}人処理\n"
        f"🆕{total_new}件DL / ⏭️{total_skip}件ｽｷｯﾌﾟ / ❌{total_err}件ｴラー\n"
        f"📂 保管庫: {dest_channel.mention} | 重複金庫: {dedup_channel.mention}"
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

    repaired_count = 0
    for idx, (vod_id, info) in enumerate(broken_vods, 1):
        prefix = f"🔧 [{idx}/{len(broken_vods)}]"
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

        success = await bot._process_vod(
            interaction, vod_id=vod_id, streamer_login=info["streamer_login"],
            streamer_display=info["streamer_display_name"], stream_title=info["stream_title"],
            dest_channel=dest_channel, dedup_channel=dedup_channel, status_msg=status_msg, vod_index=prefix
        )
        if success: repaired_count += 1
        await asyncio.sleep(2.5)

    await bot._update_status(
        interaction,
        status_msg,
        f"✅ 整合性修復プロセス完了しました！\n"
        f"  検査対象チャンネル数: {len(channels_to_scan)} 件\n"
        f"  検出不完全VOD数: {len(broken_vods)} 件\n"
        f"  修復完了数: {repaired_count} 件"
    )


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
    text = (
        f"**🤖 Bot Status**\n"
        f"**稼働時間:** {h}h{m}m | **Ping:** {round(bot.latency*1000)}ms\n"
        f"**記録VOD数:** {len(all_u)}件 (部分{partial}) | **カテゴリ:** `{CATEGORY_NAME}`\n"
        f"**接続サーバー数:** {len(bot.guilds)} | **保存先パス:** `{bot.logger.data_dir}`\n"
        f"**Bot上限:** {BOT_UPLOAD_LIMIT//(1024*1024)}MB\n"
        f"**絵文字機能:** {'有効' if bot.logger.enable_emotes else '無効'}\n"
        f"**履歴スキャン:** 無制限 (limit=None)\n"
        f"**重複金庫:** `#{DEDUP_CHANNEL_NAME}`"
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