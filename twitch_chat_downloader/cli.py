"""
Twitch Chat Downloader CLI - Python replacement for TwitchDownloaderCLI chatdownload.

Usage:
    twitch-chat-downloader chatdownload --id <vod_id_or_url> -o <output>
    twitch-chat-downloader chatdownload --id <clip_slug> -o <output>
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TaskProgressColumn,
)

from .gql_client import TwitchGQLClient, VODUnavailableError
from .formatters import format_json, format_text, format_html
from .emote_service import EmoteService

from .discord_bot import run_bot

app = typer.Typer(
    name="twitch-chat-downloader",
    help="Download Twitch VOD/Clip chat comments in JSON, Text, or HTML format.",
    no_args_is_help=True,
)

console = Console()

# Regex patterns for parsing VOD and Clip IDs
VOD_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?twitch\.tv/videos/(\d+)"
)
CLIP_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:clips\.twitch\.tv/|twitch\.tv/\w+/clip/)([a-zA-Z0-9_-]+)"
)
VOD_ID_PATTERN = re.compile(r"^(\d+)$")
CLIP_SLUG_PATTERN = re.compile(r"^([a-zA-Z0-9_-]+)$")


def parse_identifier(id_or_url: str) -> tuple[str, str]:
    """Parse a VOD ID/URL or Clip slug/URL. Returns (type, id)."""
    m = VOD_URL_PATTERN.match(id_or_url)
    if m:
        return "video", m.group(1)

    m = CLIP_URL_PATTERN.match(id_or_url)
    if m:
        return "clip", m.group(1)

    m = VOD_ID_PATTERN.match(id_or_url)
    if m:
        return "video", m.group(1)

    m = CLIP_SLUG_PATTERN.match(id_or_url)
    if m:
        return "clip", m.group(1)

    raise typer.BadParameter(
        f"Unable to parse VOD/Clip ID/URL: {id_or_url}\n"
        "Expected: VOD ID (1234567890), VOD URL, Clip slug, or Clip URL"
    )


def parse_time_duration(value: str) -> Optional[float]:
    """Parse a time duration string into seconds."""
    if not value:
        return None

    if ":" in value:
        parts = value.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        raise typer.BadParameter(f"Invalid time format: {value}")

    total = 0.0
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", value)
    if m and (m.group(1) or m.group(2) or m.group(3)):
        if m.group(1):
            total += int(m.group(1)) * 3600
        if m.group(2):
            total += int(m.group(2)) * 60
        if m.group(3):
            total += int(m.group(3))
        return total

    try:
        return float(value)
    except ValueError:
        raise typer.BadParameter(f"Invalid time format: {value}")


def get_output_extension(output_path: str) -> str:
    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".htm":
        ext = ".html"
    return ext


@app.command()
def chatdownload(
    id: str = typer.Option(
        ..., "-u", "--id",
        help="The ID or URL of the VOD or clip to download.",
    ),
    output: str = typer.Option(
        ..., "-o", "--output",
        help="File to output to (.json, .html, .txt).",
    ),
    compression: str = typer.Option(
        "None", "--compression",
        help="Compression for JSON output: None or Gzip.",
    ),
    beginning: Optional[str] = typer.Option(
        None, "-b", "--beginning",
        help="Time to trim from beginning (e.g. 1h30m, 900, 1:30:00).",
    ),
    ending: Optional[str] = typer.Option(
        None, "-e", "--ending",
        help="Time to trim from ending (e.g. 2h, 7200).",
    ),
    embed_images: bool = typer.Option(
        False, "-E", "--embed-images",
        help="Embed emotes into the chat file for offline viewing. "
             "Requires fetching third-party emote data.",
    ),
    timestamp_format: str = typer.Option(
        "Relative", "--timestamp-format",
        help="Timestamp format for .txt output: Relative, Utc, UtcFull, None.",
    ),
    bttv: bool = typer.Option(
        True, "--bttv",
        help="Enable BTTV emote embedding (requires -E).",
        show_default=True,
    ),
    ffz: bool = typer.Option(
        True, "--ffz",
        help="Enable FFZ emote embedding (requires -E).",
        show_default=True,
    ),
    stv: bool = typer.Option(
        True, "--stv", "--stv",
        help="Enable 7TV emote embedding (requires -E).",
        show_default=True,
    ),
    twitch_emotes: bool = typer.Option(
        True, "--twitch-emotes",
        help="Enable Twitch standard emote embedding (requires -E).",
        show_default=True,
    ),
    threads: int = typer.Option(
        4, "-t", "--threads",
        help="Number of parallel download threads.",
    ),
    temp_path: Optional[str] = typer.Option(
        None, "--temp-path",
        help="Path to temporary folder for cache.",
    ),
    collision: str = typer.Option(
        "Prompt", "--collision",
        help="File collision handling: Overwrite, Exit, Rename, Prompt.",
    ),
):
    """Download chat from a Twitch VOD or clip."""
    try:
        _chatdownload(
            id=id, output=output, compression=compression,
            beginning=beginning, ending=ending,
            embed_images=embed_images, timestamp_format=timestamp_format,
            bttv=bttv, ffz=ffz, stv=stv, twitch_emotes=twitch_emotes,
            threads=threads, temp_path=temp_path, collision=collision,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}", style="red")
        raise typer.Exit(code=1)


def _chatdownload(
    id: str, output: str, compression: str,
    beginning: Optional[str], ending: Optional[str],
    embed_images: bool, timestamp_format: str,
    bttv: bool, ffz: bool, stv: bool, twitch_emotes: bool,
    threads: int, temp_path: Optional[str], collision: str,
):
    # Parse ID
    content_type, content_id = parse_identifier(id)
    console.print(f"[bold cyan]✓[/] Identified as [bold]{content_type}[/]: {content_id}")

    # Parse trim times
    trim_beginning = parse_time_duration(beginning) if beginning else None
    trim_ending = parse_time_duration(ending) if ending else None

    if trim_beginning is not None:
        console.print(f"  Trim beginning: {trim_beginning:.0f}s")
    if trim_ending is not None:
        console.print(f"  Trim ending: {trim_ending:.0f}s")

    # Validate output extension
    ext = get_output_extension(output)
    if ext not in (".json", ".html", ".txt"):
        raise typer.BadParameter(
            f"Invalid output extension '{ext}'. Valid extensions: .json, .html, .txt"
        )
    if ext != ".json" and compression != "None":
        console.print("[yellow]Warning:[/] Compression is only valid for .json output. Ignoring.")

    # Handle file collision
    output_path = Path(output)
    if output_path.exists():
        if collision == "Exit":
            console.print(f"[red]File already exists: {output_path}[/]")
            raise typer.Exit(code=1)
        elif collision == "Overwrite":
            console.print(f"[yellow]Overwriting: {output_path}[/]")
        elif collision == "Rename":
            counter = 1
            while output_path.exists():
                stem = output_path.stem
                suffix = output_path.suffix
                if compression == "Gzip" and ext == ".json":
                    suffix = ".json.gz"
                output_path = output_path.parent / f"{stem}_{counter}{suffix}"
                counter += 1
            console.print(f"[yellow]Renamed to: {output_path}[/]")
        elif collision == "Prompt":
            response = typer.prompt(
                f"File '{output_path}' already exists. Overwrite? (y/N)", default="n",
            )
            if response.lower() != "y":
                console.print("[red]Aborted.[/]")
                raise typer.Exit(code=0)

    # Download chat
    client = TwitchGQLClient()
    emote_service: Optional[EmoteService] = None

    try:
        console.print("[bold cyan]⬇[/] Downloading chat comments...")

        comments = []
        video_created_at = None
        channel_id = None
        video_title = None
        is_partial = False

        if content_type == "video":
            start_time = time.time()

            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=False,
            ) as progress:
                task = progress.add_task("Downloading...", total=None)

                def progress_callback(latest_offset: float, end_offset: Optional[float]):
                    if end_offset and end_offset > 0:
                        progress.update(
                            task, completed=min(latest_offset, end_offset), total=end_offset,
                        )
                    else:
                        progress.update(task, completed=latest_offset)

                # download_video_chat returns 5 values (comments, created_at,
                # channel_id, title, is_partial) - the old 4-tuple unpack raised
                # "ValueError: too many values to unpack" on every CLI download.
                comments, video_created_at, channel_id, video_title, is_partial = (
                    client.download_video_chat(
                        content_id,
                        trim_beginning=trim_beginning,
                        trim_ending=trim_ending,
                        progress_callback=progress_callback,
                    )
                )
                if is_partial:
                    console.print(
                        "[yellow]![/] Warning: chat may be incomplete (connection issues)."
                    )

            elapsed = time.time() - start_time
            console.print(
                f"[bold green]✓[/] Downloaded [bold]{len(comments)}[/] comments "
                f"in {elapsed:.1f}s"
            )
        else:
            raise NotImplementedError(
                "Clip chat download is not yet implemented. "
                "Use the VOD ID approach or original TwitchDownloader for clips."
            )

        if not comments:
            console.print("[yellow]No comments found.[/]")
            raise typer.Exit(code=0)

        # Determine streamer info
        streamer_name = comments[0].commenter.display_name if comments else ""
        streamer_login = comments[0].commenter.name if comments else ""

        # ---- Fetch emotes if requested ----
        emote_map: Optional[dict] = None
        if embed_images or ext == ".html":
            providers_enabled = []
            if bttv:
                providers_enabled.append("BTTV")
            if ffz:
                providers_enabled.append("FFZ")
            if stv:
                providers_enabled.append("7TV")
            if twitch_emotes:
                providers_enabled.append("Twitch")

            if providers_enabled:
                console.print(
                    f"[bold cyan]🎨[/] Fetching emotes: {', '.join(providers_enabled)}..."
                )
                t0 = time.time()
                emote_service = EmoteService()

                cache = emote_service.get_emotes(
                    str(channel_id) if channel_id else content_id,
                    channel_name=streamer_name or streamer_login or "",
                )
                emote_map = cache.get_emote_map()

                # Filter by provider if not all enabled
                if not (bttv and ffz and stv and twitch_emotes):
                    filtered = {}
                    for code, emote in emote_map.items():
                        if (emote.provider == "bttv" and not bttv) \
                                or (emote.provider == "ffz" and not ffz) \
                                or (emote.provider == "stv" and not stv) \
                                or (emote.provider == "twitch" and not twitch_emotes):
                            continue
                        filtered[code] = emote
                    emote_map = filtered

                elapsed = time.time() - t0
                console.print(
                    f"  [green]✓[/] {len(emote_map)} emotes fetched in {elapsed:.1f}s"
                )

        # ---- Write output ----
        console.print(f"[bold cyan]💾[/] Writing output: {output_path}")

        if ext == ".json":
            data = format_json(
                comments,
                video_created_at=video_created_at,
                channel_id=channel_id,
                streamer_name=streamer_name,
                streamer_login=streamer_login,
                video_id=content_id,
                compression=compression,
                emote_map=emote_map,
            )
            actual_path = output_path
            if compression == "Gzip" and not str(output_path).endswith(".gz"):
                actual_path = Path(str(output_path) + ".gz")
            actual_path.write_bytes(data)
            size_mb = len(data) / (1024 * 1024)
            console.print(
                f"  [green]✓[/] JSON chat saved ({size_mb:.2f} MB) "
                f"{'(Gzip compressed)' if compression == 'Gzip' else ''}"
            )

        elif ext == ".html":
            html = format_html(
                comments,
                video_created_at=video_created_at,
                streamer_name=streamer_name,
                video_id=content_id,
                emote_map=emote_map,
            )
            output_path.write_text(html, encoding="utf-8")
            size_mb = len(html.encode("utf-8")) / (1024 * 1024)
            console.print(f"  [green]✓[/] HTML chat saved ({size_mb:.2f} MB)")

        elif ext == ".txt":
            text = format_text(
                comments,
                timestamp_format=timestamp_format,
                video_created_at=video_created_at,
            )
            output_path.write_text(text, encoding="utf-8")
            size_mb = len(text.encode("utf-8")) / (1024 * 1024)
            console.print(f"  [green]✓[/] Text chat saved ({size_mb:.2f} MB)")

    except NotImplementedError:
        raise
    except VODUnavailableError as e:
        # 存在しない VOD (削除・保管期限切れ・非公開) はリトライで回復しない
        console.print(f"[red]✗[/] {e}")
        console.print(
            "[dim]Twitch 上でこの VOD が見つかりません。再試行では回復しません。[/]"
        )
        raise typer.Exit(code=1)
    except Exception as e:
        raise RuntimeError(f"Failed to download chat: {e}") from e
    finally:
        client.close()
        if emote_service:
            emote_service.close()


@app.command()
def bot(
    token: str = typer.Option(
        ..., "--token",
        envvar="DISCORD_BOT_TOKEN",
        help="Discord Bot token. Can also be set via DISCORD_BOT_TOKEN env var.",
    ),
    data_dir: Optional[str] = typer.Option(
        None, "--data-dir",
        help="Directory to store chat data and database (default: ~/.twitch_chat_logger).",
    ),
):
    """Run the Discord bot for Twitch chat log archiving. (Bot upload limit: 8MB)"""
    console.print(f"[bold cyan]🤖[/] Starting Discord bot...")
    console.print(f"  Data directory: {data_dir or '~/.twitch_chat_logger'}")
    console.print(f"  Bot upload limit: 8MB")
    run_bot(token=token, data_dir=data_dir)


@app.command()
def info(
    id: str = typer.Option(
        ..., "-u", "--id",
        help="The ID or URL of the VOD to get info about.",
    ),
):
    """Get information about a Twitch VOD."""
    console.print("[yellow]Info command is not yet fully implemented.[/]")
    console.print(f"VOD ID: {id}")


@app.callback()
def main_callback():
    pass


def main():
    app()


if __name__ == "__main__":
    main()
