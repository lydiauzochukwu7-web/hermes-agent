# 🦋 Bluesky Platform Plugin

<p align="center">
  <img src="https://lydiamofunanya.com/assets/lydia_portrait_1779977764187-c8RzIoqn.png" alt="Lydia Uzochukwu — Contributor" width="160" style="border-radius: 50%;">
</p>

<p align="center">
  <strong>Built by <a href="https://github.com/lydiauzochukwu7-web">Lydia Uzochukwu</a></strong><br>
  <em>feat(gateway): add Bluesky platform adapter plugin</em>
</p>

This plugin connects **Hermes Agent** to [Bluesky](https://bsky.app) — the decentralized social network built on the AT Protocol. Once configured, Hermes monitors your Bluesky notifications and responds to mentions and thread replies directly in your feed.

---

## What It Does

| Feature | Details |
|---|---|
| **Notification polling** | Periodically checks `app.bsky.notification.listNotifications` for new mentions and replies |
| **Thread-aware replies** | Tracks root and parent post URIs/CIDs so replies slot correctly into Bluesky threads |
| **Markdown stripping** | Converts agent responses to plain text so they render cleanly in the Bluesky UI |
| **Rich facets** | Automatically generates clickable links and `@mention` facets in outgoing posts |
| **Configurable limits** | Polling interval, character limit, and reply cooldown are all tunable |

---

## Requirements

- A Bluesky account at [bsky.app](https://bsky.app)
- A **Bluesky App Password** (not your main login password)
- Hermes Agent installed and the gateway configured

---

## Setup

### Step 1 — Install the plugin

```bash
hermes plugins install bluesky
```

### Step 2 — Generate a Bluesky App Password

1. Log into [bsky.app](https://bsky.app)
2. Go to **Settings → Privacy and Security → App Passwords**
3. Click **Add App Password**, give it a name (e.g. `hermes`), and copy the generated password

### Step 3 — Add your credentials

Open `~/.hermes/.env` and add:

```env
BLUESKY_HANDLE=yourhandle.bsky.social
BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

> ⚠️ Use your App Password here, **not** your account password. App Passwords are scoped and can be revoked without affecting your account.

### Step 4 — Start the gateway

```bash
hermes gateway start
```

Hermes will authenticate with Bluesky and begin polling for notifications. Mention your bot account in any post and it will reply.

---

## Configuration

Add any of these optional settings to `~/.hermes/config.yaml`:

```yaml
gateway:
  bluesky:
    polling_interval: 30      # How often to check for new notifications (seconds). Default: 30
    char_limit: 300           # Maximum characters per post. Default: 300 (Bluesky's current limit)
    reply_cooldown: 5         # Minimum seconds between consecutive replies. Default: 5
```

---

## How to Use

Once the gateway is running, interact with Hermes on Bluesky the same way you would on any other platform:

1. **Mention your bot** in a post:
   ```
   @yourbot.bsky.social what's the weather like in Lagos?
   ```
2. **Reply to any of its posts** to continue the conversation — Hermes tracks the thread context automatically.
3. Use **slash commands** the same way as on other platforms:
   ```
   @yourbot.bsky.social /new
   @yourbot.bsky.social /model gpt-4o
   ```

> **Note:** Bluesky has a 300-character post limit. If Hermes's response is longer, it will be truncated to fit. For long-form answers, consider using another platform like Telegram or the CLI.

---

## File Structure

```
plugins/platforms/bluesky/
├── __init__.py      # Plugin entry point — registers the adapter with Hermes
├── adapter.py       # Core adapter: polling, authentication, send/receive logic
├── plugin.yaml      # Plugin manifest — metadata, dependencies, config schema
└── README.md        # This file
```

---

## How It Works (Technical Overview)

```
Hermes Gateway Loop
       │
       ▼
BlueSkyAdapter.poll_notifications()
  └── GET app.bsky.notification.listNotifications
         │
         ▼
  Filter: reason = "mention" or "reply"
         │
         ▼
  Resolve thread root (URI + CID)
  Track parent post (URI + CID)
         │
         ▼
  Dispatch to agent → get response text
         │
         ▼
  _strip_markdown()  →  plain text
  _extract_facets()  →  URL + @mention links
         │
         ▼
  POST com.atproto.repo.createRecord
    (reply with root + parent refs)
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 Unauthorized` on startup | Check `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in `.env` |
| Bot not responding to mentions | Make sure the gateway is running (`hermes gateway start`) and the handle matches your account exactly |
| Replies appearing out of thread | This is an AT Protocol timing issue — the plugin handles root/parent tracking correctly; try increasing `reply_cooldown` |
| Response truncated | Bluesky's 300-char limit is enforced — use `hermes gateway start` from the CLI for full-length answers |

---

## Contributing

Found a bug or want to improve the plugin? Open an issue or PR at:
**https://github.com/lydiauzochukwu7-web/hermes-agent**

---

*Part of the [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin ecosystem.*
