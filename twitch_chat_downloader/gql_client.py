"""
Twitch GQL API client for downloading chat comments from VODs and Clips.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"

# Persisted query hash for VideoCommentsByOffsetOrCursor
VIDEO_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

# Persisted query hash for ClipComments (for clip chat download)
CLIP_COMMENTS_HASH = "fbf5ee0b7d150c33c77cb5c5e072ee1cc3c18f9b6e6861c56e4a35918ed96482"

# These HTTP statuses mean the request itself was rejected (bad/unknown VOD,
# stale persisted-query hash, ...). Re-sending the identical body cannot fix
# them, so they must not go through the retry loop.
PERMANENT_HTTP_STATUS = frozenset({400, 404, 422})
# ...but still give a request one cheap second chance before declaring it dead.
PERMANENT_HTTP_RETRIES = 1


class VODUnavailableError(Exception):
    """
    VOD が取得できない (削除済み / 保管期限切れ / 非公開 /  BAN 済みチャンネル)。

    一時的な通信エラーと区別するための例外で、再試行しても復旧しないため
    リトライ対象から除外する。
    """

    retryable = False


def gql_error_message(payload: Any) -> Optional[str]:
    """Pull the first GraphQL error message out of a response body, if any."""
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not errors:
        return None
    if not isinstance(errors, list):
        return str(errors)
    first = errors[0] if errors else None
    if first is None:
        return None
    if isinstance(first, dict):
        return str(first.get("message") or first)
    return str(first)


@dataclass
class Commenter:
    _id: str
    display_name: str
    name: str


@dataclass
class Emoticon:
    emoticon_id: str


@dataclass
class Fragment:
    text: str
    emoticon: Optional[Emoticon] = None


@dataclass
class Emoticon2:
    _id: str
    begin: int
    end: int = 0


@dataclass
class UserBadge:
    _id: str
    version: str


@dataclass
class Message:
    body: str = ""
    fragments: list[Fragment] = field(default_factory=list)
    emoticons: list[Emoticon2] = field(default_factory=list)
    user_badges: list[UserBadge] = field(default_factory=list)
    user_color: Optional[str] = None
    bits_spent: int = 0


@dataclass
class Comment:
    _id: str
    created_at: str
    channel_id: str
    content_type: str
    content_id: str
    content_offset_seconds: float
    commenter: Commenter
    message: Message


@dataclass
class FileInfo:
    version: str = "1.0.0"


@dataclass
class Streamer:
    name: Optional[str] = None
    login: Optional[str] = None
    id: Optional[int] = None


@dataclass
class Video:
    id: Optional[str] = None
    start: int = 0
    end: int = 0
    length: float = 0.0
    created_at: Optional[str] = None


@dataclass
class ChatRoot:
    file_info: Optional[FileInfo] = None
    streamer: Optional[Streamer] = None
    video: Optional[Video] = None
    comments: list[Comment] = field(default_factory=list)
    embedded_data: Optional[Any] = None


class TwitchGQLClient:
    """Client for Twitch's GraphQL API to fetch chat comments."""

    def __init__(self, client_id: str = TWITCH_CLIENT_ID):
        self.client = httpx.Client(
            headers={"Client-ID": client_id},
            timeout=30.0,
        )

    def _make_payload(self, video_id: str, cursor: Optional[str] = None,
                      offset_seconds: Optional[float] = None) -> tuple[dict, str]:
        variables: dict[str, Any] = {"videoID": video_id}
        if cursor:
            variables["cursor"] = cursor
        elif offset_seconds is not None:
            variables["contentOffsetSeconds"] = offset_seconds

        payload = {
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variables,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": VIDEO_COMMENTS_HASH,
                }
            }
        }
        return payload, TWITCH_GQL_URL

    def fetch_video_comments_page(
        self, video_id: str, cursor: Optional[str] = None,
        offset_seconds: Optional[float] = None,
        max_retries: int = 10
    ) -> dict:
        """Fetch a single page of comments from a VOD."""
        payload, url = self._make_payload(video_id, cursor, offset_seconds)

        for attempt in range(max_retries + 1):
            try:
                resp = self.client.post(url, json=payload)

                # Twitch answers a deleted / expired / private VOD with HTTP 400 and
                # a misleading "persistedQuery does not have a valid sha256 hash" body.
                # Same for a genuinely stale persisted-query hash: a full retry loop
                # cannot fix it, so give it one cheap second chance (to absorb a
                # transient edge/anti-bot hiccup) and then bail out - instead of
                # burning ~1 minute (1+2+...+10 s of backoff) per dead VOD.
                if resp.status_code in PERMANENT_HTTP_STATUS:
                    if attempt < PERMANENT_HTTP_RETRIES:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    try:
                        detail = gql_error_message(resp.json())
                    except (json.JSONDecodeError, ValueError):
                        detail = None
                    raise VODUnavailableError(
                        f"VOD {video_id}: HTTP {resp.status_code} - "
                        f"{detail or resp.text[:200]}"
                    )

                resp.raise_for_status()
                data = resp.json()
                return data
            except VODUnavailableError:
                raise
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                if attempt == max_retries:
                    raise RuntimeError(
                        f"Failed to fetch comments after {max_retries} retries: {e}"
                    ) from e
                wait = 1.0 * (attempt + 1)
                time.sleep(wait)

        raise RuntimeError("Unexpected error in fetch_video_comments_page")

    def download_video_chat(
        self,
        video_id: str,
        trim_beginning: Optional[float] = None,
        trim_ending: Optional[float] = None,
        progress_callback=None,
    ) -> tuple[list[Comment], Optional[str], Optional[int], Optional[str], bool]:
        """
        Download all comments from a Twitch VOD.
        Returns (comments, video_created_at, channel_id, video_title, is_partial).
        is_partial=True means the download may be incomplete (connection issues, etc.).

        Raises:
            VODUnavailableError: the VOD itself is not fetchable (deleted, expired,
                private, suspended channel, or a stale persisted-query hash). Not
                retryable.
            RuntimeError: transport-level failures that exhausted their retries.
        """
        comments: list[Comment] = []
        cursor: Optional[str] = None
        is_first = True
        null_count = 0
        error_count = 0
        video_created_at: Optional[str] = None
        channel_id: Optional[str] = None
        video_title: Optional[str] = None
        video_start = trim_beginning or 0
        video_end = trim_ending or float("inf")
        pages_fetched = 0
        is_partial = False

        while True:
            try:
                data = self.fetch_video_comments_page(
                    video_id, cursor=cursor,
                    offset_seconds=video_start if is_first else None,
                )
            except RuntimeError as e:
                # 致命的なエラー → ここまで取得できたデータで続行、partial フラグ
                # ただし1件も取れてなければ再raise
                if not comments:
                    raise
                print(f"[!] Chat download error after {len(comments)} comments: {e}")
                is_partial = True
                break

            # ---- Validate response -------------------------------------------
            # Twitch returns HTTP 200 with {"data": {"video": null}} when the VOD
            # is gone (expired / deleted / private / suspended channel). The key
            # *exists*, so `data.get("video", {})` hands back None rather than the
            # {} default. That None then raised
            # AttributeError: 'NoneType' object has no attribute 'get' when the
            # video-level metadata was read below, before any null check ran.
            payload_data = data.get("data") if isinstance(data, dict) else None
            if not isinstance(payload_data, dict):
                detail = gql_error_message(data)
                if comments:
                    print(f"[!] Chat download interrupted after {len(comments)} comments: {detail}")
                    is_partial = True
                    break
                raise RuntimeError(
                    f"GQL error while fetching VOD {video_id}: "
                    f"{detail or 'empty or malformed response'}"
                )

            video_data = payload_data.get("video")
            if not isinstance(video_data, dict):
                if comments:
                    is_partial = True
                    break
                raise VODUnavailableError(
                    f"VOD {video_id}: 取得できません "
                    "(削除済み・保管期限切れ・非公開・チャンネル閉鎖の可能性)"
                )

            # Extract video-level info on first request
            if is_first:
                video_created_at = video_data.get("createdAt")
                video_title = video_data.get("title")
                creator = video_data.get("creator")
                channel_id = creator.get("id") if isinstance(creator, dict) else None

            video_comments = video_data.get("comments")
            if video_comments is None:
                if null_count >= 10:
                    if not comments:
                        raise RuntimeError("Received too many null comment lists.")
                    is_partial = True
                    break
                null_count += 1
                time.sleep(0.1 * null_count)
                continue

            edges = video_comments.get("edges", [])
            pages_fetched += 1

            if not edges:
                break

            max_offset_this_page = 0.0

            for edge in edges:
                node = edge.get("node", {})
                if not node:
                    continue

                commenter_data = node.get("commenter")
                if not commenter_data:
                    continue

                offset = node.get("contentOffsetSeconds", 0)
                max_offset_this_page = max(max_offset_this_page, offset)

                if offset < video_start:
                    continue
                if offset >= video_end:
                    continue

                comment = self._build_comment(node, video_id, offset)
                comments.append(comment)

            has_next = video_comments.get("pageInfo", {}).get("hasNextPage", False)
            if not has_next:
                break

            if max_offset_this_page >= video_end:
                break

            cursor = edges[-1]["cursor"]
            is_first = False
            error_count = 0

            if progress_callback:
                latest = comments[-1].content_offset_seconds if comments else max_offset_this_page
                progress_callback(latest, trim_ending)

        # ---- バリデーション: 期待される終了位置に達したかチェック ----
        if comments and not is_partial:
            last_offset = comments[-1].content_offset_seconds
            expected_end = min(video_end, float("inf"))
            if expected_end != float("inf"):
                # 最終コメントがトリム終了位置の90%以上に達しているか
                coverage = last_offset / expected_end if expected_end > 0 else 1.0
                if coverage < 0.5 and pages_fetched > 1:
                    # 50%未満しか取れてないのにページが終わった → 不完全
                    is_partial = True
                    print(f"[!] Partial download: last_offset={last_offset:.0f}s vs expected_end={expected_end:.0f}s (coverage={coverage:.0%})")

        return comments, video_created_at, channel_id, video_title, is_partial

    def _build_comment(self, node: dict, video_id: str, offset: float) -> Comment:
        """Convert a raw GQL node into a Comment dataclass."""
        commenter_data = node["commenter"]
        commenter = Commenter(
            _id=commenter_data.get("id", ""),
            display_name=commenter_data.get("displayName", "Unknown").strip(),
            name=commenter_data.get("login", "unknown"),
        )

        message_data = node.get("message", {})
        fragments_raw = message_data.get("fragments", [])

        message = Message()
        body_parts: list[str] = []

        for frag in fragments_raw:
            text = frag.get("text", "")
            if text is None:
                continue
            body_parts.append(text)

            new_fragment = Fragment(text=text)
            emote = frag.get("emote")
            if emote:
                emote_id = emote.get("emoteID", "")
                new_fragment.emoticon = Emoticon(emoticon_id=emote_id)

                emote_obj = Emoticon2(
                    _id=emote_id,
                    begin=emote.get("from", 0),
                )
                emote_obj.end = emote_obj.begin + len(text) + 1
                message.emoticons.append(emote_obj)

            message.fragments.append(new_fragment)

        message.body = "".join(body_parts)

        # Badges
        badges_raw = message_data.get("userBadges", [])
        for badge in badges_raw:
            set_id = badge.get("setID", "")
            version = badge.get("version", "")
            if not set_id and not version:
                continue
            message.user_badges.append(UserBadge(_id=set_id, version=version))

        message.user_color = message_data.get("userColor")

        # Bits
        bits_match = __import__("re").search(
            r"^cheer(\d+)$", message.body, __import__("re").IGNORECASE
        )
        if bits_match:
            try:
                message.bits_spent = int(bits_match.group(1))
            except ValueError:
                pass

        comment = Comment(
            _id=node.get("id", ""),
            created_at=node.get("createdAt", ""),
            channel_id="",
            content_type="video",
            content_id=video_id,
            content_offset_seconds=offset,
            commenter=commenter,
            message=message,
        )

        return comment

    def close(self):
        self.client.close()
