#!/usr/bin/env python3
"""
colab_ready.py を再生成するスクリプト。

colab_ready.py はプロジェクト一式を base64 の tar.gz として埋め込んだ
「Colab にコピペするだけのインストーラ」。ソースを変更したらこれを実行しないと
Colab 側は古いコードのままになる。

使い方:  python3 tools/rebuild_colab.py
"""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_DIR = "twitch-chat-downloader"

# tar.gz に含めるファイル (存在するものだけ)
INCLUDE = [
    "requirements.txt",
    "pyproject.toml",
    "README.md",
]
INCLUDE_DIRS = [
    "twitch_chat_downloader",  # *.py のみ
    "tests",                   # *.py のみ
]

TEMPLATE = '''\
# ============================================================
# Twitch Chat Downloader - Google Colab 用 自己解凍インストーラ
# 使い方: このコードをColabセルにコピペして ▶実行
# ============================================================
import base64, subprocess, sys, os

# Base64エンコードされた tar.gz ({size} bytes)
B64 = "{b64}"

data = base64.b64decode(B64)
with open("/tmp/tcd.tar.gz", "wb") as f:
    f.write(data)

# 解凍
subprocess.run(["tar", "xzf", "/tmp/tcd.tar.gz", "-C", "/content/"], check=True)
os.chdir("/content/twitch-chat-downloader")

# インストール
subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], check=True)

print("=" * 54)
print("  ✅ Twitch Chat Downloader インストール完了!")
print("=" * 54)
print()
print("🤖 Discord Bot:")
print("  !twitch-chat-downloader bot --token DISCORD_BOT_TOKEN &")
print()
print("📋 準備確認:")
print("  !twitch-chat-downloader --help")
print()
print("🧪 動作確認 (任意):")
print("  !python3 tests/test_logs_scan.py")
print()
print("📅 デイリーログ (logs.zonian.dev) - Discord内コマンド:")
print("  /chat logs download channel:配信者名")
print("  /chat logs days channel:配信者名")
print("  /chat logs scan                     <- トラッキングリスト全員を一括DL")
print("  /chat logs redownload channel:配信者名 date_from:... date_to:...")
print("       <- 重複回避DBを消去して強制再DL (誤った「ログなし」登録の修正用)")
print("       (download/scan の force:True でも同じく旧記録を消去して再DL)")
print()
print("📂 保存先チャンネル (1カテゴリー最大50チャンネル対策):")
print("  dest:archive   -> VOD本棚 #twitch-chat-archives に同居保存")
print("  dest:<チャンネルID> -> 任意のチャンネルへ")
print("  (未指定でも専用チャンネルを作れなければ自動でVOD本棚へ)")
'''


def build_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        def add(path: Path, arcname: str):
            tf.add(path, arcname=f"{TOP_DIR}/{arcname}", recursive=False)

        tf.add(ROOT, arcname=TOP_DIR, recursive=False)  # トップディレクトリ
        for rel in INCLUDE:
            p = ROOT / rel
            if p.exists():
                add(p, rel)
        for d in INCLUDE_DIRS:
            folder = ROOT / d
            if not folder.is_dir():
                continue
            tf.add(folder, arcname=f"{TOP_DIR}/{d}", recursive=False)
            for p in sorted(folder.glob("*.py")):
                add(p, f"{d}/{p.name}")
    return buf.getvalue()


def main() -> None:
    data = build_tarball()
    b64 = base64.b64encode(data).decode("ascii")
    out = ROOT / "colab_ready.py"
    out.write_text(TEMPLATE.format(size=len(data), b64=b64), encoding="utf-8")
    print(f"[✓] {out.name} を更新: tar.gz {len(data):,} bytes / base64 {len(b64):,} chars")

    # 検証: 埋め込んだ tar.gz を展開して中身を一覧表示
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
        print(f"[✓] 収録ファイル {len(names)}件:")
        for n in names:
            print(f"      {n}")


if __name__ == "__main__":
    main()
