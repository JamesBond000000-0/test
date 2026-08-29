# Twitch Chat Downloader (Python)

Twitch VOD/Clip のチャットをダウンロードする Python CLI ツール + Discord Bot。
元の [TwitchDownloader](https://github.com/lay295/TwitchDownloader) (C# .NET) のチャットダウンロード機能・CLI を Python で再実装したものです。

## 機能

### CLI ツール
- ✅ **VOD チャットのダウンロード** - Twitch GQL API を使用
- ✅ **3つの出力形式**: JSON / Text / HTML
- ✅ **時間範囲指定** - `-b` / `-e` でトリム
- ✅ **Gzip 圧縮** - JSON 出力を最大 90% 削減
- ✅ **タイムスタンプ形式** - Relative / Utc / UtcFull / None
- ✅ **URL or ID の自動判別**
- ✅ **BTTV/FFZ/7TV/Twitch 絵文字** - 1,100+ 絵文字を自動取得＆レンダリング
- ✅ **ファイル衝突ハンドリング** - Overwrite / Exit / Rename / Prompt

### Discord Bot 🤖
- ✅ **`!chat download <streamer>`** - 配信者の最新VODを自動検出→DL→アップロード
- ✅ **`!chat download <streamer> <vod_id>`** - 特定VODを指定してDL
- ✅ **`!chat sync`** - チャンネル履歴をスキャンしてDBに同期
- ✅ **`!chat list [streamer]`** - アップロード済みVOD一覧
- ✅ **`!chat status`** - Bot ステータス表示
- ✅ **Gzip圧縮** - チャットを圧縮してアップロード
- ✅ **ファイル分割** - 25MB制限を超える場合は自動分割
- ✅ **重複回避（ポータブル）** - Discordメッセージ履歴からメタデータを読み取る方式。どこで起動しても同じチャンネル内で重複なし
- ✅ **メタデータ添付** - VOD ID、配信者、日時、タイトルをメッセージに表示＋JSONで重複検出用に埋め込み
- ✅ **絵文字込み** - BTTV/FFZ/7TV/Twitch 絵文字を埋め込み
- ✅ **トークンは環境変数 or CLI引数** - `DISCORD_BOT_TOKEN` / `--token`

### 📅 デイリーチャットログ (logs.zonian.dev) 🆕
- ✅ **`/chat logs download <streamer>`** - [logs.zonian.dev](https://logs.zonian.dev/api) ミラーAPI経由で チャットログを**1日(UTC)ずつ**DL→Zstd圧縮→`#twitch-logs-archives`へ保存
- ✅ **`[user]` オプション** - 特定ユーザーの発言のみを抽出して保存
- ✅ **`[date_from]/[date_to]/[limit]/[force]`** - 期間・件数の絞り込みと強制再DL
- ✅ **`/chat logs days <streamer>`** - 記録済み日数・保存済み/未保存/未終了の日数を表示
- ✅ **確実に終わった1日のみ対象** - ログの「1日」は **UTC 00:00〜23:59** (JSTとは9時間ずれあり)。UTC24時＋安全マージン(初期値2時間)を過ぎた日のみDLするため、1日の途中で取得してデータが半端になることがない
- ✅ **重複回避はVODと同じ方式** - 重複金庫チャンネルに `{"t":"logs",...}` メタデータJSONを埋め込み、チャンネル履歴スキャンで無限重複回避
- ✅ **0件の日も登録** - その日にチャットが無かった日も重複防止DBに登録され、再スキャン対象にならない

## インストール

```bash
cd twitch-chat-downloader
pip install -e .
```

依存: `httpx`, `typer`, `rich`, `discord.py` (自動インストール)

## 使い方 (CLI)

```bash
# 基本: VOD ID でダウンロード
twitch-chat-downloader chatdownload --id 1234567890 -o chat.json

# VOD URL でも OK
twitch-chat-downloader chatdownload --id https://www.twitch.tv/videos/1234567890 -o chat.html

# HTML 出力（絵文字自動レンダリング）
twitch-chat-downloader chatdownload --id 1234567890 -o chat.html -b 0 -e 30m

# JSON に絵文字メタデータを埋め込み
twitch-chat-downloader chatdownload --id 1234567890 -o chat.json -E

# Gzip 圧縮
twitch-chat-downloader chatdownload --id 1234567890 -o chat.json --compression Gzip
```

## 使い方 (Discord Bot)

```bash
# トークンを直接指定
twitch-chat-downloader bot --token YOUR_DISCORD_BOT_TOKEN

# 環境変数で指定
export DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN
twitch-chat-downloader bot

# データ保存先を指定
twitch-chat-downloader bot --token TOKEN --data-dir /path/to/data
```

### Discord Bot コマンド

| コマンド | 説明 |
|---------|------|
| `!chat download <streamer>` | 配信者の最新VODを自動検出→DL→アップロード（重複回避） |
| `!chat download <streamer> <vod_id>` | 特定VODを指定してDL |
| `/chat logs download <streamer> [user] [date_from] [date_to] [limit] [force]` | デイリーチャットログ(zonian)を1日ずつDL→圧縮→保存 |
| `/chat logs days <streamer> [user]` | デイリーログの記録日数・保存状況を表示 |
| `!chat list [streamer]` | アップロード済みVOD一覧 |
| `!chat status` | Bot ステータス表示 |
| `!chat sync` | チャンネル履歴をスキャンしてDBに同期 |
| `!chat help` | ヘルプ表示 |

### 📅 デイリーログDLの流れ (logs.zonian.dev)

```
1. Discordで /chat logs download channel:orslok を実行
2. Bot が logs.zonian.dev の API から orslok の記録済み日付一覧を取得
   (ミラーが justlog/rustlog インスタンス群から最良の結果を返す)
3. 「確実に1日が終わっている」日だけを抽出:
   - ログの1日 = UTC 00:00〜23:59 (JST 09:00〜翌08:59)
   - UTC24時 + 安全マージン2時間 (env LOGS_DAY_SAFETY_MARGIN_HOURS) を
     過ぎていない日はスキップ → 1日の途中でDLして半端になるのを防止
4. 重複金庫チャンネルをスキャンして未保存の日だけを抽出 (古い順)
5. 各日をDL → Zstd圧縮(level 19, MT) → 8MB超なら分割
   → #twitch-logs-archives にEmbed付きでアップロード
6. 重複金庫に {"t":"logs","ch":...,"d":"YYYY-MM-DD",...} を登録
   → ローカルSQLiteにもキャッシュ
```

アップロードされるファイル: `orslok_20260827.json.zst` (1日1ファイル)
Embedには「日付 (UTC)」「JST範囲」「メッセージ数」が表示されます。

#### 環境変数 (デイリーログ関連)

| 変数 | 初期値 | 説明 |
|------|--------|------|
| `LOGS_API_BASE` | `https://logs.zonian.dev` | APIベースURL |
| `LOGS_DAY_SAFETY_MARGIN_HOURS` | `2` | 1日の完了判定に加える安全マージン(時間) |
| `LOGS_ZSTD_LEVEL` | `19` | Zstd圧縮レベル (22は大容量時に時間がかかりすぎるため19) |

### 🔄 重複回避の仕組み（ポータブル設計）

このBotは **Discordメッセージそのものを重複判定のソース** として使います。

**なぜこれが必要か？**
- Botを別のサーバー/VPSに移行してもDBは引き継がれない
- 複数のBotインスタンスが同じチャンネルで動く可能性がある
- → Discordチャンネル履歴をスキャンすれば、誰がアップロードしたかに関係なく重複を検出できる

**仕組み:**

1. アップロード時にメッセージ末尾にコンパクトなJSONを埋め込み:
   `{"t":"chat","vod":"2819361247","s":"xqc","sd":"xQc",...}`
2. 新規ダウンロード前にチャンネル履歴（最大5000件）をスキャン
3. 上記JSONが含まれるメッセージを検出→VOD IDを抽出
4. 抽出結果をローカルSQLiteにキャッシュ（次回から高速）
5. 未アップロードのVODのみを処理

**初回:** チャンネル履歴スキャン（1-2秒程度）
**2回目以降:** キャッシュから即座に判定
**手動再スキャン:** `!chat sync`

### アップロードの流れ

1. Discordで `!chat download xqc` を実行
2. Bot が xQc の最新VODを Twitch API から取得
3. **チャンネル履歴をスキャン**して既存メタデータをチェック（重複回避）
4. 未アップロードのVODのみを抽出
5. 各VODのチャットをダウンロード（絵文字込み）
6. Gzip圧縮 → 25MB超える場合は分割
7. Discordにアップロード（**メタデータJSONをメッセージ末尾に埋め込み**）
8. ローカルDBにキャッシュ

```
📼 Twitch Chat Log
VOD: 2819361247
配信者: xQc (xqc)
日時: 2026-07-13 22:46:58
タイトル: 🥪CLICK🥪HERE🥪DRAMA🥪NEWS🥪LIVE🥪...
コメント数: 12,345
絵文字: bttv,ffz,stv,twitch
圧縮: gzip | サイズ: 1,234.5 KB
```

## プロジェクト構成

```
twitch-chat-downloader/
├── pyproject.toml
├── README.md
└── twitch_chat_downloader/
    ├── __init__.py
    ├── cli.py              # Typer CLI
    ├── gql_client.py       # Twitch GQL API クライアント
    ├── twitch_api.py       # Twitch ユーザー/VOD 情報取得
    ├── formatters.py       # JSON/Text/HTML フォーマッター
    ├── emote_service.py    # BTTV/FFZ/7TV/Twitch 絵文字取得
    ├── chat_logger.py      # ダウンロード/圧縮/分割/DB管理 (VOD+デイリーログ)
    ├── logs_api.py         # 🆕 logs.zonian.dev デイリーログAPI・圧縮分割
    └── discord_bot.py      # Discord Bot (/chat + /chat logs + /chat track)
```
