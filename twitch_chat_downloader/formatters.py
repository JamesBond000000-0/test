"""
Output formatters for Twitch chat: JSON, Text, HTML.
"""

from __future__ import annotations

import json
import gzip
import io
import re
import zstandard as zstd
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from .gql_client import Comment


def format_json(
    comments: list[Comment],
    video_created_at: Optional[str] = None,
    channel_id: Optional[int] = None,
    streamer_name: Optional[str] = None,
    streamer_login: Optional[str] = None,
    video_id: Optional[str] = None,
    compression: str = "None",
    emote_map: Optional[dict[str, Any]] = None,
) -> bytes:
    """
    Format chat comments as JSON, optionally compressed with Zstd or Gzip.
    Returns bytes.
    """
    if comments:
        first = comments[0]
        last = comments[-1]
        video_start = int(first.content_offset_seconds)
        video_end = int(last.content_offset_seconds)

        created_dt = None
        if first.created_at:
            try:
                created_dt = datetime.fromisoformat(first.created_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
    else:
        video_start = 0
        video_end = 0
        created_dt = None

    # Build output dict in TwitchDownloader-compatible format
    output: dict = {
        "FileInfo": {
            "Version": "1.0.0"
        },
        "streamer": {
            "name": streamer_name or "",
            "login": streamer_login or "",
            "id": channel_id or 0,
        },
        "video": {
            "id": video_id or "",
            "start": video_start,
            "end": video_end,
            "length": video_end - video_start,
            "created_at": (created_dt.isoformat() if created_dt else video_created_at) or "",
        },
        "comments": [],
    }

    # Optionally embed third-party emote metadata
    if emote_map:
        # Build a structured embeddedData section (simplified)
        embedded = {"thirdParty": []}
        code_set = set()
        for code, emote in emote_map.items():
            if code in code_set:
                continue
            code_set.add(code)
            embedded["thirdParty"].append({
                "code": code,
                "provider": emote.provider,
                "id": emote.id,
                "image_url": emote.image_url,
                "animated": emote.animated,
            })
        output["embeddedData"] = embedded

    for c in comments:
        comment_dict = {
            "_id": c._id,
            "created_at": c.created_at,
            "channel_id": c.channel_id,
            "content_type": c.content_type,
            "content_id": c.content_id,
            "content_offset_seconds": c.content_offset_seconds,
            "commenter": {
                "display_name": c.commenter.display_name,
                "_id": c.commenter._id,
                "name": c.commenter.name,
            },
            "message": {
                "body": c.message.body,
                "fragments": [
                    {
                        "text": f.text,
                        **(f.emoticon and {"emoticon": {"emoticon_id": f.emoticon.emoticon_id}} or {}),
                    }
                    for f in c.message.fragments
                ],
                "emoticons": [
                    {"_id": e._id, "begin": e.begin, "end": e.end}
                    for e in c.message.emoticons
                ],
                "user_badges": [
                    {"_id": b._id, "version": b.version}
                    for b in c.message.user_badges
                ],
                "user_color": c.message.user_color,
                "bits_spent": c.message.bits_spent,
            },
        }
        output["comments"].append(comment_dict)

    json_bytes = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")

    # Zstd最高圧縮レベル22 (Ultra) での圧縮
    if compression == "Zstd":
        cctx = zstd.ZstdCompressor(level=22, write_checksum=True)
        return cctx.compress(json_bytes)

    elif compression == "Gzip":
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
            f.write(json_bytes)
        return buf.getvalue()

    return json_bytes


def format_text(
    comments: list[Comment],
    timestamp_format: str = "Relative",
    video_created_at: Optional[str] = None,
) -> str:
    """
    Format chat comments as plain text.
    timestamp_format: "Relative", "Utc", "UtcFull", "None"
    """
    lines: list[str] = []

    video_dt = None
    if video_created_at:
        try:
            video_dt = datetime.fromisoformat(video_created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    for c in comments:
        timestamp_str = ""

        if timestamp_format == "None":
            pass
        elif timestamp_format == "Relative":
            offset = c.content_offset_seconds
            hours = int(offset // 3600)
            minutes = int((offset % 3600) // 60)
            seconds = int(offset % 60)
            if hours > 0:
                timestamp_str = f"[{hours:02d}:{minutes:02d}:{seconds:02d}] "
            else:
                timestamp_str = f"[{minutes:02d}:{seconds:02d}] "
        elif timestamp_format in ("Utc", "UtcFull"):
            if video_dt:
                comment_dt = video_dt + timedelta(seconds=c.content_offset_seconds)
                if timestamp_format == "Utc":
                    timestamp_str = f"[{comment_dt.strftime('%Y-%m-%d %H:%M:%S')}] "
                else:
                    timestamp_str = f"[{comment_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] "
            else:
                offset = c.content_offset_seconds
                minutes = int(offset // 60)
                seconds = int(offset % 60)
                timestamp_str = f"[{minutes:02d}:{seconds:02d}] "

        line = f"{timestamp_str}{c.commenter.display_name}: {c.message.body}"
        lines.append(line)

    return "\n".join(lines)


def format_html(
    comments: list[Comment],
    video_created_at: Optional[str] = None,
    streamer_name: Optional[str] = None,
    video_id: Optional[str] = None,
    emote_map: Optional[dict[str, Any]] = None,
) -> str:
    """
    Format chat comments as a standalone HTML file.
    """
    chat_entries_html = ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    for c in comments:
        color = c.message.user_color or "#000000"

        # Badges
        badges_html = ""
        for badge in c.message.user_badges[:3]:
            badges_html += f'<span class="badge" title="{badge._id} v{badge.version}">[{badge._id}]</span> '

        # Format message with emote rendering
        if emote_map:
            message_html = _render_message_with_emotes(c.message.body, emote_map)
        else:
            message_html = _message_to_html(c.message.body)

        offset = c.content_offset_seconds
        hours = int(offset // 3600)
        minutes = int((offset % 3600) // 60)
        seconds = int(offset % 60)
        if hours > 0:
            time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            time_str = f"{minutes:02d}:{seconds:02d}"

        chat_entries_html += f"""        <div class="chat-line">
            <span class="timestamp">{time_str}</span>
            {badges_html}
            <span class="name" style="color:{color}">{_escape_html(c.commenter.display_name)}</span>
            <span class="message">{message_html}</span>
        </div>\n"""

    streamer = streamer_name or "Unknown"
    title = f"Twitch Chat - {streamer}"
    if video_id:
        title += f" - {video_id}"

    # Count providers for the header
    provider_counts = ""
    if emote_map:
        providers = set()
        for emote in emote_map.values():
            if hasattr(emote, "provider"):
                providers.add(emote.provider)
        if providers:
            provider_counts = f" | Emotes: {', '.join(sorted(providers))}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_escape_html(title)}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #0e0e10;
            color: #efeff1;
            padding: 16px;
            font-size: 14px;
            line-height: 1.5;
        }}
        .chat-header {{
            padding: 12px 16px;
            background: #1f1f23;
            border-radius: 8px;
            margin-bottom: 16px;
            border: 1px solid #2d2d30;
        }}
        .chat-header h1 {{ font-size: 16px; font-weight: 600; }}
        .chat-header .meta {{ font-size: 12px; color: #adadb8; margin-top: 4px; }}
        .chat-line {{
            padding: 4px 8px;
            border-radius: 4px;
            display: flex;
            align-items: flex-start;
            gap: 6px;
            flex-wrap: wrap;
        }}
        .chat-line:hover {{ background: #1f1f23; }}
        .timestamp {{ color: #adadb8; font-size: 12px; white-space: nowrap; font-family: 'Courier New', monospace; min-width: 60px; }}
        .badge {{ font-size: 11px; color: #bf94ff; white-space: nowrap; }}
        .name {{ font-weight: 600; white-space: nowrap; }}
        .message {{ word-break: break-word; display: inline-flex; align-items: center; flex-wrap: wrap; gap: 2px; }}
        .emote {{ display: inline-block; vertical-align: middle; height: 28px; width: auto; object-fit: contain; }}
        a {{ color: #bf94ff; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .bits {{ color: #ffd700; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="chat-header">
        <h1>💬 Twitch Chat</h1>
        <div class="meta">
            Streamer: {_escape_html(streamer)} | Comments: {len(comments)} | Generated: {now}{provider_counts}
        </div>
    </div>
    {chat_entries_html}
</body>
</html>"""
    return html


# ---- Internal Helpers ----

def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _message_to_html(text: str) -> str:
    """Convert a message body to HTML (no emote rendering)."""
    if not text:
        return ""
    text = _escape_html(text)

    # Highlight bits (cheering)
    text = re.sub(
        r'(^cheer\d+|\bcheer\d+\b)',
        r'<span class="bits">\1</span>',
        text,
        flags=re.IGNORECASE,
    )

    # Linkify URLs
    text = re.sub(
        r'(https?://[^\s<]+)',
        r'<a href="\1" target="_blank" rel="noopener">\1</a>',
        text,
    )

    return text


def _render_message_with_emotes(message_body: str, emote_map: dict[str, Any]) -> str:
    if not emote_map or not message_body:
        return _escape_html(message_body)

    codes = sorted(emote_map.keys(), key=len, reverse=True)
    escaped_codes = [re.escape(c) for c in codes]
    if not escaped_codes:
        return _message_to_html(message_body)

    pattern = r"(?<!\w)(" + "|".join(escaped_codes) + r")(?!\w)"

    def _replace(match):
        code = match.group(1)
        emote = emote_map.get(code)
        if not emote:
            return _escape_html(code)
        src = emote.image_url if hasattr(emote, "image_url") else str(emote)
        return (
            f'<img class="emote" src="{_escape_html(src)}" '
            f'alt="{_escape_html(code)}" title="{_escape_html(code)}" '
            f'loading="lazy">'
        )

    escaped_body = _escape_html(message_body)
    return re.sub(pattern, _replace, escaped_body)