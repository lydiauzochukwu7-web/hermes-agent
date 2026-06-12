"""Tests for the Bluesky platform adapter plugin."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/bluesky/adapter.py under a unique module name
_bluesky_mod = load_plugin_adapter("bluesky")

_build_facets = _bluesky_mod._build_facets
_parse_bool = _bluesky_mod._parse_bool
BlueskyAdapter = _bluesky_mod.BlueskyAdapter
check_requirements = _bluesky_mod.check_requirements
validate_config = _bluesky_mod.validate_config
register = _bluesky_mod.register
_standalone_send = _bluesky_mod._standalone_send


class TestBlueskyHelpers:

    def test_parse_bool(self):
        assert _parse_bool(True) is True
        assert _parse_bool(False) is False
        assert _parse_bool("true") is True
        assert _parse_bool("yes") is True
        assert _parse_bool("1") is True
        assert _parse_bool("false") is False
        assert _parse_bool("no") is False
        assert _parse_bool("0") is False
        assert _parse_bool("other", default=True) is True

    def test_build_facets_simple(self):
        text = "Hello check out https://example.com/foo and http://test.org"
        facets = _build_facets(text)
        assert len(facets) == 2
        
        # Verify first facet
        assert facets[0]["features"][0]["uri"] == "https://example.com/foo"
        start = facets[0]["index"]["byteStart"]
        end = facets[0]["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end] == b"https://example.com/foo"

        # Verify second facet
        assert facets[1]["features"][0]["uri"] == "http://test.org"
        start = facets[1]["index"]["byteStart"]
        end = facets[1]["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end] == b"http://test.org"

    def test_build_facets_unicode_offset(self):
        # "👋" is 4 bytes in UTF-8
        text = "👋 Hello https://example.com"
        facets = _build_facets(text)
        assert len(facets) == 1
        
        start = facets[0]["index"]["byteStart"]
        end = facets[0]["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end] == b"https://example.com"
        # 👋 (4 bytes) + space (1 byte) + Hello (5 bytes) + space (1 byte) = 11 bytes offset
        assert start == 11

    def test_build_facets_punctuation(self):
        text = "Search here: (https://google.com)."
        facets = _build_facets(text)
        assert len(facets) == 1
        assert facets[0]["features"][0]["uri"] == "https://google.com"
        start = facets[0]["index"]["byteStart"]
        end = facets[0]["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end] == b"https://google.com"

    def test_strip_markdown(self):
        strip = BlueskyAdapter._strip_markdown
        assert strip("**bold**") == "bold"
        assert strip("__bold__") == "bold"
        assert strip("*italic*") == "italic"
        assert strip("_italic_") == "italic"
        assert strip("`code`") == "code"
        assert strip("```python\nprint(1)\n```") == "print(1)"
        assert strip("![alt](https://example.com/img.png)") == "https://example.com/img.png"
        assert strip("[click here](https://example.com)") == "click here (https://example.com)"


class TestBlueskyAdapterInit:

    def test_init_from_env(self, monkeypatch):
        monkeypatch.setenv("BLUESKY_HANDLE", "bot.bsky.social")
        monkeypatch.setenv("BLUESKY_PASSWORD", "secret-password")
        monkeypatch.setenv("BLUESKY_POLL_INTERVAL", "5")
        monkeypatch.setenv("BLUESKY_ALLOW_ALL_USERS", "true")

        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True)
        adapter = BlueskyAdapter(cfg)

        assert adapter.handle == "bot.bsky.social"
        assert adapter.password == "secret-password"
        assert adapter.poll_interval == 5
        assert adapter.allow_all_users is True

    def test_init_from_config_extra(self, monkeypatch):
        for key in ("BLUESKY_HANDLE", "BLUESKY_PASSWORD", "BLUESKY_POLL_INTERVAL", "BLUESKY_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)

        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "handle": "config.bsky.social",
                "password": "config-password",
                "poll_interval": 15,
                "allow_all_users": True,
            },
        )
        adapter = BlueskyAdapter(cfg)

        assert adapter.handle == "config.bsky.social"
        assert adapter.password == "config-password"
        assert adapter.poll_interval == 15
        assert adapter.allow_all_users is True


class TestBlueskyAdapterMessageSplitting:

    def test_split_message_short(self):
        from gateway.config import PlatformConfig
        adapter = BlueskyAdapter(PlatformConfig(enabled=True))
        text = "Hello world"
        assert adapter._split_message(text) == ["Hello world"]

    def test_split_message_long(self):
        from gateway.config import PlatformConfig
        adapter = BlueskyAdapter(PlatformConfig(enabled=True))
        # 700 character text
        text = " ".join(["word"] * 150)
        chunks = adapter._split_message(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 300


class TestBlueskyRequirementsAndValidation:

    def test_check_requirements(self, monkeypatch):
        monkeypatch.setenv("BLUESKY_HANDLE", "user.bsky.social")
        monkeypatch.setenv("BLUESKY_PASSWORD", "pass")
        assert check_requirements() is True

    def test_check_requirements_missing(self, monkeypatch):
        monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
        assert check_requirements() is False

    def test_validate_config(self, monkeypatch):
        monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
        monkeypatch.delenv("BLUESKY_PASSWORD", raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(extra={"handle": "user", "password": "pwd"})
        assert validate_config(cfg) is True


class TestBlueskyAdapterLifecycle:

    @pytest.mark.asyncio
    async def test_connect_and_login_success(self, monkeypatch):
        monkeypatch.setenv("BLUESKY_HANDLE", "bot.bsky.social")
        monkeypatch.setenv("BLUESKY_PASSWORD", "pass")

        from gateway.config import PlatformConfig
        adapter = BlueskyAdapter(PlatformConfig(enabled=True))

        # Mock login methods
        adapter._login = AsyncMock()
        adapter._poll_loop = AsyncMock()
        adapter._mark_connected = MagicMock()

        # Prevent background poll task execution
        async def mock_poll_loop():
            pass
        monkeypatch.setattr(adapter, "_poll_loop", mock_poll_loop)

        assert await adapter.connect() is True
        assert adapter._running is True
        adapter._login.assert_called_once()
        
        # Cleanup
        await adapter.disconnect()
        assert adapter._running is False

    @pytest.mark.asyncio
    async def test_connect_login_fail(self, monkeypatch):
        monkeypatch.setenv("BLUESKY_HANDLE", "bot.bsky.social")
        monkeypatch.setenv("BLUESKY_PASSWORD", "pass")

        from gateway.config import PlatformConfig
        adapter = BlueskyAdapter(PlatformConfig(enabled=True))

        async def fail_login():
            raise ValueError("Invalid credentials")

        monkeypatch.setattr(adapter, "_login", fail_login)
        adapter._set_fatal_error = MagicMock()

        assert await adapter.connect() is False
        adapter._set_fatal_error.assert_called_once()


class TestBlueskyAdapterSend:

    @pytest.fixture
    def adapter(self, monkeypatch):
        monkeypatch.setenv("BLUESKY_HANDLE", "bot.bsky.social")
        monkeypatch.setenv("BLUESKY_PASSWORD", "pass")
        from gateway.config import PlatformConfig
        a = BlueskyAdapter(PlatformConfig(enabled=True))
        a.did = "did:plc:bot"
        a.access_jwt = "token"
        a._running = True
        return a

    @pytest.mark.asyncio
    async def test_send_success(self, adapter, monkeypatch):
        adapter._ensure_session = AsyncMock()
        
        # Mock httpx POST createRecord
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"uri": "at://did:plc:bot/app.bsky.feed.post/123", "cid": "post_cid"})
        
        mock_post = AsyncMock(return_value=mock_response)
        monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

        result = await adapter.send("chat_id", "hello world")
        assert result.success is True
        assert result.message_id == "at://did:plc:bot/app.bsky.feed.post/123"

        # Verify createRecord payload details
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["record"]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_send_reply_mapping(self, adapter, monkeypatch):
        adapter._ensure_session = AsyncMock()
        adapter._thread_roots = {
            "at://did:plc:user/app.bsky.feed.post/parent": {
                "uri": "at://did:plc:user/app.bsky.feed.post/root",
                "cid": "root_cid"
            }
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"uri": "at://did:plc:bot/app.bsky.feed.post/new", "cid": "new_cid"})
        mock_post = AsyncMock(return_value=mock_response)
        monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

        result = await adapter.send(
            "at://did:plc:user/app.bsky.feed.post/root",
            "reply message",
            reply_to="at://did:plc:user/app.bsky.feed.post/parent|parent_cid"
        )
        assert result.success is True
        
        # Verify reply reference in post body
        call_json = mock_post.call_args[1]["json"]
        reply = call_json["record"]["reply"]
        assert reply["root"]["uri"] == "at://did:plc:user/app.bsky.feed.post/root"
        assert reply["root"]["cid"] == "root_cid"
        assert reply["parent"]["uri"] == "at://did:plc:user/app.bsky.feed.post/parent"
        assert reply["parent"]["cid"] == "parent_cid"


class TestBlueskyStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_creates_session_and_posts(self, monkeypatch):
        from gateway.config import PlatformConfig
        monkeypatch.setenv("BLUESKY_HANDLE", "bot.bsky.social")
        monkeypatch.setenv("BLUESKY_PASSWORD", "pass")

        # Mock login and createRecord POST requests
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json = MagicMock(return_value={"did": "did:plc:bot", "accessJwt": "token"})

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json = MagicMock(return_value={"uri": "at://did:plc:bot/app.bsky.feed.post/123", "cid": "post_cid"})

        # Sequential Mock Responses for httpx Client
        responses = [login_resp, post_resp]
        
        async def mock_post(url, *args, **kwargs):
            return responses.pop(0)

        monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

        result = await _standalone_send(
            PlatformConfig(enabled=True),
            "target_user",
            "hello from standalone"
        )

        assert result["success"] is True
        assert result["message_id"] == "at://did:plc:bot/app.bsky.feed.post/123"
