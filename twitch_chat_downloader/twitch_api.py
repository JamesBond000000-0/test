"""
Twitch API client for user lookup and VOD listing.
Uses the same GQL endpoint as the chat downloader.
"""

from __future__ import annotations

import httpx

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"


class TwitchAPI:
    """Minimal Twitch API client for user and VOD info."""

    def __init__(self, client_id: str = TWITCH_CLIENT_ID):
        self._http = httpx.Client(
            headers={"Client-ID": client_id},
            timeout=15.0,
        )

    def resolve_user(self, login: str) -> dict | None:
        """Resolve a Twitch username/ login to {id, login, displayName, profileImageURL}."""
        query = f"""
        query {{
            user(login: "{login.lower()}") {{
                id
                login
                displayName
                profileImageURL(width: 300)
            }}
        }}
        """
        resp = self._http.post(TWITCH_GQL_URL, json={"query": query})
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return None
        user = data.get("data", {}).get("user")
        if user:
            # Clean up profileImageURL
            url = user.get("profileImageURL", "")
            if url and ("{width}" in url or "{height}" in url):
                url = url.replace("{width}", "300").replace("{height}", "300")
                user["profileImageURL"] = url
        return user

    def resolve_user_by_id(self, user_id: str) -> dict | None:
        """Resolve a Twitch user by their numeric ID (login name may change)."""
        query = f"""
        query {{
            user(id: "{user_id}") {{
                id
                login
                displayName
                profileImageURL(width: 300)
            }}
        }}
        """

        resp = self._http.post(TWITCH_GQL_URL, json={"query": query})
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return None
        user = data.get("data", {}).get("user")
        if user:
            url = user.get("profileImageURL", "")
            if url and ("{width}" in url or "{height}" in url):
                url = url.replace("{width}", "300").replace("{height}", "300")
                user["profileImageURL"] = url
        return user

    def get_recent_vods(

        self,
        login: str,
        limit: int = 10,
        broadcast_type: str | None = "ARCHIVE",
        max_retries: int = 2,
    ) -> list[dict]:
        """
        Get recent VODs (past broadcasts) for a user.
        Returns list of {id, title, createdAt, lengthSeconds, viewCount}.
        """
        query = f"""
        query {{
            user(login: "{login.lower()}") {{
                videos(first: {limit}) {{
                    edges {{
                        node {{
                            id
                            title
                            createdAt
                            lengthSeconds
                            viewCount
                        }}
                    }}
                }}
            }}
        }}
        """
        for attempt in range(max_retries + 1):
            resp = self._http.post(TWITCH_GQL_URL, json={"query": query})
            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                err_msg = data["errors"][0]["message"]
                if attempt < max_retries and "service" in err_msg.lower():
                    import time as _time
                    _time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"GQL error: {data['errors']}")

            break

        edges = data.get("data", {}).get("user", {}).get("videos", {}).get("edges", [])
        vods = []
        for edge in edges:
            node = edge["node"]
            vods.append({
                "id": node["id"],
                "title": node.get("title", ""),
                "created_at": node.get("createdAt", ""),
                "length_seconds": node.get("lengthSeconds", 0),
                "view_count": node.get("viewCount", 0),
            })
        return vods

    def get_all_vods(
        self,
        login: str = "",
        max_total: int = 100,
        user_id: str = "",
    ) -> list[dict]:
        """
        Fetch ALL available VODs for a user using pagination.
        Returns list sorted oldest-first (by created_at ascending).
        """
        all_vods = []
        cursor = None
        limit = min(max_total, 100)
        max_retries = 2

        identifier = f'id: "{user_id}"' if user_id else f'login: "{login.lower()}"'

        while len(all_vods) < max_total:
            cursor_arg = f'after: "{cursor}", ' if cursor else ""
            query = f"""
            query {{
                user({identifier}) {{
                    videos(first: {limit}, {cursor_arg}sort: TIME) {{
                        edges {{
                            cursor
                            node {{
                                id
                                title
                                createdAt
                                lengthSeconds
                                viewCount
                            }}
                        }}
                        pageInfo {{
                            hasNextPage
                        }}
                    }}
                }}
            }}
            """
            for attempt in range(max_retries + 1):
                try:
                    resp = self._http.post(TWITCH_GQL_URL, json={"query": query})
                    resp.raise_for_status()
                    data = resp.json()
                    if "errors" in data:
                        err_msg = data["errors"][0]["message"]
                        if attempt < max_retries and "service" in err_msg.lower():
                            import time
                            time.sleep(1.0 * (attempt + 1))
                            continue
                        raise RuntimeError(f"GQL error: {data['errors']}")
                    break
                except httpx.HTTPError:
                    if attempt < max_retries:
                        import time
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    raise

            edges = data.get("data", {}).get("user", {}).get("videos", {}).get("edges", [])
            if not edges:
                break

            for edge in edges:
                node = edge["node"]
                all_vods.append({
                    "id": node["id"],
                    "title": node.get("title", ""),
                    "created_at": node.get("createdAt", ""),
                    "length_seconds": node.get("lengthSeconds", 0),
                    "view_count": node.get("viewCount", 0),
                })

            has_next = data.get("data", {}).get("user", {}).get("videos", {}).get("pageInfo", {}).get("hasNextPage", False)
            if not has_next:
                break

            cursor = edges[-1]["cursor"]
            limit = min(max_total - len(all_vods), 100)
            if limit <= 0:
                break

        # Sort oldest first
        all_vods.sort(key=lambda v: v.get("created_at", ""))
        return all_vods

    def get_vod_info(self, vod_id: str) -> dict | None:
        """Get info about a specific VOD by its ID.
        Returns {id, title, created_at, length_seconds, view_count,
                 streamer_id, streamer_login, streamer_display,
                 thumbnail_url, streamer_profile_image}."""
        query = f"""
        query {{
            video(id: "{vod_id}") {{
                id
                title
                createdAt
                lengthSeconds
                viewCount
                previewThumbnailURL
                owner {{
                    id
                    login
                    displayName
                    profileImageURL(width: 300)
                }}
            }}
        }}
        """
        resp = self._http.post(TWITCH_GQL_URL, json={"query": query})
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return None
        video = data.get("data", {}).get("video")
        if video is None:
            return None

        owner = video.get("owner", {})
        thumb_url = video.get("previewThumbnailURL", "")
        if thumb_url and "{width}" in thumb_url:
            thumb_url = thumb_url.replace("{width}", "640").replace("{height}", "360")

        profile_img = owner.get("profileImageURL", "")
        if profile_img and "{width}" in profile_img:
            profile_img = profile_img.replace("{width}", "300").replace("{height}", "300")

        return {
            "id": video["id"],
            "title": video.get("title", ""),
            "created_at": video.get("createdAt", ""),
            "length_seconds": video.get("lengthSeconds", 0),
            "view_count": video.get("viewCount", 0),
            "thumbnail_url": thumb_url,
            "streamer_id": owner.get("id"),
            "streamer_login": owner.get("login"),
            "streamer_display": owner.get("displayName"),
            "streamer_profile_image": profile_img,
        }

    def close(self):
        self._http.close()
