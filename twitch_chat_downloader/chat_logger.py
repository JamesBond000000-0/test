"""
Chat Logger - Download, compress, split, and manage Twitch chat logs.
"""

from __future__ import annotations

import io
import json
import math
import time
import zstandard as zstd
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Callable

from .gql_client import TwitchGQLClient, Comment
from .twitch_api import TwitchAPI
from .emote_service import EmoteService
from .formatters import format_json, format_html


# Botアップロード上限（Botデフォルト8MB。分割対象は7.2MB）
BOT_UPLOAD_LIMIT = 8 * 1024 * 1024
DEFAULT_MAX_UPLOAD_SIZE = BOT_UPLOAD_LIMIT
DEFAULT_MAX_RETRIES = 3
MIN_PART_SIZE = 100 * 1024  # 100KB - これ以上細かく分割しない

# Metadata version
METADATA_VERSION = "1.0"


def _ensure_dir(path: str | Path):
    Path(path).mkdir(parents=True, exist_ok=True)


class ChatLoggerDB:
    """SQLite database to track uploaded VODs."""

    def __init__(self, db_path: str | Path = "~/.twitch_chat_logger/chat_log.db"):
        db_path = Path(db_path).expanduser().resolve()
        _ensure_dir(db_path.parent)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                vod_id TEXT PRIMARY KEY,
                streamer_id TEXT,
                streamer_login TEXT,
                streamer_display_name TEXT,
                stream_title TEXT,
                stream_date TEXT,
                stream_start TEXT,
                stream_end TEXT,
                stream_duration_seconds INTEGER DEFAULT 0,
                total_comments INTEGER DEFAULT 0,
                total_parts INTEGER DEFAULT 0,
                compressed_size_bytes INTEGER DEFAULT 0,
                uploaded_at TEXT NOT NULL,
                discord_channel_id TEXT,
                discord_message_ids TEXT,
                emotes_providers TEXT,
                is_partial INTEGER DEFAULT 0
            )
        """)
        self._conn.commit()

    def is_uploaded(self, vod_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM uploads WHERE vod_id = ?", (vod_id,)
        ).fetchone()
        return row is not None

    def record_upload(self, vod_id: str, metadata: dict):
        self._conn.execute("""
            INSERT OR REPLACE INTO uploads
                (vod_id, streamer_id, streamer_login, streamer_display_name,
                 stream_title, stream_date, stream_start, stream_end,
                 stream_duration_seconds, total_comments, total_parts,
                 compressed_size_bytes, uploaded_at, discord_channel_id,
                 discord_message_ids, emotes_providers, is_partial)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            vod_id,
            metadata.get("streamer_id"),
            metadata.get("streamer_login"),
            metadata.get("streamer_display_name"),
            metadata.get("stream_title"),
            metadata.get("stream_date"),
            metadata.get("stream_start"),
            metadata.get("stream_end"),
            metadata.get("stream_duration_seconds", 0),
            metadata.get("total_comments", 0),
            metadata.get("total_parts", 0),
            metadata.get("compressed_size_bytes", 0),
            datetime.now(timezone.utc).isoformat(),
            metadata.get("discord_channel_id"),
            json.dumps(metadata.get("discord_message_ids", [])),
            metadata.get("emotes_providers"),
            1 if metadata.get("is_partial", False) else 0,
        ))
        self._conn.commit()

    def get_uploads(
        self,
        streamer_login: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if streamer_login:
            rows = self._conn.execute(
                "SELECT * FROM uploads WHERE streamer_login = ? "
                "ORDER BY uploaded_at DESC LIMIT ?",
                (streamer_login.lower(), limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM uploads ORDER BY uploaded_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        cols = [d[0] for d in self._conn.execute("PRAGMA table_info(uploads)").fetchall()]
        result = []
        for row in rows:
            d = dict(zip(cols, row))
            d["discord_message_ids"] = json.loads(d.get("discord_message_ids") or "[]")
            result.append(d)
        return result

    def remove_upload(self, vod_id: str):
        self._conn.execute("DELETE FROM uploads WHERE vod_id = ?", (vod_id,))
        self._conn.commit()

    def is_partial(self, vod_id: str) -> bool:
        row = self._conn.execute(
            "SELECT is_partial FROM uploads WHERE vod_id = ?", (vod_id,)
        ).fetchone()
        return row is not None and row[0] == 1

    def close(self):
        self._conn.close()


# Import sqlite3 here to avoid circular imports
import sqlite3


class ChatLogger:
    """
    Download, compress, split, and manage Twitch chat logs for Discord upload.
    """

    def __init__(self,
        data_dir: str | Path = "~/.twitch_chat_logger",
        max_upload_size: int = DEFAULT_MAX_UPLOAD_SIZE,
        enable_emotes: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.data_dir = Path(data_dir).expanduser().resolve()
        _ensure_dir(self.data_dir)
        self.max_upload_size = max_upload_size
        self.enable_emotes = enable_emotes
        self.max_retries = max_retries

        self.db = ChatLoggerDB(self.data_dir / "chat_log.db")
        self.twitch_api = TwitchAPI()
        self.gql_client = TwitchGQLClient()
        self.emote_service = EmoteService() if enable_emotes else None

    def resolve_streamer(self, login: str) -> dict | None:
        return self.twitch_api.resolve_user(login)

    def resolve_streamer_by_id(self, user_id: str) -> dict | None:
        return self.twitch_api.resolve_user_by_id(user_id)

    def get_available_vods(
        self,
        login: str,
        limit: int = 10,
    ) -> tuple[list[dict], list[dict]]:
        vods = self.twitch_api.get_recent_vods(login, limit=limit)
        new_vods = []
        for vod in vods:
            if self.db.is_uploaded(vod["id"]):
                vod["_already_uploaded"] = True
                if self.db.is_partial(vod["id"]):
                    vod["_already_uploaded"] = False
                    vod["_retry"] = True
                    new_vods.append(vod)
            else:
                vod["_already_uploaded"] = False
                new_vods.append(vod)
        return vods, new_vods

    def get_vod_info(self, vod_id: str) -> dict | None:
        return self.twitch_api.get_vod_info(vod_id)

    def download_and_prepare(
        self,
        vod_id: str,
        streamer_login: str = "",
        trim_beginning: float | None = None,
        trim_ending: float | None = None,
        progress_callback: Callable | None = None,
    ) -> tuple[dict, bool]:
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    wait = 5.0 * attempt
                    print(f"[~] Retry {attempt}/{self.max_retries} after {wait}s...")
                    time.sleep(wait)

                return self._download_and_prepare_impl(
                    vod_id, streamer_login, trim_beginning, trim_ending, progress_callback,
                )
            except (RuntimeError, ValueError, ConnectionError) as e:
                last_error = e
                print(f"[!] Attempt {attempt + 1} failed: {e}")
                if attempt < self.max_retries:
                    continue
                raise

        raise last_error or RuntimeError(f"Failed to download VOD {vod_id}")

    def _download_and_prepare_impl(
        self,
        vod_id: str,
        streamer_login: str = "",
        trim_beginning: float | None = None,
        trim_ending: float | None = None,
        progress_callback: Callable | None = None,
    ) -> tuple[dict, bool]:
        comments, video_created_at, channel_id, video_title, is_partial = (
            self.gql_client.download_video_chat(
                vod_id,
                trim_beginning=trim_beginning,
                trim_ending=trim_ending,
                progress_callback=progress_callback,
            )
        )

        vod_info = self.twitch_api.get_vod_info(vod_id)
        streamer_display = vod_info["streamer_display"] if vod_info else (
            comments[0].commenter.display_name if comments else streamer_login
        )
        streamer_login_final = vod_info["streamer_login"] if vod_info else streamer_login
        streamer_id_val = vod_info["streamer_id"] if vod_info else channel_id

        created_dt = (
            video_created_at
            or (vod_info["created_at"] if vod_info else None)
            or ""
        )

        stream_start = created_dt
        stream_end = ""
        stream_duration_seconds = 0
        if vod_info and vod_info.get("length_seconds"):
            stream_duration_seconds = int(vod_info["length_seconds"])
            if created_dt:
                try:
                    from datetime import timedelta
                    dt = datetime.fromisoformat(created_dt.replace("Z", "+00:00"))
                    end_dt = dt + timedelta(seconds=stream_duration_seconds)
                    stream_end = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, AttributeError):
                    pass

        stream_title = (vod_info["title"] if vod_info else video_title) or ""

        thumbnail_url = vod_info.get("thumbnail_url", "") if vod_info else ""
        streamer_profile_image = vod_info.get("streamer_profile_image", "") if vod_info else ""

        # GQLからコメントが0件だった場合の、重複データベース登録用のメタデータを構築
        if not comments:
            metadata = {
                "version": METADATA_VERSION,
                "type": "twitch_chat_log",
                "vod_id": vod_id,
                "streamer_id": streamer_id_val,
                "streamer_login": streamer_login_final,
                "streamer_display_name": streamer_display,
                "stream_title": stream_title,
                "stream_date": created_dt,
                "stream_start": stream_start,
                "stream_end": stream_end,
                "stream_duration_seconds": stream_duration_seconds,
                "total_comments": 0,
                "total_parts": 0,
                "compressed_size_bytes": 0,
                "compression": "zstd",
                "emotes_providers": "",
                "is_partial": is_partial,
                "thumbnail_url": thumbnail_url,
                "streamer_profile_image": streamer_profile_image,
                "parts": [], # アップロードファイルは無し
            }
            return metadata, is_partial

        emote_map = None
        emotes_providers = ""
        if self.enable_emotes and self.emote_service:
            cache = self.emote_service.get_emotes(
                str(streamer_id_val) if streamer_id_val else vod_id,
                channel_name=streamer_display,
            )
            emote_map = cache.get_emote_map()
            providers = sorted({e.provider for e in emote_map.values()})
            emotes_providers = ",".join(providers)

        # 本番のデータ圧縮。Zstd (最高レベル 22) を指定
        json_data = format_json(
            comments,
            video_created_at=video_created_at,
            channel_id=channel_id,
            streamer_name=streamer_display,
            streamer_login=streamer_login_final,
            video_id=vod_id,
            compression="Zstd",
            emote_map=emote_map if emote_map else None,
        )

        parts = self._split_data_safe(json_data, base_name=f"{vod_id}_{streamer_login_final}")

        metadata = {
            "version": METADATA_VERSION,
            "type": "twitch_chat_log",
            "vod_id": vod_id,
            "streamer_id": streamer_id_val,
            "streamer_login": streamer_login_final,
            "streamer_display_name": streamer_display,
            "stream_title": stream_title,
            "stream_date": created_dt,
            "stream_start": stream_start,
            "stream_end": stream_end,
            "stream_duration_seconds": stream_duration_seconds,
            "total_comments": len(comments),
            "total_parts": len(parts),
            "compressed_size_bytes": sum(len(p["data"]) for p in parts),
            "compression": "zstd",
            "emotes_providers": emotes_providers,
            "is_partial": is_partial,
            "thumbnail_url": thumbnail_url,
            "streamer_profile_image": streamer_profile_image,
            "parts": parts,
        }

        return metadata, is_partial

    def _split_data_safe(self, data: bytes, base_name: str, max_retries: int = 3) -> list[dict]:
        target_size = int(self.max_upload_size * 0.9)

        if len(data) <= target_size:
            return [{
                "name": f"{base_name}.json.zst", # 拡張子を .zst に変更
                "data": data,
                "size": len(data),
                "part": 1,
                "total": 1,
            }]

        # Zstd で展開 (Decompress)
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(data)
        root = json.loads(decompressed.decode("utf-8"))
        all_comments = root.get("comments", [])
        total_comments = len(all_comments)

        if total_comments == 0:
            return [{
                "name": f"{base_name}.json.zst",
                "data": data,
                "size": len(data),
                "part": 1,
                "total": 1,
            }]

        avg_comment_size = len(decompressed) / total_comments
        target_per_part = max(int(target_size / avg_comment_size), 1)

        for attempt in range(max_retries):
            try:
                return self._build_parts(root, all_comments, target_per_part, base_name)
            except ValueError:
                target_per_part = max(target_per_part // 2, 1)

        return self._split_raw_chunks(data, base_name, base_name)

    def _build_parts(self, root: dict, all_comments: list, comments_per_part: int, base_name: str) -> list[dict]:
        parts = []
        total = len(all_comments)
        
        # Zstd最高圧縮レベル22を各分割ファイルに適用
        cctx = zstd.ZstdCompressor(level=22, write_checksum=True)

        for i in range(0, total, comments_per_part):
            chunk = all_comments[i:i + comments_per_part]
            part_root = dict(root)
            part_root["comments"] = chunk
            part_root["_split"] = {
                "part": len(parts) + 1,
                "total_parts": None,
                "original_vod_id": root.get("video", {}).get("id", ""),
            }

            part_json = json.dumps(part_root, ensure_ascii=False, indent=2).encode("utf-8")
            compressed = cctx.compress(part_json)

            if len(compressed) > self.max_upload_size:
                raise ValueError(f"Part too large: {len(compressed)} > {self.max_upload_size}")

            parts.append({
                "name": f"{base_name}.part{len(parts) + 1}.json.zst", # 拡張子を .zst に変更
                "data": compressed,
                "size": len(compressed),
                "part": len(parts) + 1,
                "total": 0,
            })

        total_parts = len(parts)
        for p in parts:
            p["total"] = total_parts
            if total_parts > 1:
                p["name"] = f"{base_name}.part{p['part']}_{total_parts}.json.zst"

        return parts

    def _split_raw_chunks(self, data: bytes, base_name: str, original_name: str) -> list[dict]:
        max_safe = int(self.max_upload_size * 0.85)
        parts = []
        total_parts = math.ceil(len(data) / max_safe)

        for i in range(total_parts):
            chunk = data[i * max_safe:(i + 1) * max_safe]
            parts.append({
                "name": f"{base_name}.chunk{i + 1}_{total_parts}.zst", # 拡張子を .zst に変更
                "data": chunk,
                "size": len(chunk),
                "part": i + 1,
                "total": total_parts,
            })

        return parts

    def build_metadata_message(self, info: dict) -> str:
        meta = {
            "v": METADATA_VERSION,
            "t": "chat",
            "vod": info["vod_id"],
            "s": info["streamer_login"],
            "sd": info["streamer_display_name"],
            "title": info["stream_title"][:200],
            "date": info["stream_date"],
            "start": info.get("stream_start", ""),
            "end": info.get("stream_end", ""),
            "dur": info.get("stream_duration_seconds", 0),
            "n": info["total_comments"],
            "p": info["total_parts"],
            "e": info.get("emotes_providers", ""),
            "partial": 1 if info.get("is_partial", False) else 0,
        }
        return json.dumps(meta, ensure_ascii=False)

    def parse_metadata_message(self, text: str) -> dict | None:
        try:
            meta = json.loads(text)
            if meta.get("t") == "chat" and meta.get("vod"):
                return {
                    "vod_id": meta["vod"],
                    "streamer_login": meta.get("s", ""),
                    "streamer_display_name": meta.get("sd", ""),
                    "stream_title": meta.get("title", ""),
                    "stream_date": meta.get("date", ""),
                    "stream_start": meta.get("stream_start", ""),
                    "stream_end": meta.get("stream_end", ""),
                    "stream_duration_seconds": meta.get("dur", 0),
                    "total_comments": meta.get("n", 0),
                    "total_parts": meta.get("p", 1),
                    "emotes_providers": meta.get("e", ""),
                    "is_partial": meta.get("partial", 0) == 1,
                }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return None

    def close(self):
        self.gql_client.close()
        self.twitch_api.close()
        if self.emote_service:
            self.emote_service.close()
        self.db.close()