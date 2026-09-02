"""
Zonian Logs API Client - logs.zonian.dev ミラー経由で Twitch チャットの
デイリーログ (justlog / rustlog インスタンス群) を取得するサービス。

API 仕様 (https://logs.zonian.dev/api):
  - GET /api/{channel}[/{user}]          ... ログ記録済み日付の一覧 (メタデータ)
  - GET /channel/{ch}/{y}/{m}/{d}?json=true  ... 指定日(UTC)のチャンネル全体ログ
  - GET /channelid/{id}/{y}/{m}/{d}?json=true ... チャンネルID指定版

「1日」の定義は UTC (00:00:00Z ~ 23:59:59Z)。日本時間 (JST, UTC+9) とは
9時間ずれるため、日付の完了判定は UTC 基準で厳密に行う
(= その日の UTC 24:00 + 安全マージンを過ぎていない日は DL 対象にしない)。
"""

from __future__ import annotations

import json
import os
import time
import zstandard as zstd
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

# ---- Constants / Configuration ----

DEFAULT_BASE_URL = "https://logs.zonian.dev"

# 巨大な1日分 (実測で200MB超のJSONあり) のDL対策:
# - read timeout は「データが完全に止まって」から諦めるまでの時間。
#   ストリーミングDL中はバイトが流れ続けている限りタイマーがリセットされるため、
#   巨大ファイルでも途中で切れない。サーバー混雑時のストールに耐えるよう長めに設定。
# - すべて環境変数で調整可能。
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DEFAULT_TIMEOUT_SECONDS = _env_float("LOGS_API_TIMEOUT", 300.0)        # 読み出し(ストール検知)タイムアウト
DEFAULT_CONNECT_TIMEOUT = _env_float("LOGS_API_CONNECT_TIMEOUT", 15.0)  # 接続タイムアウト
DEFAULT_MAX_RETRIES = _env_int("LOGS_API_MAX_RETRIES", 4)              # 再試行回数 (初回+4回)
DEFAULT_MAX_DOWNLOAD_SECONDS = _env_float("LOGS_API_MAX_DOWNLOAD_SECONDS", 3600.0)  # 1回あたりの全体上限
STREAM_CHUNK_SIZE = 1024 * 1024  # 1MB 単位でストリーミング

# 「記録済みとされている日なのに中身が空だった」場合の再検証設定。
# ミラーの裏のインスタンスが一時的に落ちていると空/404が返ることがあるため、
# 即座に「ログなし」とは決めつけず、時間をおいて再確認する。
EMPTY_RECHECK_DELAY_SECONDS = _env_float("LOGS_EMPTY_RECHECK_DELAY", 10.0)
EMPTY_RECHECK_ATTEMPTS = _env_int("LOGS_EMPTY_RECHECK_ATTEMPTS", 2)

# 「確実に1日が終わっている」判定のための安全マージン (時間)。
# UTC 0時を過ぎてもインスタンス側の書き込み/集約が遅れる可能性を考慮。
# 環境変数 LOGS_DAY_SAFETY_MARGIN_HOURS で上書き可能。
DEFAULT_DAY_SAFETY_MARGIN_HOURS = 2.0

# Discord Bot のアップロード上限 (chat_logger と同一)
BOT_UPLOAD_LIMIT = 8 * 1024 * 1024

# デイリーログのZstd圧縮レベル。
# level 22 (VOD用) は大容量の1日分に時間がかかりすぎるため (検証: 約1.3%のサイズ向上に
# 対し80%の時間増)、実用上ほぼ同等の level 19 + マルチスレッドを用いる。
# 環境変数 LOGS_ZSTD_LEVEL で変更可能。
DEFAULT_ZSTD_LEVEL = 19

JST = timezone(timedelta(hours=9), name="JST")

LOG_SOURCE_NAME = "logs.zonian.dev"


def get_zstd_level() -> int:
    try:
        return int(os.environ.get("LOGS_ZSTD_LEVEL", DEFAULT_ZSTD_LEVEL))
    except (TypeError, ValueError):
        return DEFAULT_ZSTD_LEVEL


def get_base_url() -> str:
    return os.environ.get("LOGS_API_BASE", DEFAULT_BASE_URL).rstrip("/")


def get_safety_margin_hours() -> float:
    try:
        return max(float(os.environ.get("LOGS_DAY_SAFETY_MARGIN_HOURS", DEFAULT_DAY_SAFETY_MARGIN_HOURS)), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_DAY_SAFETY_MARGIN_HOURS


class LogsAPIError(RuntimeError):
    """logs.zonian.dev API のエラー。"""


class LogsEmptyMismatchError(LogsAPIError):
    """
    サーバーの記録日一覧には存在する日なのに、実ログ取得が空(0件/404)だった。

    サーバー側(ミラー配下のログインスタンス)の一時的な不調の可能性が高いため、
    「ログなし」として重複回避DBに登録せず、後日あらためてDLし直すのが安全。
    """


def make_log_id(channel: str, user_filter: Optional[str], day: str) -> str:
    """重複判定用の一意ID: {channel}|{user or *}|{YYYY-MM-DD}"""
    return f"{channel.lower()}|{(user_filter or '').lower() or '*'}|{day}"


def day_is_complete(
    day: date,
    now: Optional[datetime] = None,
    margin_hours: Optional[float] = None,
) -> bool:
    """
    指定日(UTC)が「確実に終わっている」かを判定する。

    UTC の日付は UTC 24:00 に終わるが、JST では朝9時までずれ込む。
    さらにインスタンス側の書き込み遅延を考慮し、マージン時間も加味する。
    例) JST 8/29 10:00 (= UTC 8/29 01:00) の時点では:
        - UTC 8/28 は終了済み (1時間前) だが、マージン2h未満なのでまだ対象外
        - UTC 8/27 以前は確定済みなので DL 対象
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if margin_hours is None:
        margin_hours = get_safety_margin_hours()
    day_end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    deadline = day_end + timedelta(hours=margin_hours)
    return now >= deadline


def day_jst_range(day: date) -> tuple[datetime, datetime]:
    """UTC日付に対応するJST範囲を返す (例: 8/27 UTC -> JST 8/27 09:00 ~ 8/28 08:59:59)"""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).astimezone(JST)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end


def day_jst_range_text(day: date) -> str:
    s, e = day_jst_range(day)
    if s.date() == e.date():
        return f"{s:%Y-%m-%d %H:%M}~{e:%H:%M} JST"
    return f"{s:%Y-%m-%d %H:%M}~{e:%m-%d %H:%M} JST"


class ZonianLogsClient:
    """logs.zonian.dev ミラーAPI クライアント (同期版: 既存コードと同じスタイル)"""

    def __init__(self, base_url: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url or get_base_url()
        self.timeout = timeout
        self._timeout_config = httpx.Timeout(
            connect=DEFAULT_CONNECT_TIMEOUT,
            read=timeout,
            write=60.0,
            pool=60.0,
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self._timeout_config,
            headers={
                "User-Agent": "twitch-chat-downloader/1.2 (Discord bot; daily chat logs)",
                "Accept": "application/json",
            },
        )

    # ---- Low-level ----

    @staticmethod
    def retry_wait_seconds(attempt: int) -> float:
        """リトライ前の待機時間 (指数バックオフ): 5, 10, 20, 40, 60(上限)..."""
        return float(min(5.0 * (2 ** attempt), 60.0))

    def _read_timeout_for(self, attempt: int) -> float:
        """試行ごとにreadタイムアウトを段階的に伸ばす (巨大ファイル対策)。"""
        return self.timeout * (attempt + 1)

    def _stream_get(self, path: str, read_timeout: float, progress_cb=None) -> tuple[int, bytes]:
        """
        GET をストリーミングで実行し、本文全体をバイト列で返す。

        - バイトが流れている限りタイムアウトしない (チャンクごとにreadタイマー更新)。
        - 完全にストールして read_timeout 秒応答がなければ ReadTimeout になる。
        - 1試行あたりの全体上限 (DEFAULT_MAX_DOWNLOAD_SECONDS) も監視する。
        - 巨大レスポンスは進捗を標準出力 (+ progress_cb) に出す
          (バッチの停滞監視の手がかり)。
        """
        def note(text: str) -> None:
            print(f"[logs] {text}")
            if progress_cb is not None:
                try:
                    progress_cb(text)
                except Exception:
                    pass

        timeout_cfg = httpx.Timeout(
            connect=DEFAULT_CONNECT_TIMEOUT,
            read=read_timeout,
            write=60.0,
            pool=60.0,
        )
        started = time.time()
        deadline = started + max(DEFAULT_MAX_DOWNLOAD_SECONDS, read_timeout)
        received = 0
        last_log = started
        with self._client.stream("GET", path, timeout=timeout_cfg) as resp:
            status = resp.status_code
            if status == 404:
                resp.read()
                return status, b""
            if status == 429 or status >= 500:
                resp.read()
                raise LogsAPIError(f"HTTP {status} from {path}")
            if status != 200:
                resp.read()
                raise LogsAPIError(f"HTTP {status} from {path} (未対応のステータス)")
            buf = bytearray()
            for chunk in resp.iter_bytes(chunk_size=STREAM_CHUNK_SIZE):
                buf.extend(chunk)
                received += len(chunk)
                now = time.time()
                if now - last_log >= 10.0:
                    note(f"{path} 受信中... {received/1024/1024:.1f}MB / {now-started:.0f}秒")
                    last_log = now
                if now > deadline:
                    raise LogsAPIError(
                        f"DL全体が制限時間({DEFAULT_MAX_DOWNLOAD_SECONDS:.0f}秒)を超過 "
                        f"({received/1024/1024:.1f}MB受信済み): {path}"
                    )
            if received > 5 * 1024 * 1024:
                note(f"{path} 受信完了 {received/1024/1024:.1f}MB / {time.time()-started:.1f}秒")
            return status, bytes(buf)

    def _request(self, path: str, progress_cb=None) -> tuple[int, bytes]:
        """
        リトライ (指数バックオフ) 付きGET。タイムアウト/接続エラー/429/5xxで再試行。
        404 は即座に返す (データ無し = リトライ不要)。

        戻り値: (status_code, body_bytes)
        """
        last_error: Optional[Exception] = None
        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            read_timeout = self._read_timeout_for(attempt)
            try:
                return self._stream_get(path, read_timeout, progress_cb)
            except (httpx.TimeoutException, httpx.TransportError, LogsAPIError) as e:
                last_error = e
                if attempt < DEFAULT_MAX_RETRIES:
                    wait = self.retry_wait_seconds(attempt)
                    print(
                        f"[logs] {path} 失敗 ({e}) -> {wait:.0f}秒後に再試行 "
                        f"({attempt+1}/{DEFAULT_MAX_RETRIES}) [timeout {read_timeout:.0f}s]"
                    )
                    if progress_cb is not None:
                        try:
                            progress_cb(f"{path} ﾘﾄﾗｲ待機 {wait:.0f}s ({attempt+1}/{DEFAULT_MAX_RETRIES})")
                        except Exception:
                            pass
                    time.sleep(wait)
        raise LogsAPIError(f"APIリクエスト失敗: {path} ({last_error})")

    @staticmethod
    def _parse_json(body: bytes, path: str) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise LogsAPIError(f"APIレスポンスの解析に失敗しました: {path}")

    # ---- Public API ----

    def get_available_days(self, channel: str, user: Optional[str] = None) -> dict:
        """
        GET /api/{channel}[/{user}] - ログ記録済み日付の一覧を取得。

        戻り値:
          {
            "channel_login": str, "channel_id": str,
            "user": str|None,
            "available_channel": bool, "available_user": bool,
            "days": [date, ...] (昇順ソート済み),
            "since": date|None,
            "instances": [str, ...],
            "days_count": int,
          }
        チャンネルが存在しない / どこにも記録されていない場合は LogsAPIError。
        """
        channel_clean = channel.strip().lower().lstrip("@")
        user_clean = (user or "").strip().lower().lstrip("@") or None
        path = f"/api/{channel_clean}" + (f"/{user_clean}" if user_clean else "")

        status, body = self._request(path)
        data = self._parse_json(body, path)

        if status == 404 or data.get("error"):
            err = data.get("error") or f"HTTP {status}"
            raise LogsAPIError(f"チャンネル `{channel_clean}` のログが見つかりません: {err}")

        available = data.get("available") or {}
        if not available.get("channel"):
            raise LogsAPIError(
                f"チャンネル `{channel_clean}` はどのログインスタンスにも記録されていません"
            )

        logged = ((data.get("loggedData") or {}).get("list")) or []
        days: list[date] = []
        for entry in logged:
            try:
                days.append(date(int(entry["year"]), int(entry["month"]), int(entry["day"])))
            except (KeyError, TypeError, ValueError):
                continue
        days.sort()

        since_raw = (data.get("loggedData") or {}).get("since") or {}
        since: Optional[date] = None
        try:
            if since_raw.get("year"):
                since = date(int(since_raw["year"]), int(since_raw["month"]), int(since_raw["day"]))
        except (KeyError, TypeError, ValueError):
            since = None

        req = data.get("request") or {}
        chan = req.get("channel") or {}

        return {
            "channel_login": chan.get("login") or channel_clean,
            "channel_id": str(chan.get("id") or ""),
            "user": user_clean,
            "available_channel": True,
            "available_user": bool(available.get("user")),
            "days": days,
            "days_count": len(days),
            "since": since,
            "instances": ((data.get("channelLogs") or {}).get("instances")) or [],
        }

    def _fetch_day_once(self, path: str, day: date, progress_cb=None) -> list[dict]:
        """指定パスから1日分のメッセージを取得 (404は空リスト)。"""
        status, body = self._request(path, progress_cb)
        if status == 404:
            return []  # その日のログなし

        data = self._parse_json(body, path)
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise LogsAPIError(f"{day:%Y-%m-%d} のレスポンス形式が不正です")
        return messages

    def fetch_day(
        self,
        channel: str,
        day: date,
        channel_id: Optional[str] = None,
        recorded_day: bool = False,
        progress_cb=None,
    ) -> list[dict]:
        """
        GET /channel/{ch}/{y}/{m}/{d}?json=true - 指定日(UTC)のチャンネル全体の
        チャットログを取得。メッセージが1件も無い日は空リストを返す。

        recorded_day:
          サーバー側の「記録済み日付一覧」に載っている日の場合に True。
          True なのに取得結果が空だった場合、ミラー配下のインスタンスの一時的な
          不調の可能性が高いため、次の再検証を行う:
            1) /channelid/{id} 経由で再取得 (別ルートで解決される場合がある)
            2) 少し待ってから /channel/ 経由で再取得 (EMPTY_RECHECK_ATTEMPTS 回)
          それでも空なら LogsEmptyMismatchError を送出する。呼び出し側 (Bot) は
          これを「ログなし」として重複回避DBに登録せず、後日再試行すべき。
        """
        def note(text: str) -> None:
            print(f"[logs] {text}")
            if progress_cb is not None:
                try:
                    progress_cb(text)
                except Exception:
                    pass

        channel_clean = channel.strip().lower().lstrip("@")
        path = f"/channel/{channel_clean}/{day.year}/{day.month}/{day.day}?json=true"

        messages = self._fetch_day_once(path, day, progress_cb)
        if messages or not recorded_day:
            return messages

        # ---- ここから先: 記録済みのはずなのに空 -> 一時的なサーバー不調と疑う ----
        note(f"{channel_clean} {day:%Y-%m-%d}: 記録済み日付なのに0件 -> 再検証します")

        # 1) チャンネルID経由の別ルートで再取得
        if channel_id:
            id_path = f"/channelid/{channel_id}/{day.year}/{day.month}/{day.day}?json=true"
            try:
                messages = self._fetch_day_once(id_path, day, progress_cb)
                if messages:
                    note(f"{channel_clean} {day:%Y-%m-%d}: channelid 経由で {len(messages)}件 取得できた")
                    return messages
            except LogsAPIError as e:
                note(f"channelid 経由の再取得も失敗: {e}")

        # 2) 時間をおいて数回再取得 (インスタンス復旧を待つ)
        for i in range(max(EMPTY_RECHECK_ATTEMPTS, 0)):
            wait = EMPTY_RECHECK_DELAY_SECONDS * (i + 1)
            note(f"{channel_clean} {day:%Y-%m-%d}: {wait:.0f}秒待機して再取得 ({i+1}/{EMPTY_RECHECK_ATTEMPTS})")
            time.sleep(wait)
            try:
                messages = self._fetch_day_once(path, day, progress_cb)
            except LogsAPIError as e:
                note(f"再取得失敗: {e}")
                continue
            if messages:
                note(f"{channel_clean} {day:%Y-%m-%d}: 再取得で {len(messages)}件 取得できた")
                return messages

        raise LogsEmptyMismatchError(
            f"{day:%Y-%m-%d} はサーバーの記録日一覧に存在しますが、ログ取得は0件でした "
            f"(再検証{EMPTY_RECHECK_ATTEMPTS + (1 if channel_id else 0)}回後も空)。"
            f"サーバー側の一時的な不調の可能性があるため「ログなし」として登録せずスキップします。"
        )

    def close(self):
        self._client.close()


# ---- Document building / compression / splitting ----

def filter_messages_by_user(messages: list[dict], user: str) -> list[dict]:
    """特定ユーザーの発言のみ抽出 (username / displayName の両方を大文字小文字無視で比較)"""
    u = user.strip().lower().lstrip("@")
    if not u:
        return messages
    return [
        m for m in messages
        if str(m.get("username", "")).lower() == u
        or str(m.get("displayName", "")).lower() == u
    ]


def build_day_document(
    channel_login: str,
    channel_id: str,
    channel_display: str,
    day: date,
    messages: list[dict],
    user_filter: Optional[str] = None,
) -> dict:
    """1日分のチャットログJSONドキュメントを構築する。"""
    jst_start, jst_end = day_jst_range(day)
    return {
        "version": "1.0",
        "type": "twitch_daily_chat_log",
        "source": LOG_SOURCE_NAME,
        "channel": {
            "login": channel_login.lower(),
            "id": str(channel_id or ""),
            "display_name": channel_display or channel_login,
        },
        "user_filter": (user_filter or "").lower() or None,
        "date": {
            "utc": day.isoformat(),
            "utc_start": f"{day.isoformat()}T00:00:00Z",
            "utc_end": f"{day.isoformat()}T23:59:59Z",
            "jst_start": jst_start.isoformat(),
            "jst_end": jst_end.isoformat(),
        },
        "message_count": len(messages),
        "comments": messages,
    }


def compress_and_split(
    document: dict,
    base_name: str,
    max_upload_size: int = BOT_UPLOAD_LIMIT,
    progress_cb=None,
) -> list[dict]:
    """
    ドキュメントを Zstd で圧縮し、アップロード上限を超える場合は
    メッセージ単位で分割する (chat_logger の VOD分割と同じ方式・命名規則)。
    """
    def note(text: str) -> None:
        print(f"[logs] {text}")
        if progress_cb is not None:
            try:
                progress_cb(text)
            except Exception:
                pass

    target_size = int(max_upload_size * 0.9)
    cctx = zstd.ZstdCompressor(level=get_zstd_level(), write_checksum=True, threads=-1)

    full_json = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    note(f"{base_name}: 圧縮開始 (元 {len(full_json)/1024/1024:.1f}MB / zstd lv{get_zstd_level()})")
    t0 = time.time()
    compressed = cctx.compress(full_json)
    note(f"{base_name}: 圧縮完了 {len(compressed)/1024/1024:.1f}MB / {time.time()-t0:.0f}秒")

    if len(compressed) <= target_size:
        return [{
            "name": f"{base_name}.json.zst",
            "data": compressed,
            "size": len(compressed),
            "part": 1,
            "total": 1,
        }]

    # 分割: 1メッセージあたりの平均サイズから分割数を算出し、再圧縮
    all_messages = document.get("comments", [])
    if not all_messages:
        return [{
            "name": f"{base_name}.json.zst",
            "data": compressed,
            "size": len(compressed),
            "part": 1,
            "total": 1,
        }]

    avg = len(full_json) / len(all_messages)
    per_part = max(int(target_size / avg), 1)

    for _attempt in range(3):
        parts = _build_parts(document, all_messages, per_part, base_name, cctx, max_upload_size)
        if parts is not None:
            return parts
        per_part = max(per_part // 2, 1)

    # 最終フォールバック: バイト列をそのまま分割
    max_safe = int(max_upload_size * 0.85)
    total_parts = (len(compressed) + max_safe - 1) // max_safe
    out = []
    for i in range(total_parts):
        chunk = compressed[i * max_safe:(i + 1) * max_safe]
        out.append({
            "name": f"{base_name}.chunk{i + 1}_{total_parts}.zst",
            "data": chunk,
            "size": len(chunk),
            "part": i + 1,
            "total": total_parts,
        })
    return out


def _build_parts(
    root: dict,
    all_messages: list[dict],
    messages_per_part: int,
    base_name: str,
    cctx,
    max_upload_size: int,
) -> Optional[list[dict]]:
    parts: list[dict] = []
    total = len(all_messages)

    for i in range(0, total, messages_per_part):
        chunk = all_messages[i:i + messages_per_part]
        part_root = dict(root)
        part_root["comments"] = chunk
        part_root["_split"] = {
            "part": len(parts) + 1,
            "total_parts": None,
            "channel": root.get("channel", {}).get("login", ""),
            "date": root.get("date", {}).get("utc", ""),
        }
        part_json = json.dumps(part_root, ensure_ascii=False, indent=2).encode("utf-8")
        compressed = cctx.compress(part_json)
        if len(compressed) > max_upload_size:
            return None  # まだ大きい -> 呼び出し元で分割数を増やして再試行
        parts.append({
            "name": f"{base_name}.part{len(parts) + 1}.json.zst",
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


def make_base_name(channel: str, day: date, user_filter: Optional[str] = None) -> str:
    """アップロードファイル名: {channel}[_{user}]_{YYYYMMDD}"""
    ch = channel.strip().lower()
    if user_filter:
        return f"{ch}_{user_filter.strip().lower()}_{day:%Y%m%d}"
    return f"{ch}_{day:%Y%m%d}"
