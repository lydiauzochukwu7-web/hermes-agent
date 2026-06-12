"""
Bluesky Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to Bluesky and relays
mentions and replies in your notification feed to/from the Hermes agent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# httpx is imported lazily inside methods to keep startup times fast during plugin discovery
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform, PlatformConfig


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _build_facets(text: str) -> list[dict]:
    """Parse HTTP/HTTPS links from the text and construct richtext link facets using UTF-8 byte offsets."""
    facets = []
    text_bytes = text.encode("utf-8")
    
    url_regex = re.compile(rb'https?://[^\s()<>]+')
    for match in url_regex.finditer(text_bytes):
        start = match.start()
        end = match.end()
        # Clean trailing punctuation from the url bytes
        url_bytes = text_bytes[start:end]
        while url_bytes and url_bytes[-1] in b'.,;:!?")':
            url_bytes = url_bytes[:-1]
            end -= 1
        if not url_bytes:
            continue
        url_str = url_bytes.decode("utf-8")
        facets.append({
            "index": {
                "byteStart": start,
                "byteEnd": end
            },
            "features": [{
                "$type": "app.bsky.richtext.facet#link",
                "uri": url_str
            }]
        })
    return facets


class BlueskyAdapter(BasePlatformAdapter):
    """Async Bluesky adapter implementing the BasePlatformAdapter interface."""

    MAX_MESSAGE_LENGTH = 300  # Bluesky post limit is 300 characters/graphemes

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("bluesky"))
        
        extra = config.extra or {}
        self.handle = os.getenv("BLUESKY_HANDLE") or extra.get("handle", "")
        self.password = os.getenv("BLUESKY_PASSWORD") or extra.get("password", "")
        try:
            self.poll_interval = int(os.getenv("BLUESKY_POLL_INTERVAL") or extra.get("poll_interval", 10))
        except (ValueError, TypeError):
            self.poll_interval = 10

        self.allowed_users = extra.get("allowed_users", [])
        self._allowed_users_lower = {u.lower() for u in self.allowed_users if isinstance(u, str)}
        self.allow_all_users = _parse_bool(os.getenv("BLUESKY_ALLOW_ALL_USERS") or extra.get("allow_all_users", False))

        self.did = ""
        self.access_jwt = ""
        self.refresh_jwt = ""
        self.last_login_time = 0.0

        self._poll_task = None
        self.last_seen_time = ""
        self._thread_roots = {}  # maps post_uri -> {"uri": root_uri, "cid": root_cid}
        self._lock_key = None

    @property
    def name(self) -> str:
        return "Bluesky"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        if not HTTPX_AVAILABLE:
            self._set_fatal_error(
                "missing_dependency",
                "httpx is required for the Bluesky platform plugin",
                retryable=False,
            )
            return False

        if not self.handle or not self.password:
            logger.error("Bluesky: handle and password must be configured")
            self._set_fatal_error(
                "config_missing",
                "BLUESKY_HANDLE and BLUESKY_PASSWORD must be set",
                retryable=False,
            )
            return False

        # Lock key to prevent multi-profile collision
        try:
            from gateway.status import acquire_scoped_lock
            lock_key = f"bluesky:{self.handle}"
            if not acquire_scoped_lock("bluesky", lock_key):
                logger.error("Bluesky: %s already in use by another profile", self.handle)
                self._set_fatal_error("lock_conflict", "Bluesky handle in use by another profile", retryable=False)
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None

        try:
            # Login immediately to verify credentials
            await self._login()
        except Exception as e:
            logger.error("Bluesky: failed to login — %s", e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            if self._lock_key:
                try:
                    from gateway.status import release_scoped_lock
                    release_scoped_lock("bluesky", self._lock_key)
                except Exception:
                    pass
            return False

        # Start looking for notifications from the connect time onwards
        self.last_seen_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Start background polling task
        self._poll_task = asyncio.create_task(self._poll_loop())

        self._mark_connected()
        logger.info("Bluesky: connected as %s (DID: %s)", self.handle, self.did)
        return True

    async def disconnect(self) -> None:
        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("bluesky", self._lock_key)
            except Exception:
                pass
        self._mark_disconnected()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        self.access_jwt = ""
        self.did = ""
        logger.info("Bluesky: disconnected")

    async def _login(self) -> None:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={
                    "identifier": self.handle,
                    "password": self.password,
                },
                timeout=15.0
            )
            if resp.status_code >= 400:
                raise ValueError(f"Bluesky auth failed ({resp.status_code}): {resp.text}")
            data = resp.json()
            self.did = data["did"]
            self.access_jwt = data["accessJwt"]
            self.refresh_jwt = data.get("refreshJwt", "")
            self.last_login_time = time.time()

    async def _ensure_session(self) -> None:
        if not self.access_jwt or not self.did or (time.time() - self.last_login_time > 3600):
            await self._login()

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self.is_connected:
            return SendResult(success=False, error="Not connected")

        import httpx
        try:
            await self._ensure_session()

            # Resolve target post details
            target_uri = None
            target_cid = None

            if reply_to:
                if "|" in reply_to:
                    target_uri, target_cid = reply_to.split("|", 1)
                else:
                    target_uri = reply_to
                    target_cid = await self._fetch_post_cid(target_uri)
            elif chat_id.startswith("at://"):
                target_uri = chat_id
                target_cid = await self._fetch_post_cid(target_uri)

            # Get root of thread
            root_uri = None
            root_cid = None
            if target_uri:
                root_info = self._thread_roots.get(target_uri)
                if root_info:
                    root_uri = root_info.get("uri")
                    root_cid = root_info.get("cid")
                else:
                    root_info = await self._resolve_thread_root(target_uri)
                    if root_info:
                        root_uri = root_info.get("uri")
                        root_cid = root_info.get("cid")

            # Split content into Bluesky chunks
            chunks = self._split_message(content)
            last_uri = None
            last_cid = None

            async with httpx.AsyncClient() as client:
                for i, chunk in enumerate(chunks):
                    record = {
                        "$type": "app.bsky.feed.post",
                        "text": chunk,
                        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    }
                    
                    facets = _build_facets(chunk)
                    if facets:
                        record["facets"] = facets

                    if i == 0:
                        if target_uri and target_cid:
                            record["reply"] = {
                                "root": {"uri": root_uri or target_uri, "cid": root_cid or target_cid},
                                "parent": {"uri": target_uri, "cid": target_cid}
                            }
                    else:
                        if last_uri and last_cid:
                            record["reply"] = {
                                "root": {"uri": root_uri or target_uri or last_uri, "cid": root_cid or target_cid or last_cid},
                                "parent": {"uri": last_uri, "cid": last_cid}
                            }

                    resp = await client.post(
                        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                        json={
                            "repo": self.did,
                            "collection": "app.bsky.feed.post",
                            "record": record
                        },
                        headers={"Authorization": f"Bearer {self.access_jwt}"},
                        timeout=15.0
                    )
                    if resp.status_code >= 400:
                        return SendResult(success=False, error=f"Post failed ({resp.status_code}): {resp.text}")
                    
                    post_data = resp.json()
                    last_uri = post_data["uri"]
                    last_cid = post_data["cid"]
                    
                    if root_uri or (target_uri and target_cid):
                        self._thread_roots[last_uri] = {
                            "uri": root_uri or target_uri,
                            "cid": root_cid or target_cid
                        }

            return SendResult(success=True, message_id=last_uri)

        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Bluesky has no typing indicator primitive — no-op."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_post = chat_id.startswith("at://")
        return {
            "name": chat_id,
            "type": "group" if is_post else "dm",
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    def _split_message(self, content: str, max_chars: int = 300) -> list[str]:
        """Convert basic markdown and split content into Bluesky-safe post chunks."""
        content = self._strip_markdown(content)
        if len(content) <= max_chars:
            return [content]
        
        chunks = []
        remaining = content
        while remaining:
            if len(remaining) <= max_chars:
                chunks.append(remaining)
                break
            split_idx = remaining.rfind("\n", 0, max_chars)
            if split_idx == -1 or split_idx < max_chars // 2:
                split_idx = remaining.rfind(" ", 0, max_chars)
            if split_idx == -1:
                split_idx = max_chars
            
            chunk = remaining[:split_idx].rstrip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_idx:].lstrip()
        return chunks

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Convert basic markdown to plain text for Bluesky."""
        # Code blocks: ```python\ncode\n``` -> code
        text = re.sub(r"```\w*\n?([\s\S]*?)\n?```", r"\1", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        return text

    async def _fetch_post_cid(self, uri: str) -> Optional[str]:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://bsky.social/xrpc/app.bsky.feed.getPosts",
                    params={"uris": [uri]},
                    headers={"Authorization": f"Bearer {self.access_jwt}"},
                    timeout=10.0
                )
                if resp.status_code == 200:
                    posts = resp.json().get("posts", [])
                    if posts:
                        return posts[0].get("cid")
        except Exception as e:
            logger.debug("Bluesky: failed to fetch post CID: %s", e)
        return None

    async def _resolve_thread_root(self, uri: str) -> Optional[dict]:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://bsky.social/xrpc/app.bsky.feed.getPostThread",
                    params={"uri": uri},
                    headers={"Authorization": f"Bearer {self.access_jwt}"},
                    timeout=10.0
                )
                if resp.status_code == 200:
                    thread = resp.json().get("thread", {})
                    current = thread
                    while current.get("parent"):
                        parent_type = current["parent"].get("$type")
                        if parent_type == "app.bsky.feed.defs#threadViewPost":
                            current = current["parent"]
                        else:
                            break
                    root_post = current.get("post", {})
                    if root_post.get("uri") and root_post.get("cid"):
                        return {"uri": root_post["uri"], "cid": root_post["cid"]}
        except Exception as e:
            logger.debug("Bluesky: failed to resolve thread root: %s", e)
        return None

    # ── Notification Polling ──────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        import httpx
        
        while self._running:
            try:
                await self._ensure_session()
                
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://bsky.social/xrpc/app.bsky.notification.listNotifications",
                        headers={"Authorization": f"Bearer {self.access_jwt}"},
                        timeout=15.0
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        notifications = data.get("notifications", [])
                        
                        new_notifications = []
                        for notif in notifications:
                            reason = notif.get("reason")
                            if reason not in ("mention", "reply"):
                                continue
                            
                            is_read = notif.get("isRead", False)
                            indexed_at = notif.get("indexedAt", "")
                            
                            if is_read or indexed_at <= self.last_seen_time:
                                continue
                                
                            new_notifications.append(notif)
                        
                        if new_notifications:
                            new_notifications.sort(key=lambda x: x.get("indexedAt", ""))
                            
                            max_indexed_at = self.last_seen_time
                            for notif in new_notifications:
                                await self._process_notification(notif)
                                max_indexed_at = max(max_indexed_at, notif.get("indexedAt", ""))
                            
                            self.last_seen_time = max_indexed_at
                            
                            await client.post(
                                "https://bsky.social/xrpc/app.bsky.notification.updateSeen",
                                json={"seenAt": max_indexed_at},
                                headers={"Authorization": f"Bearer {self.access_jwt}"},
                                timeout=10.0
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Bluesky: error in poll loop: %s", e)
            
            await asyncio.sleep(self.poll_interval)

    async def _process_notification(self, notif: dict) -> None:
        author = notif.get("author", {})
        sender_did = author.get("did", "")
        sender_handle = author.get("handle", "")
        
        # User auth check
        if self._allowed_users_lower:
            authorized = (
                sender_did.lower() in self._allowed_users_lower or
                sender_handle.lower() in self._allowed_users_lower
            )
            if not authorized and not self.allow_all_users:
                logger.debug("Bluesky: ignoring message from unauthorized user %s", sender_handle)
                return
        elif not self.allow_all_users:
            logger.debug("Bluesky: ignoring message from %s (allow_all_users is false)", sender_handle)
            return

        record = notif.get("record", {})
        text = record.get("text", "")
        uri = notif.get("uri", "")
        cid = notif.get("cid", "")
        
        # Strip bot @mention from start of message
        bot_prefix = f"@{self.handle.lower()}"
        text_lower = text.lower()
        if text_lower.startswith(bot_prefix):
            text = text[len(bot_prefix):].strip()
        else:
            text = re.sub(rf"^@{re.escape(self.handle)}\b\s*", "", text, flags=re.IGNORECASE).strip()

        # Cache thread root information
        reply_info = record.get("reply", {})
        root_info = reply_info.get("root")
        if root_info:
            self._thread_roots[uri] = {
                "uri": root_info.get("uri"),
                "cid": root_info.get("cid")
            }
        else:
            self._thread_roots[uri] = {
                "uri": uri,
                "cid": cid
            }

        chat_id = root_info.get("uri") if root_info else uri
        chat_type = "group" if root_info else "dm"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=sender_handle,
            chat_type=chat_type,
            user_id=sender_did,
            user_name=sender_handle,
        )

        message_id = f"{uri}|{cid}"

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            timestamp=datetime.now()
        )

        if self._message_handler:
            await self.handle_message(event)


# ── Plugin registration ──────────────────────────────────────────────────

def check_requirements() -> bool:
    """Check if Bluesky is configured."""
    handle = os.getenv("BLUESKY_HANDLE", "")
    password = os.getenv("BLUESKY_PASSWORD", "")
    return bool(handle and password) and HTTPX_AVAILABLE


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    handle = os.getenv("BLUESKY_HANDLE") or extra.get("handle", "")
    password = os.getenv("BLUESKY_PASSWORD") or extra.get("password", "")
    return bool(handle and password)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    handle = os.getenv("BLUESKY_HANDLE", "").strip()
    password = os.getenv("BLUESKY_PASSWORD", "").strip()
    if not (handle and password):
        return None
    seed: dict = {
        "handle": handle,
        "password": password,
    }
    poll = os.getenv("BLUESKY_POLL_INTERVAL", "").strip()
    if poll:
        try:
            seed["poll_interval"] = int(poll)
        except ValueError:
            pass
    home = os.getenv("BLUESKY_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("BLUESKY_HOME_CHANNEL_NAME", home),
        }
    return seed


def interactive_setup() -> None:
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Bluesky")
    existing_handle = get_env_value("BLUESKY_HANDLE")
    if existing_handle:
        print_info(f"Bluesky: already configured (handle: {existing_handle})")
        if not prompt_yes_no("Reconfigure Bluesky?", False):
            return

    print_info("Connect Hermes to Bluesky. Relays mentions in your notification feed.")
    print()

    handle = prompt("Bluesky handle (e.g. username.bsky.social)", default=existing_handle or "")
    if not handle:
        print_warning("Handle is required — skipping Bluesky setup")
        return
    save_env_value("BLUESKY_HANDLE", handle.strip())

    password = prompt("Bluesky App Password (Settings -> App Passwords)", password=True)
    if not password:
        print_warning("App Password is required — skipping Bluesky setup")
        return
    save_env_value("BLUESKY_PASSWORD", password.strip())

    poll = prompt("Poll interval in seconds (default 10)", default=get_env_value("BLUESKY_POLL_INTERVAL") or "")
    if poll:
        try:
            save_env_value("BLUESKY_POLL_INTERVAL", str(int(poll)))
        except ValueError:
            pass

    allow_all = prompt_yes_no("Allow anyone on Bluesky to talk to the bot?", False)
    if allow_all:
        save_env_value("BLUESKY_ALLOW_ALL_USERS", "true")
        save_env_value("BLUESKY_ALLOWED_USERS", "")
    else:
        save_env_value("BLUESKY_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed handles/DIDs (comma-separated, leave empty to deny everyone)",
            default=get_env_value("BLUESKY_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("BLUESKY_ALLOWED_USERS", allowed.replace(" ", ""))

    home = prompt(
        "Default post URI for cron delivery (optional, home channel)",
        default=get_env_value("BLUESKY_HOME_CHANNEL") or "",
    )
    if home:
        save_env_value("BLUESKY_HOME_CHANNEL", home.strip())

    print()
    print_success("Bluesky configuration saved to ~/.hermes/.env")


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process standalone delivery function for Bluesky cron jobs."""
    import httpx
    extra = getattr(pconfig, "extra", {}) or {}
    handle = os.getenv("BLUESKY_HANDLE") or extra.get("handle", "")
    password = os.getenv("BLUESKY_PASSWORD") or extra.get("password", "")
    if not handle or not password:
        return {"error": "Bluesky standalone send: BLUESKY_HANDLE and BLUESKY_PASSWORD must be configured"}

    target = chat_id or os.getenv("BLUESKY_HOME_CHANNEL", "") or extra.get("home_channel", {}).get("chat_id", "")
    if not target:
        return {"error": "Bluesky standalone send: chat_id or BLUESKY_HOME_CHANNEL is required"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={"identifier": handle, "password": password},
                timeout=15.0
            )
            if resp.status_code >= 400:
                return {"error": f"Bluesky login failed: {resp.text}"}
            data = resp.json()
            access_jwt = data["accessJwt"]
            bot_did = data["did"]

            headers = {
                "Authorization": f"Bearer {access_jwt}",
                "Content-Type": "application/json"
            }

            # Strip markdown and link-split message
            adapter_dummy = BlueskyAdapter(pconfig)
            chunks = adapter_dummy._split_message(message)
            
            last_uri = None
            last_cid = None
            
            # Resolve original target
            target_uri = None
            target_cid = None
            if target.startswith("at://"):
                target_uri = target
                # Fetch target's CID
                posts_resp = await client.get(
                    "https://bsky.social/xrpc/app.bsky.feed.getPosts",
                    params={"uris": [target_uri]},
                    headers={"Authorization": f"Bearer {access_jwt}"},
                    timeout=10.0
                )
                if posts_resp.status_code == 200:
                    posts = posts_resp.json().get("posts", [])
                    if posts:
                        target_cid = posts[0].get("cid")

            # Resolve thread root
            root_uri = None
            root_cid = None
            if target_uri:
                thread_resp = await client.get(
                    "https://bsky.social/xrpc/app.bsky.feed.getPostThread",
                    params={"uri": target_uri},
                    headers={"Authorization": f"Bearer {access_jwt}"},
                    timeout=10.0
                )
                if thread_resp.status_code == 200:
                    thread = thread_resp.json().get("thread", {})
                    current = thread
                    while current.get("parent"):
                        parent_type = current["parent"].get("$type")
                        if parent_type == "app.bsky.feed.defs#threadViewPost":
                            current = current["parent"]
                        else:
                            break
                    root_post = current.get("post", {})
                    root_uri = root_post.get("uri")
                    root_cid = root_post.get("cid")

            for i, chunk in enumerate(chunks):
                record = {
                    "$type": "app.bsky.feed.post",
                    "text": chunk,
                    "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                }
                
                facets = _build_facets(chunk)
                if facets:
                    record["facets"] = facets

                if i == 0:
                    if target_uri and target_cid:
                        record["reply"] = {
                            "root": {"uri": root_uri or target_uri, "cid": root_cid or target_cid},
                            "parent": {"uri": target_uri, "cid": target_cid}
                        }
                else:
                    if last_uri and last_cid:
                        record["reply"] = {
                            "root": {"uri": root_uri or target_uri or last_uri, "cid": root_cid or target_cid or last_cid},
                            "parent": {"uri": last_uri, "cid": last_cid}
                        }

                post_resp = await client.post(
                    "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                    json={
                        "repo": bot_did,
                        "collection": "app.bsky.feed.post",
                        "record": record
                    },
                    headers=headers,
                    timeout=15.0
                )
                if post_resp.status_code >= 400:
                    return {"error": f"Bluesky post failed: {post_resp.text}"}
                
                post_data = post_resp.json()
                last_uri = post_data["uri"]
                last_cid = post_data["cid"]

            return {"success": True, "message_id": last_uri}

    except Exception as e:
        return {"error": f"Bluesky standalone send exception: {e}"}


def register(ctx) -> None:
    """Plugin entry point."""
    ctx.register_platform(
        name="bluesky",
        label="Bluesky",
        adapter_factory=lambda cfg: BlueskyAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["BLUESKY_HANDLE", "BLUESKY_PASSWORD"],
        install_hint="httpx is required (part of base hermes dependencies)",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="BLUESKY_HOME_CHANNEL",
        allowed_users_env="BLUESKY_ALLOWED_USERS",
        allow_all_env="BLUESKY_ALLOW_ALL_USERS",
        max_message_length=300,
        platform_hint=(
            "You are chatting via Bluesky. It does not support native markdown formatting. "
            "Instead, use links in plain text format and the platform adapter will render them."
        ),
        emoji="🦋",
        standalone_sender_fn=_standalone_send,
    )
