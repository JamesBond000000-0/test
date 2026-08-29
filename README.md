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
| `!chat list [streamer]` | アップロード済みVOD一覧 |
| `!chat status` | Bot ステータス表示 |
| `!chat sync` | チャンネル履歴をスキャンしてDBに同期 |
| `!chat help` | ヘルプ表示 |

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
    ├── cli.py              # Typer CLI (427行)
    ├── gql_client.py       # Twitch GQL API クライアント (332行)
    ├── twitch_api.py       # Twitch ユーザー/VOD 情報取得 (133行)
    ├── formatters.py       # JSON/Text/HTML フォーマッター (363行)
    ├── emote_service.py    # BTTV/FFZ/7TV/Twitch 絵文字取得 (584行)
    ├── chat_logger.py      # ダウンロード/圧縮/分割/DB管理 (311行)
    └── discord_bot.py      # Discord Bot (427行)
```

**合計: 2,580行（Python 7ファイル）**
