"""
Twitchユーザー名解決のフォールバック手段。
Twitch APIで見つからなかったユーザー(名前変更・削除)を解決する。

重要: Cloudflare保護サイト(twitchtracker等)は自動化が困難なため、
        解決できなかった場合はユーザーに手動解決を促す。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

def _log(m: str):
    logger.info(m)
    print(f"[tracker] {m}")

def _err(m: str):
    logger.error(m)
    print(f"[tracker] X {m}")


def _auto_install(pkg: str, timeout: int = 180) -> bool:
    name = pkg.split(">=")[0].split("==")[0].split("[")[0]
    try:
        __import__(name)
        return True
    except ImportError:
        pass
    _log(f"{pkg} install...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"],
                       check=True, timeout=timeout, capture_output=True)
        _log(f"{pkg} done")
        return True
    except Exception as e:
        _err(f"install fail: {e}")
    return False


def resolve_via_twitchtracker(login: str) -> Optional[dict]:
    """
    twitchtracker.com のリダイレクトを利用して名前変更を解決。
    Cloudflare保護のため自動化は不安定。成功したら {'login','id','display_name'} を返す。
    """
    _log(f"resolving '{login}' via twitchtracker...")
    url = f"https://twitchtracker.com/{login}"
    
    # 方法1: HTTP redirect (301/302) を追跡
    r = _try_http_redirect(url)
    if r: return r
    
    # 方法2: Chrome で JS redirect を実行
    r = _try_browser(url, login)
    if r: return r
    
    _err(f"could not resolve '{login}'")
    return None


def _try_http_redirect(url: str) -> Optional[dict]:
    """HTTPレベルのリダイレクト（301/302）を追跡"""
    _auto_install("httpx>=0.25")
    import httpx
    try:
        # リダイレクトを追跡しないでステータス確認
        resp = httpx.get(url, follow_redirects=True, timeout=15.0)
        final = str(resp.url)
        # リダイレクト先が元のURLと違うか
        if "twitchtracker.com/" in final:
            path = final.split("twitchtracker.com/")[-1].rstrip("/").split("?")[0]
            if path and path != url.split("twitchtracker.com/")[-1].rstrip("/"):
                _log(f"HTTP redirect: {path}")
                return _verify_user(path)
    except: pass
    return None


def _try_browser(url: str, original_login: str) -> Optional[dict]:
    """Chrome で twitchtracker にアクセスしリダイレクトを検出"""
    # google-colab-selenium
    r = _try_colab(url, original_login)
    if r: return r
    # seleniumbase
    r = _try_base(url, original_login)
    if r: return r
    return None


def _try_colab(url: str, original_login: str) -> Optional[dict]:
    if not _auto_install("google-colab-selenium[undetected]>=1.0.18"):
        return None
    try:
        import google_colab_selenium as gs
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        _log("Chrome starting...")
        d = gs.Chrome(options=opts)
        d.get(url)
        time.sleep(5)
        cur = d.current_url
        src = d.page_source
        title = d.title or ""
        d.quit()
        return _process_result(cur, src, title, original_login)
    except Exception as e:
        _err(f"colab: {e}")
    return None


def _try_base(url: str, original_login: str) -> Optional[dict]:
    if not _auto_install("seleniumbase>=4.0.0"):
        return None
    try:
        from seleniumbase import SB
        _log("seleniumbase starting...")
        with SB(uc=True, headless=True) as sb:
            sb.uc_open_with_reconnect(url, reconnect_time=3)
            cur = sb.driver.current_url
            src = sb.driver.page_source
            title = sb.driver.title or ""
            return _process_result(cur, src, title, original_login)
    except Exception as e:
        _err(f"base: {e}")
    return None


def _process_result(current_url: str, html: str, title: str, original_login: str) -> Optional[dict]:
    """ブラウザアクセス結果からユーザー情報を抽出"""
    # リダイレクト先URLを抽出
    resolved_login = None
    if "twitchtracker.com/" in current_url:
        path = current_url.split("twitchtracker.com/")[-1].rstrip("/").split("?")[0]
        if path and path != original_login:
            resolved_login = path
            _log(f"Redirect detected: {original_login} -> {resolved_login}")
            return _verify_user(resolved_login)
    
    if resolved_login is None:
        resolved_login = original_login
    
    # ページが有効か確認
    _log(f"Page title: {title[:60]}")
    if any(x in title.lower() for x in ["404", "not found", "error", "page not found"]):
        _err("404 page")
        return None
    
    # __INITIAL_STATE__ からID抽出
    for m in re.finditer(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL):
        try:
            state = json.loads(m.group(1))
            for path in [["user","id"], ["userData","id"], ["channel","id"],
                         ["data","user","id"], ["stats","userId"]]:
                val = state
                for k in path:
                    val = val.get(k, {}) if isinstance(val, dict) else None
                    if val is None: break
                if val and isinstance(val, (int,str)) and str(val).isdigit():
                    tid = str(val)
                    if len(tid) >= 5 and tid not in ("2147483647","0","1","9999999999"):
                        _log(f"ID from state: {tid}")
                        return {"login": resolved_login, "id": tid, "display_name": resolved_login}
        except: pass
    
    _err("Could not extract valid data")
    return None


def _verify_user(login: str) -> Optional[dict]:
    """Twitch APIでユーザー確認"""
    import httpx
    try:
        resp = httpx.post("https://gql.twitch.tv/gql",
            headers={"Client-ID": "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"},
            json={"query": f"query{{user(login:\"{login}\"){{id login displayName}}}}"},
            timeout=10.0)
        if resp.status_code == 200:
            body = resp.json() if resp.text else {}
            data = body.get("data") if isinstance(body, dict) else None
            user = data.get("user") if isinstance(data, dict) else None
            if user and user.get("id"):
                _log(f"Verified via API: {user['login']} (ID: {user['id']})")
                return {"login": user["login"], "id": user["id"], "display_name": user.get("displayName", user["login"])}
    except: pass
    return None


def suggest_manual_resolution(login: str) -> str:
    return (
        f"❌ `{login}` はTwitch APIで見つかりません（名前変更された可能性）。\n"
        f"手動確認: https://twitchtracker.com/{login}\n"
        f"  → ブラウザで開くと正しい新しい名前にリダイレクトされます\n"
        f"  → 新しい名前を `!chat track add <新しい名前>` で追加してください"
    )
