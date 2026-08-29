"""
Emote service for fetching emotes from BTTV, FFZ, and 7TV providers.
Used for embedding emote images into chat downloads (JSON & HTML).
"""

from __future__ import annotations

import base64
import io
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Emote:
    """Generic emote from any provider."""
    code: str
    id: str
    provider: str  # "bttv", "ffz", "stv"
    image_url: str
    animated: bool = False

    @property
    def image_extension(self) -> str:
        if self.image_url.endswith(".gif") or self.image_url.endswith(".gif?"):
            return "gif"
        if self.image_url.endswith(".webp") or self.image_url.endswith(".webp?"):
            return "webp"
        if self.image_url.endswith(".png") or self.image_url.endswith(".png?"):
            return "png"
        if self.image_url.endswith(".svg") or self.image_url.endswith(".svg?"):
            return "svg"
        return "png"


@dataclass
class EmoteCache:
    """Cache of fetched emotes for a channel."""
    channel_id: str
    channel_name: str
    bttv_emotes: list[Emote] = field(default_factory=list)
    ffz_emotes: list[Emote] = field(default_factory=list)
    stv_emotes: list[Emote] = field(default_factory=list)
    global_bttv_emotes: list[Emote] = field(default_factory=list)
    global_ffz_emotes: list[Emote] = field(default_factory=list)
    global_stv_emotes: list[Emote] = field(default_factory=list)
    twitch_emotes: list[Emote] = field(default_factory=list)  # Twitch standard emotes
    fetched_at: float = 0.0

    def get_all_emotes(self) -> list[Emote]:
        """Return all emotes (channel + global)."""
        return (
            self.bttv_emotes
            + self.ffz_emotes
            + self.stv_emotes
            + self.global_bttv_emotes
            + self.global_ffz_emotes
            + self.global_stv_emotes
            + self.twitch_emotes
        )

    def get_emote_map(self) -> dict[str, Emote]:
        """Return a dict mapping emote code -> Emote."""
        emote_map: dict[str, Emote] = {}
        for emote in self.get_all_emotes():
            # If multiple emotes share the same code (rare), keep the channel one
            if emote.code not in emote_map or (
                emote.provider in ("bttv", "ffz", "stv")
                and emote_map[emote.code].provider == "global"
            ):
                emote_map[emote.code] = emote
        return emote_map


# ---------------------------------------------------------------------------
# EmoteService
# ---------------------------------------------------------------------------

class EmoteService:
    """
    Fetches and caches emotes from BTTV, FFZ, 7TV, and Twitch standard emotes.
    """

    # BTTV
    BTTV_GLOBAL_URL = "https://api.betterttv.net/3/cached/emotes/global"
    BTTV_CHANNEL_URL = "https://api.betterttv.net/3/cached/users/twitch/{channel_id}"
    BTTV_CDN_URL = "https://cdn.betterttv.net/emote/{emote_id}/{size}x"

    # FFZ
    FFZ_GLOBAL_URL = "https://api.frankerfacez.com/v1/set/global"
    FFZ_ROOM_URL = "https://api.frankerfacez.com/v1/room/id/{channel_id}"
    FFZ_CDN_URL = "https://cdn.frankerfacez.com/emote/{emote_id}/{size}"

    # 7TV
    STV_USER_URL = "https://7tv.io/v3/users/twitch/{channel_id}"
    STV_EMOTE_SET_URL = "https://7tv.io/v3/emote-sets/{set_id}"
    STV_GLOBAL_SET_ID = "global"
    STV_CDN_TEMPLATE = "https://cdn.7tv.app/emote/{emote_id}/{size}x.{format}"

    # Twitch standard emotes (from twitchemotes.com)
    TWITCH_GLOBAL_API = "https://api.twitchemotes.com/api/v4/global"
    TWITCH_CHANNEL_API = "https://api.twitchemotes.com/api/v4/channels/{channel_id}"
    TWITCH_CDN_TEMPLATE = "https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/dark/2.0"

    # Cache TTL (seconds)
    CACHE_TTL = 300  # 5 minutes

    def __init__(self):
        self._http = httpx.Client(timeout=15.0, follow_redirects=True)
        self._cache: dict[str, EmoteCache] = {}
        self._global_cache: Optional[EmoteCache] = None

    # ---- Public API ----

    def get_emotes(
        self,
        channel_id: str,
        channel_name: str = "",
        include_global: bool = True,
        force_refresh: bool = False,
    ) -> EmoteCache:
        """
        Fetch (or return cached) emotes for a channel.
        """
        now = time.time()
        cached = self._cache.get(channel_id)

        if cached and not force_refresh and (now - cached.fetched_at) < self.CACHE_TTL:
            return cached

        cache = EmoteCache(
            channel_id=channel_id,
            channel_name=channel_name,
        )

        # Fetch channel emotes in parallel
        try:
            cache.bttv_emotes = self._fetch_bttv_channel(channel_id)
        except Exception:
            pass

        try:
            cache.ffz_emotes = self._fetch_ffz_channel(channel_id)
        except Exception:
            pass

        try:
            cache.stv_emotes = self._fetch_stv_channel(channel_id)
        except Exception:
            pass

        try:
            cache.twitch_emotes = self._fetch_twitch_channel(channel_id)
        except Exception:
            pass

        # Fetch global emotes (cached across channels)
        if include_global:
            global_cache = self._get_global_emotes(force_refresh)
            cache.global_bttv_emotes = global_cache.bttv_emotes
            cache.global_ffz_emotes = global_cache.ffz_emotes
            cache.global_stv_emotes = global_cache.stv_emotes
            cache.twitch_emotes.extend(global_cache.twitch_emotes)

        cache.fetched_at = now
        self._cache[channel_id] = cache
        return cache

    def download_emote_image(self, emote: Emote, size: int = 2) -> Optional[bytes]:
        """
        Download the raw image bytes for an emote.
        Size: 1=small, 2=medium, 3=large.
        """
        url = self._build_download_url(emote, size)
        if not url:
            return None
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    def emote_to_base64(self, emote: Emote, size: int = 2) -> Optional[str]:
        """Download an emote and return it as a base64 data URI."""
        data = self.download_emote_image(emote, size)
        if not data:
            return None
        ext = emote.image_extension
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/{ext};base64,{b64}"

    def close(self):
        self._http.close()

    # ---- Internal: BTTV ----

    def _fetch_bttv_channel(self, channel_id: str) -> list[Emote]:
        url = self.BTTV_CHANNEL_URL.format(channel_id=channel_id)
        resp = self._http.get(url)
        resp.raise_for_status()
        data = resp.json()

        emotes: list[Emote] = []

        # Channel-specific emotes
        for em_data in data.get("channelEmotes", []):
            emote = self._bttv_parse_emote(em_data)
            if emote:
                emotes.append(emote)

        # Shared emotes (room-level shared)
        for em_data in data.get("sharedEmotes", []):
            emote = self._bttv_parse_emote(em_data)
            if emote:
                emotes.append(emote)

        return emotes

    def _bttv_parse_emote(self, data: dict) -> Optional[Emote]:
        emote_id = data.get("id")
        code = data.get("code")
        image_type = data.get("imageType", "png")
        if not emote_id or not code:
            return None

        animated = image_type == "gif"
        url = self.BTTV_CDN_URL.format(emote_id=emote_id, size=2)
        return Emote(
            code=code,
            id=emote_id,
            provider="bttv",
            image_url=url,
            animated=animated,
        )

    # ---- Internal: FFZ ----

    def _fetch_ffz_channel(self, channel_id: str) -> list[Emote]:
        url = self.FFZ_ROOM_URL.format(channel_id=channel_id)
        resp = self._http.get(url)
        resp.raise_for_status()
        data = resp.json()

        emotes: list[Emote] = []
        sets = data.get("sets", {})

        for set_id, set_data in sets.items():
            for em_data in set_data.get("emoticons", []):
                emote = self._ffz_parse_emote(em_data)
                if emote:
                    emotes.append(emote)

        return emotes

    def _ffz_parse_emote(self, data: dict) -> Optional[Emote]:
        emote_id = data.get("id")
        code = data.get("name")
        if not emote_id or not code:
            return None

        animated = data.get("animated", False)
        # FFZ uses size 2 for medium
        url = self.FFZ_CDN_URL.format(emote_id=emote_id, size=2)
        return Emote(
            code=code,
            id=str(emote_id),
            provider="ffz",
            image_url=url,
            animated=animated,
        )

    # ---- Internal: 7TV ----

    def _fetch_stv_channel(self, channel_id: str) -> list[Emote]:
        # First get user info to find their emote_set
        user_url = self.STV_USER_URL.format(channel_id=channel_id)
        resp = self._http.get(user_url)
        if resp.status_code == 404:
            return []  # User not on 7TV
        resp.raise_for_status()
        user_data = resp.json()

        emote_set_id = user_data.get("emote_set", {}).get("id")
        if not emote_set_id:
            return []

        # Now fetch the emote set
        set_url = self.STV_EMOTE_SET_URL.format(set_id=emote_set_id)
        resp = self._http.get(set_url)
        resp.raise_for_status()
        set_data = resp.json()

        emotes: list[Emote] = []
        for item in set_data.get("emotes", []):
            emote = self._stv_parse_emote(item)
            if emote:
                emotes.append(emote)

        return emotes

    def _stv_parse_emote(self, data: dict) -> Optional[Emote]:
        """Parse a single emote from a 7TV emote set response."""
        # The 7TV API structure: {"id": "...", "name": "...", "data": {...}}
        emote_id = data.get("id")
        name = data.get("name")

        if not emote_id or not name:
            return None

        # The emote data might be nested or flat depending on API version
        emote_data = data.get("data") or data
        animated = emote_data.get("animated", False)

        # Build URL from the host files
        host = emote_data.get("host", {})
        host_url = host.get("url", "")
        files = host.get("files", [])

        # Try to find a 2x or 1x file
        selected_file = None
        for f in files:
            fname = f.get("name", "")
            if "/2x." in fname or fname.startswith("2x"):
                selected_file = fname
                break
        if not selected_file:
            for f in files:
                fname = f.get("name", "")
                if "/1x." in fname or fname.startswith("1x"):
                    selected_file = fname
                    break
        if not selected_file and files:
            selected_file = files[0].get("name", "")

        if host_url and selected_file:
            full_url = f"https:{host_url}/{selected_file}" if host_url.startswith("//") else f"{host_url}/{selected_file}"
        else:
            # Fallback URL construction
            fmt = "gif" if animated else "webp"
            full_url = self.STV_CDN_TEMPLATE.format(emote_id=emote_id, size=2, format=fmt)

        return Emote(
            code=name,
            id=emote_id,
            provider="stv",
            image_url=full_url,
            animated=animated,
        )

    # ---- Internal: Twitch Standard Emotes ----

    def _fetch_twitch_global(self) -> list[Emote]:
        """Fetch Twitch global emotes (e.g. KEKW, LUL, PogChamp)."""
        try:
            resp = self._http.get(self.TWITCH_GLOBAL_API, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return self._fetch_twitch_global_fallback()

        emotes: list[Emote] = []
        for em_data in data if isinstance(data, list) else data.get("data", []):
            emote_id = em_data.get("id")
            code = em_data.get("code") or em_data.get("name")
            if not emote_id or not code:
                continue
            url = self.TWITCH_CDN_TEMPLATE.format(emote_id=emote_id)
            emotes.append(Emote(
                code=code,
                id=str(emote_id),
                provider="twitch",
                image_url=url,
                animated=False,
            ))
        return emotes

    def _fetch_twitch_global_fallback(self) -> list[Emote]:
        """Fallback: hard-coded common Twitch global emotes if API is down."""
        common_global = {
            "Kappa": "25",
            "PogChamp": "88",
            "LUL": "425618",
            "KEKW": "763212",
            "DansGame": "33",
            "FailFish": "360",
            "KappaPride": "368113",
            "Kreygasm": "41",
            "NotLikeThis": "58765",
            "OpieOP": "80",
            "ResidentSleeper": "245",
            "4Head": "354",
            "PJSalt": "36",
            "PJSugar": "405094",
            "PunchTrees": "49",
            "SeemsGood": "91",
            "SSSsss": "46",
            "SwiftRage": "34",
            "TriHard": "42",
            "WutFace": "102243",
            "BabyRage": "296597",
            "BibleThump": "86",
            "EleGiggle": "76",
        }
        emotes = []
        for code, emote_id in common_global.items():
            url = self.TWITCH_CDN_TEMPLATE.format(emote_id=emote_id)
            emotes.append(Emote(
                code=code,
                id=emote_id,
                provider="twitch",
                image_url=url,
                animated=False,
            ))
        return emotes

    def _fetch_twitch_channel(self, channel_id: str) -> list[Emote]:
        """Fetch Twitch channel-specific subscriber emotes."""
        try:
            url = self.TWITCH_CHANNEL_API.format(channel_id=channel_id)
            resp = self._http.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        emotes: list[Emote] = []
        # The twitchemotes API returns channel emotes in a similar list format
        channel_data = data if isinstance(data, list) else data.get("data", [])
        for em_data in channel_data:
            emote_id = em_data.get("id")
            code = em_data.get("code") or em_data.get("name")
            if not emote_id or not code:
                continue
            url = self.TWITCH_CDN_TEMPLATE.format(emote_id=emote_id)
            emotes.append(Emote(
                code=code,
                id=str(emote_id),
                provider="twitch",
                image_url=url,
                animated=False,
            ))
        return emotes

    def _get_global_emotes(self, force_refresh: bool = False) -> EmoteCache:
        """Fetch global emotes from all providers (cached)."""
        now = time.time()
        if self._global_cache and not force_refresh and (now - self._global_cache.fetched_at) < self.CACHE_TTL:
            return self._global_cache

        cache = EmoteCache(channel_id="__global__", channel_name="Global")

        try:
            cache.bttv_emotes = self._fetch_bttv_global()
        except Exception:
            pass

        try:
            cache.ffz_emotes = self._fetch_ffz_global()
        except Exception:
            pass

        try:
            cache.stv_emotes = self._fetch_stv_global()
        except Exception:
            pass

        try:
            cache.twitch_emotes = self._fetch_twitch_global()
        except Exception:
            pass

        cache.fetched_at = now
        self._global_cache = cache
        return cache

    def _fetch_bttv_global(self) -> list[Emote]:
        resp = self._http.get(self.BTTV_GLOBAL_URL)
        resp.raise_for_status()
        data = resp.json()
        emotes: list[Emote] = []
        for em_data in data:
            emote = self._bttv_parse_emote(em_data)
            if emote:
                emotes.append(emote)
        return emotes

    def _fetch_ffz_global(self) -> list[Emote]:
        resp = self._http.get(self.FFZ_GLOBAL_URL)
        resp.raise_for_status()
        data = resp.json()
        emotes: list[Emote] = []
        # Global response: {"sets": {"set_id": {..., "emoticons": [...]}}}
        sets = data.get("sets", {})
        for set_id, set_data in sets.items():
            for em_data in set_data.get("emoticons", []):
                emote = self._ffz_parse_emote(em_data)
                if emote:
                    emotes.append(emote)
        return emotes

    def _fetch_stv_global(self) -> list[Emote]:
        # 7TV global emote set
        set_url = self.STV_EMOTE_SET_URL.format(set_id=self.STV_GLOBAL_SET_ID)
        resp = self._http.get(set_url)
        resp.raise_for_status()
        data = resp.json()
        emotes: list[Emote] = []
        for item in data.get("emotes", []):
            emote = self._stv_parse_emote(item)
            if emote:
                emotes.append(emote)
        return emotes

    # ---- Helpers ----

    def _build_download_url(self, emote: Emote, size: int = 2) -> Optional[str]:
        """Build a download URL for a specific emote."""
        if emote.provider == "bttv":
            return self.BTTV_CDN_URL.format(emote_id=emote.id, size=size)
        elif emote.provider == "ffz":
            sizes = {1: 1, 2: 2, 3: 4}
            return self.FFZ_CDN_URL.format(emote_id=emote.id, size=sizes.get(size, 2))
        elif emote.provider == "stv":
            fmt = "gif" if emote.animated else "webp"
            return self.STV_CDN_TEMPLATE.format(emote_id=emote.id, size=size, format=fmt)
        elif emote.provider == "twitch":
            return self.TWITCH_CDN_TEMPLATE.format(emote_id=emote.id)
        return None


# ---------------------------------------------------------------------------
# HTML Builder with emote rendering
# ---------------------------------------------------------------------------

def render_message_with_emotes(
    message_body: str,
    emote_map: dict[str, Emote],
    default_color: str = "#000000",
) -> str:
    """
    Replace emote codes in a message body with <img> tags.
    Emote codes are matched as whole words only (longest match first).
    """
    if not emote_map or not message_body:
        return _escape_html(message_body)

    # Sort by length descending to match longer codes first
    codes = sorted(emote_map.keys(), key=len, reverse=True)
    escaped_codes = [re.escape(c) for c in codes]
    if not escaped_codes:
        return _escape_html(message_body)

    # Build alternation. Use word boundaries to ensure whole-word matching.
    # Pattern: (?<!\w)(code1|code2|...)(?!\w)
    # This prevents "Pog" from matching inside "PogChamp"
    pattern = r"(?<!\w)(" + "|".join(escaped_codes) + r")(?!\w)"

    def _replace(match):
        code = match.group(1)
        emote = emote_map.get(code)
        if not emote:
            return _escape_html(code)
        src = emote.image_url
        return (
            f'<img class="emote" src="{_escape_html(src)}" '
            f'alt="{_escape_html(code)}" title="{_escape_html(code)}" '
            f'loading="lazy">'
        )

    return re.sub(pattern, _replace, _escape_html(message_body))


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
