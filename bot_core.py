#!/usr/bin/env python3
"""
================================================================================
 Instagram Status Monitor — Shared Core (bot_core.py)
================================================================================
Imported by bot_basic.py and bot_advanced.py. Do NOT run this file directly.

This is the original single-file bot's logic, split so the same core
(polling, case-rotation, persistence, auth, keep-alive server) can back two
different Discord bot entrypoints without duplicating or drifting the
underlying behavior. Env vars, deployment method, and the case-rotation /
polling / confirmation-threshold logic are unchanged from the original.

NEW in this version: per-user channel routing (channel_config.json) via a
/config command, replacing the old single global BAN_CHANNEL_ID /
UNBAN_CHANNEL_ID env vars — needed because each client now has their own
server/channels rather than sharing one deployment's env vars.
================================================================================
"""

import os
import re
import html
import io
import json
import uuid
import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

import discord
from discord import app_commands

load_dotenv()

# ============================================================================
# CONFIGURATION (unchanged env vars / meanings from the original bot)
# ============================================================================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
API_TIMEOUT_SECONDS = int(os.getenv("API_TIMEOUT_SECONDS", "20"))
CONFIRMATION_THRESHOLD = int(os.getenv("CONFIRMATION_THRESHOLD", "2"))
BACKOFF_MAX_SECONDS = int(os.getenv("BACKOFF_MAX_SECONDS", "1800"))  # cap: 30 min
PORT = int(os.getenv("PORT", "8080"))

DATA_FILE = Path(os.getenv("DATA_FILE", "monitored_accounts.json"))
CHANNEL_CONFIG_FILE = Path(os.getenv("CHANNEL_CONFIG_FILE", "channel_config.json"))

# Embed footer text — shared by both bot_basic.py and bot_advanced.py so a
# single env var controls branding across both tiers.
BRAND_FOOTER = os.getenv("BOT_FOOTER_TEXT", "Instagram Monitor — Premium Monitoring")

INTER_CHECK_DELAY_MS = 2000
API_URL = "https://insta-story.com/api/v1/web/profile"
MAX_BULK_ADD = 25

# Shared browser-identity headers for every request to insta-story.com (the
# API) AND to the Instagram CDN image URLs it hands back — both are guarded
# by the same anti-bot checks, so both need to look like they came from the
# same "browser session" hitting insta-story.com.
IG_REQUEST_HEADERS = {
    "Origin": "https://insta-story.com",
    "Referer": "https://insta-story.com/instanavigation",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

if not DISCORD_BOT_TOKEN:
    raise SystemExit("ERROR: DISCORD_BOT_TOKEN is not set in .env")

# ============================================================================
# AUTHORIZATION WITH TIME-LIMITED ACCESS (unchanged)
# ============================================================================
def parse_duration(duration_str: str) -> Optional[timedelta]:
    match = re.fullmatch(r"(\d+)([dhm])", duration_str.strip())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        return timedelta(days=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "m":
        return timedelta(minutes=value)
    return None

_raw_user_ids = os.getenv("AUTHORIZED_USER_IDS", "").strip()
_bot_start_time = datetime.now(timezone.utc)

AUTHORIZED_USERS: dict = {}
for entry in _raw_user_ids.split(","):
    entry = entry.strip()
    if not entry:
        continue
    if ":" in entry:
        uid_str, duration_str = entry.split(":", 1)
        duration = parse_duration(duration_str)
        AUTHORIZED_USERS[int(uid_str)] = (_bot_start_time + duration) if duration else None
    else:
        AUTHORIZED_USERS[int(entry)] = None

if not AUTHORIZED_USERS:
    raise SystemExit("ERROR: AUTHORIZED_USER_IDS is not set in .env")

def is_authorized(user_id: int) -> bool:
    if user_id not in AUTHORIZED_USERS:
        return False
    expiry = AUTHORIZED_USERS[user_id]
    if expiry is None:
        return True
    return datetime.now(timezone.utc) < expiry

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("discord").setLevel(logging.WARNING)
logger = logging.getLogger("igbot")

# ============================================================================
# PERSISTENCE: monitored accounts + per-user channel routing
#
# Backed by Upstash Redis (free tier, survives restarts/redeploys — needed
# since Render's FREE plan has no persistent disk option) if
# UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are set. Falls back to
# local JSON files automatically if they're not set, so local dev/testing
# or a future move to a paid host with a real disk both still work with no
# code changes — only the two env vars decide which backend is used.
# ============================================================================
_storage_lock = asyncio.Lock()
_channel_lock = asyncio.Lock()

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
USE_REDIS = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)

ACCOUNTS_REDIS_KEY = os.getenv("ACCOUNTS_REDIS_KEY", "igbot:monitored_accounts")
CHANNEL_CONFIG_REDIS_KEY = os.getenv("CHANNEL_CONFIG_REDIS_KEY", "igbot:channel_config")

if USE_REDIS:
    logger.info("Persistence backend: Upstash Redis")
else:
    logger.warning(
        "Persistence backend: local JSON files (UPSTASH_REDIS_REST_URL/"
        "TOKEN not set). On Render's free plan this data will NOT survive "
        "a restart or redeploy — set the Upstash env vars to fix that."
    )

def _validate_data(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    for user_key, user_accounts in data.items():
        if not isinstance(user_key, str) or not user_key.startswith("user_"):
            return False
        if not isinstance(user_accounts, dict):
            return False
        for account_key, entry in user_accounts.items():
            if not isinstance(entry, dict):
                return False
            for field in ["username", "status", "last_checked"]:
                if field not in entry:
                    return False
    return True

# --- Upstash Redis REST helpers ---
async def _redis_command(*args) -> Optional[dict]:
    """Runs a single Redis command via Upstash's REST API. Docs:
    https://upstash.com/docs/redis/features/restapi"""
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(UPSTASH_REDIS_REST_URL, json=list(args), headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Redis command failed ({resp.status}): {body[:200]}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.error(f"Redis command error: {e}")
        return None

async def _redis_get_json(key: str) -> dict:
    result = await _redis_command("GET", key)
    if not result or result.get("result") is None:
        return {}
    try:
        return json.loads(result["result"])
    except (json.JSONDecodeError, TypeError):
        logger.error(f"Corrupt JSON in Redis key {key}, starting fresh.")
        return {}

async def _redis_set_json(key: str, data: dict) -> bool:
    result = await _redis_command("SET", key, json.dumps(data, ensure_ascii=False))
    ok = bool(result and result.get("result") == "OK")
    if not ok:
        logger.error(f"Failed to save Redis key {key}")
    return ok

# --- Local file fallback (unchanged from before) ---
def _load_data_file() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load data file: {e}. Starting fresh.")
        return {}

def _save_data_file(data: dict) -> None:
    tmp_path = DATA_FILE.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(DATA_FILE)
    except OSError as e:
        logger.error(f"Failed to save data file: {e}")

def _load_channel_config_file() -> dict:
    if not CHANNEL_CONFIG_FILE.exists():
        return {}
    try:
        with CHANNEL_CONFIG_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load channel config: {e}. Starting fresh.")
        return {}

def _save_channel_config_file(data: dict) -> None:
    tmp_path = CHANNEL_CONFIG_FILE.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(CHANNEL_CONFIG_FILE)
    except OSError as e:
        logger.error(f"Failed to save channel config: {e}")

# --- Public API (used identically by both bots — backend is invisible to them) ---
async def load_data() -> dict:
    async with _storage_lock:
        if USE_REDIS:
            return await _redis_get_json(ACCOUNTS_REDIS_KEY)
        return _load_data_file()

async def save_data(data: dict) -> None:
    async with _storage_lock:
        if not _validate_data(data):
            logger.error("Data validation failed! Not saving to prevent corruption.")
            return
        if USE_REDIS:
            await _redis_set_json(ACCOUNTS_REDIS_KEY, data)
        else:
            _save_data_file(data)

def get_user_key(user_id: int) -> str:
    return f"user_{user_id}"

async def get_user_channels(user_id: int) -> dict:
    """Returns e.g. {'ban': 123, 'unban': 456, 'verify': 789} — a key is only
    present if that user has configured that alert type via /config."""
    async with _channel_lock:
        data = await _redis_get_json(CHANNEL_CONFIG_REDIS_KEY) if USE_REDIS else _load_channel_config_file()
    return data.get(get_user_key(user_id), {})

async def set_user_channel(user_id: int, kind: str, channel_id: Optional[int]) -> None:
    """kind is one of 'ban', 'unban', 'verify'. channel_id=None clears it."""
    async with _channel_lock:
        data = await _redis_get_json(CHANNEL_CONFIG_REDIS_KEY) if USE_REDIS else _load_channel_config_file()
        user_key = get_user_key(user_id)
        if user_key not in data:
            data[user_key] = {}
        if channel_id is None:
            data[user_key].pop(kind, None)
        else:
            data[user_key][kind] = channel_id
        if USE_REDIS:
            await _redis_set_json(CHANNEL_CONFIG_REDIS_KEY, data)
        else:
            _save_channel_config_file(data)

# ============================================================================
# HELPER: Human-Readable Time (unchanged)
# ============================================================================
def relative_time(iso_timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.total_seconds() < 60:
            return "just now"
        elif delta.total_seconds() < 3600:
            minutes = int(delta.total_seconds() // 60)
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        elif delta.total_seconds() < 86400:
            hours = int(delta.total_seconds() // 3600)
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        else:
            days = int(delta.total_seconds() // 86400)
            return f"{days} day{'s' if days > 1 else ''} ago"
    except Exception:
        return "unknown"

def instagram_url(username: str) -> str:
    return f"https://instagram.com/{username}"

def format_duration_hm(seconds: float) -> str:
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"

def compute_time_taken(entry: dict) -> Optional[str]:
    added_at_str = entry.get("added_at")
    if not added_at_str:
        return None
    try:
        added_at = datetime.fromisoformat(added_at_str)
        elapsed = (datetime.now(timezone.utc) - added_at).total_seconds()
        return format_duration_hm(elapsed)
    except Exception:
        return None

# ============================================================================
# STAT CARD IMAGE GENERATION (advanced bot only — basic bot never calls this)
# ============================================================================
import os
import io
import logging
from pathlib import Path
from typing import Optional
import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def _find_font(candidates: list, fallback: str) -> str:
    for path in candidates:
        if os.path.exists(path):
            return path
    return fallback

_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Inter is installed via `apt-get install fonts-inter` (see Dockerfile) — it's
# what gives the card a premium, non-default-PIL-font look. Falls back
# through Roboto, then DejaVu, so this never crashes on a bare host.
FONT_BOLD = _find_font([
    "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf",
    "/usr/share/fonts/truetype/roboto-fontface/roboto/Roboto-Bold.ttf",
], _DEJAVU_BOLD)
FONT_SEMIBOLD = _find_font([
    "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Medium.ttf",
], FONT_BOLD)
FONT_MEDIUM = _find_font([
    "/usr/share/fonts/opentype/inter/Inter-Medium.otf",
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Medium.ttf",
    "/usr/share/fonts/truetype/roboto-fontface/roboto/Roboto-Medium.ttf",
], FONT_BOLD)
FONT_REGULAR = _find_font([
    "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf",
    "/usr/share/fonts/truetype/roboto-fontface/roboto/Roboto-Regular.ttf",
], _DEJAVU_REGULAR)

CARDS_DIR = Path(os.getenv("CARDS_DIR", "cards"))
CARDS_DIR.mkdir(parents=True, exist_ok=True)

# Sampled directly from the reference card — a soft charcoal, not pure black.
CARD_BG = (18, 18, 20)


def format_count(n) -> str:
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}K".replace(".0K", "K")
    return str(n)


def _center_crop_square(img: Image.Image) -> Image.Image:
    """Crop the longer side down so the image is square before it goes into
    the circular mask — avoids squishing non-square avatars and keeps the
    subject centered instead of stretched off to one side."""
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def make_circular(img: Image.Image, size: int) -> Image.Image:
    img = _center_crop_square(img.convert("RGBA")).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


async def fetch_profile_pic_bytes(profile_pic_url: Optional[str]) -> Optional[bytes]:
    if not profile_pic_url:
        return None
    # The profile_pic_url comes back from insta-story.com's API, and its
    # image host enforces the same anti-bot checks as the API endpoint
    # itself — a bare aiohttp request (no Origin/Referer/browser UA) gets
    # rejected, which is why the avatar was silently falling back to the
    # gray placeholder circle. Sending it with the same headers used for
    # the insta-story.com API call fixes that.
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        **IG_REQUEST_HEADERS,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(profile_pic_url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"Profile picture fetch got HTTP {resp.status}")
    except Exception as e:
        logger.warning(f"Failed to fetch profile picture: {e}")
    return None


def _pill(d: ImageDraw.ImageDraw, x, y, w, h, color, text):
    """Original top-left anchored pill — kept as-is in case anything else
    in the file calls this directly."""
    d.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=color)
    f = ImageFont.truetype(FONT_BOLD, 24)
    tw = d.textlength(text, font=f)
    d.text((x + w / 2 - tw / 2, y + 13), text, font=f, fill="white")


def _pill_centered(d: ImageDraw.ImageDraw, x, y_center, w, h, color, text, font_size=26):
    """Vertically-centered pill, used in the single-row header below."""
    top = y_center - h / 2
    d.rounded_rectangle((x, top, x + w, top + h), radius=h / 2, fill=color)
    f = ImageFont.truetype(FONT_BOLD, font_size)
    d.text((x + w / 2, y_center), text, font=f, fill="white", anchor="mm")


def _draw_verified_badge(d: ImageDraw.ImageDraw, cx, cy, r=16, color=(0, 149, 246), outline=CARD_BG):
    d.ellipse((cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3), fill=outline)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    d.line(
        [(cx - r * 0.5, cy + r * 0.05), (cx - r * 0.05, cy + r * 0.45), (cx + r * 0.55, cy - r * 0.35)],
        fill="white", width=3, joint="curve",
    )


def _draw_dots(d: ImageDraw.ImageDraw, x, y, color=(140, 140, 140), r=4, gap=15):
    for i in range(3):
        cx = x + i * gap
        d.ellipse((cx - r, y - r, cx + r, y + r), fill=color)
    return x + 2 * gap + r


def _draw_stat(d: ImageDraw.ImageDraw, x, y, number, label, font_num, font_label, gap_after=44):
    d.text((x, y), number, font=font_num, fill="white", anchor="lm")
    num_w = d.textlength(number, font=font_num)
    label_x = x + num_w + 8
    d.text((label_x, y), label, font=font_label, fill=(163, 163, 163), anchor="lm")
    label_w = d.textlength(label, font=font_label)
    return label_x + label_w + gap_after


STATUS_PILL_COLORS = {
    "RECOVERED": (56, 193, 114),
    "BANNED": (224, 54, 54),
    "VERIFIED": (0, 149, 246),
    "EXPIRED": (230, 160, 30),
}
RING_COLOR_DEFAULT = (70, 70, 70)
RING_COLOR_GREEN = (56, 193, 114)
RING_COLOR_RED = (224, 54, 54)
RING_COLOR_BLUE = (0, 149, 246)
RING_COLOR_YELLOW = (230, 160, 30)
CARD_CORNER_RADIUS = 40


def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width, img.height), radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


# Layout constants measured directly off the reference screenshot (1170x453).
W, H = 1170, 453
AX, AY, ASZ = 60, 95, 280          # avatar box
RING_PAD, RING_WIDTH = 6, 7
TX = 379                            # content column left edge
ROW1_Y = 148                        # username / badge / pill / dots
ROW2_Y = 246                        # stats
ROW3_Y = 309                        # bio


def generate_stat_card(username, status, followers=None, following=None, posts=None,
                        profile_pic_bytes=None, is_verified=False,
                        status_label=None, ring_color=None, bio=None) -> str:
    """
    Two variants, styled after Instagram's dark-mode profile header:
      - "active": avatar, live stats, a single header row (username +
        optional verified badge + colored status pill + "..." menu), a
        stats row, and a bio/full-name line — used for RECOVERED (green),
        VERIFIED (blue), and EXPIRED (yellow) cards.
      - anything else ("banned" / not found): muted gray avatar with a red
        X overlay, username forced to "UserNotFound", stats forced to
        0/0/0.

    status_label: "RECOVERED" / "BANNED" / "VERIFIED" / "EXPIRED" — picks
    the pill color/text from STATUS_PILL_COLORS. Falls back to a plain
    blue "Follow" pill if omitted.

    ring_color: overrides the avatar ring color. Pass one of the
    RING_COLOR_* constants to match status_label.

    bio: optional line of gray text under the stats row (e.g. full name).
    """
    img = Image.new("RGB", (W, H), CARD_BG)
    d = ImageDraw.Draw(img)
    fb = ImageFont.truetype(FONT_BOLD, 46)
    fr = ImageFont.truetype(FONT_REGULAR, 26)
    fs = ImageFont.truetype(FONT_BOLD, 34)

    ring = ring_color or RING_COLOR_DEFAULT
    d.ellipse(
        (AX - RING_PAD, AY - RING_PAD, AX + ASZ + RING_PAD, AY + ASZ + RING_PAD),
        outline=ring, width=RING_WIDTH,
    )

    pill_color = (0, 149, 246)
    pill_text = "Follow"
    if status_label and status_label in STATUS_PILL_COLORS:
        pill_color = STATUS_PILL_COLORS[status_label]
        pill_text = status_label

    if status != "active":
        # --- banned / not-found branch ---
        d.ellipse((AX, AY, AX + ASZ, AY + ASZ), fill=(80, 80, 80))
        pad = ASZ * 0.2
        d.line((AX + pad, AY + pad, AX + ASZ - pad, AY + ASZ - pad), fill=(214, 45, 45), width=14)
        d.line((AX + ASZ - pad, AY + pad, AX + pad, AY + ASZ - pad), fill=(214, 45, 45), width=14)

        d.text((TX, ROW1_Y), "UserNotFound", font=fb, fill="white", anchor="lm")
        pill_w = max(160, len(pill_text) * 17 + 50)
        _pill_centered(d, TX, ROW1_Y + 62, pill_w, 54, pill_color, pill_text)

        d.text((TX, ROW2_Y), "0 posts", font=fr, fill=(163, 163, 163))
        d.text((TX + 190, ROW2_Y), "0 followers", font=fr, fill=(163, 163, 163))
        d.text((TX + 440, ROW2_Y), "0 following", font=fr, fill=(163, 163, 163))
        d.text((TX, ROW3_Y), "UserNotFound", font=fr, fill=(130, 130, 130))

        p = CARDS_DIR / f"card_{username}_{status}.png"
        _round_corners(img, CARD_CORNER_RADIUS).save(p)
        return str(p)

    # --- active branch ---
    if profile_pic_bytes:
        try:
            av = make_circular(Image.open(io.BytesIO(profile_pic_bytes)), ASZ)
            img.paste(av, (AX, AY), av)
        except Exception:
            d.ellipse((AX, AY, AX + ASZ, AY + ASZ), fill=(60, 60, 60))
    else:
        d.ellipse((AX, AY, AX + ASZ, AY + ASZ), fill=(60, 60, 60))

    # Row 1: username + verified badge + status pill + "..." menu, all inline
    d.text((TX, ROW1_Y), username, font=fb, fill="white", anchor="lm")
    x = TX + d.textlength(username, font=fb) + 20

    if is_verified:
        _draw_verified_badge(d, x + 16, ROW1_Y - 2, r=16)
        x += 32 + 24

    pill_w = max(150, len(pill_text) * 17 + 50)
    _pill_centered(d, x, ROW1_Y, pill_w, 54, pill_color, pill_text)
    x += pill_w + 30

    _draw_dots(d, x, ROW1_Y)

    # Row 2: stats, sequential single line
    x = TX
    x = _draw_stat(d, x, ROW2_Y, str(posts or 0), "posts", fs, fr)
    x = _draw_stat(d, x, ROW2_Y, format_count(followers), "followers", fs, fr)
    _draw_stat(d, x, ROW2_Y, format_count(following), "following", fs, fr)

    # Row 3: bio / full name
    if bio:
        d.text((TX, ROW3_Y), bio, font=fr, fill=(163, 163, 163), anchor="lm")

    tag = (status_label or status).lower()
    p = CARDS_DIR / f"card_{username}_{tag}.png"
    _round_corners(img, CARD_CORNER_RADIUS).save(p)
    return str(p)


# ---- convenience wrappers for the four status themes ----

def generate_recovered_card(username, followers, following, posts, profile_pic_bytes=None,
                             is_verified=True, bio=None):
    return generate_stat_card(username, "active", followers, following, posts,
                               profile_pic_bytes, is_verified,
                               status_label="RECOVERED", ring_color=RING_COLOR_GREEN, bio=bio)


def generate_banned_card(username):
    return generate_stat_card(username, "banned", status_label="BANNED", ring_color=RING_COLOR_RED)


def generate_verified_card(username, followers, following, posts, profile_pic_bytes=None, bio=None):
    return generate_stat_card(username, "active", followers, following, posts,
                               profile_pic_bytes, is_verified=True,
                               status_label="VERIFIED", ring_color=RING_COLOR_BLUE, bio=bio)


def generate_expired_card(username, followers, following, posts, profile_pic_bytes=None,
                           is_verified=False, bio=None):
    return generate_stat_card(username, "active", followers, following, posts,
                               profile_pic_bytes, is_verified,
                               status_label="EXPIRED", ring_color=RING_COLOR_YELLOW, bio=bio)


# ============================================================================
# INSTAGRAM STATUS CHECKER — case rotation + API call (UNCHANGED LOGIC)
# ============================================================================
def count_alpha_chars(username: str) -> int:
    return sum(1 for c in username if c.isalpha())

def generate_case_variant(original_username: str, case_index: int) -> str:
    letters = count_alpha_chars(original_username)
    if letters == 0:
        return original_username
    total = 1 << letters
    case_index %= total
    result = []
    bit_pos = 0
    for ch in original_username:
        if ch.isalpha():
            if (case_index >> bit_pos) & 1:
                result.append(ch.upper())
            else:
                result.append(ch.lower())
            bit_pos += 1
        else:
            result.append(ch)
    return "".join(result)

def next_case_index(original_username: str, current_index: int) -> int:
    letters = count_alpha_chars(original_username)
    if letters == 0:
        return 0
    total = 1 << letters
    return (current_index + 1) % total

class CheckResult:
    def __init__(self, status: str, followers: Optional[int] = None,
                 following: Optional[int] = None, posts: Optional[int] = None,
                 is_verified: bool = False, profile_pic_url: Optional[str] = None, note: str = "",
                 failed: bool = False):
        self.status = status  # "active" or "suspended" only
        self.followers = followers
        self.following = following
        self.posts = posts
        self.is_verified = is_verified
        self.profile_pic_url = profile_pic_url
        self.note = note
        # failed=True means we could NOT get a real read (timeout, bad
        # response, rate-limited after retries) — status is a fallback
        # value only, not a confirmed observation, and must NOT count as
        # evidence toward a ban/unban threshold. failed=False means we got
        # a real response from the API, whether that response said the
        # account exists or not.
        self.failed = failed

async def check_instagram_status(username: str, retries: int = 2) -> CheckResult:
    payload = {
        "username": username,
        "visitor_id": str(uuid.uuid4()),
        "user_info": True,
        "user_stories": False,
        "user_highlights": False,
        "user_posts": False,
    }
    headers = {
        "Content-Type": "application/json",
        **IG_REQUEST_HEADERS,
    }

    for attempt in range(retries + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(API_URL, json=payload, headers=headers) as resp:
                    status_code = resp.status
                    if status_code == 429:
                        logger.warning(f"@{username}: rate-limited (attempt {attempt+1})")
                        if attempt < retries:
                            await asyncio.sleep(3)
                            continue
                        return CheckResult("suspended", note="rate-limited after retries", failed=True)

                    try:
                        data = await resp.json()
                    except Exception:
                        if attempt < retries:
                            await asyncio.sleep(2)
                            continue
                        return CheckResult("suspended", note="non-JSON response after retries", failed=True)

                    user_info = data.get("user_info")
                    if isinstance(user_info, dict) and user_info.get("id"):
                        followers = user_info.get("followers")
                        following = user_info.get("following")
                        posts = user_info.get("posts")
                        is_verified = bool(user_info.get("is_verified", False))
                        profile_pic_url = user_info.get("profile_pic_url")
                        return CheckResult("active", followers, following, posts, is_verified, profile_pic_url, note="", failed=False)

                    # A real, valid JSON response with no user_info is a
                    # genuine "this account was not found" — not a failure.
                    return CheckResult("suspended", note="", failed=False)

        except asyncio.TimeoutError:
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            return CheckResult("suspended", note="timeout after retries", failed=True)
        except Exception as e:
            logger.error(f"@{username}: request error (attempt {attempt+1}): {e}")
            if attempt < retries:
                await asyncio.sleep(2)
                continue
            return CheckResult("suspended", note="", failed=True)

    return CheckResult("suspended", note="max retries exceeded", failed=True)

# ============================================================================
# GENERIC POLLING CYCLE (case-rotation + confirmation-threshold logic
# UNCHANGED from the original bot; the difference between tiers is only in
# what the on_status_change / on_verification_change callbacks DO)
# ============================================================================
async def run_poll_cycle(on_status_change, on_verification_change=None, track_verification=False):
    """
    Only the WORK LIST (which accounts to check) is snapshotted up front.
    Each account's result is read-modify-written individually right after
    it's checked, instead of accumulating all updates in memory and
    overwriting the entire dataset once at the end of the cycle. A full
    cycle can take minutes for a large watchlist; saving only at the end
    meant any account added/removed by a user *during* that window got
    silently discarded when the stale end-of-cycle snapshot was written
    back. Re-reading fresh data before each individual save closes that
    window down to a single account's check instead of the whole cycle.
    """
    snapshot = await load_data()
    if not snapshot:
        return

    work_items = [
        (user_key, account_key)
        for user_key, user_accounts in snapshot.items()
        for account_key in user_accounts
    ]

    for user_key, account_key in work_items:
        user_id = int(user_key.split("_")[1])

        # Re-read fresh right before touching this account, in case it was
        # added/removed/edited elsewhere since the snapshot was taken.
        current_data = await load_data()
        user_accounts = current_data.get(user_key)
        if user_accounts is None or account_key not in user_accounts:
            continue  # removed since the snapshot was taken — don't resurrect it
        entry = user_accounts[account_key]

        # Skip accounts still in a backoff window from recent failed checks
        # (timeouts / bad responses / rate-limits) — retrying on schedule
        # while a service is already struggling just makes it worse, and a
        # failed check must never masquerade as evidence of a real ban.
        backoff_until_str = entry.get("backoff_until")
        if backoff_until_str:
            try:
                if datetime.now(timezone.utc) < datetime.fromisoformat(backoff_until_str):
                    continue
            except Exception:
                pass

        username = entry["username"]
        delay = INTER_CHECK_DELAY_MS / 1000 + random.uniform(0, 1)
        await asyncio.sleep(delay)

        case_index = entry.get("case_index", 0)
        query_variant = generate_case_variant(username, case_index)
        entry["case_index"] = next_case_index(username, case_index)

        try:
            result = await check_instagram_status(query_variant)
        except Exception as e:
            logger.exception(f"Error checking @{username} (variant '{query_variant}'): {e}")
            continue

        entry["last_checked"] = datetime.now(timezone.utc).isoformat()

        if result.failed:
            # A failed check is NOT evidence of anything — don't touch
            # pending_status/pending_count at all. Just back off this
            # account with exponential backoff (capped) so repeated
            # failures don't keep hammering an already-struggling API.
            entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
            backoff_seconds = min(
                POLL_INTERVAL_SECONDS * (2 ** (entry["consecutive_failures"] - 1)),
                BACKOFF_MAX_SECONDS,
            )
            entry["backoff_until"] = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
            logger.warning(
                f"@{username}: check failed ({result.note or 'unknown error'}) — "
                f"backing off {backoff_seconds}s (consecutive failures: {entry['consecutive_failures']})"
            )
        else:
            entry["consecutive_failures"] = 0
            entry["backoff_until"] = None
            time_taken_str = compute_time_taken(entry)

            if result.status != entry["status"]:
                if entry.get("pending_status") == result.status:
                    entry["pending_count"] = entry.get("pending_count", 0) + 1
                else:
                    entry["pending_status"] = result.status
                    entry["pending_count"] = 1

                if entry["pending_count"] >= CONFIRMATION_THRESHOLD:
                    old_status = entry["status"]
                    entry["status"] = result.status
                    entry["pending_status"] = None
                    entry["pending_count"] = 0
                    try:
                        await on_status_change(user_id, account_key, entry, old_status, result.status, result, time_taken_str)
                    except Exception:
                        logger.exception(f"on_status_change handler failed for @{username}")
            else:
                entry["pending_status"] = None
                entry["pending_count"] = 0

            if track_verification and result.status == "active":
                old_verified = bool(entry.get("is_verified", False))
                new_verified = bool(result.is_verified)
                verify_watch = bool(entry.get("verify_watch", False))
                if new_verified != old_verified:
                    entry["is_verified"] = new_verified
                    # Alert only fires for accounts explicitly opted into
                    # verification monitoring (via the dedicated add path) —
                    # the badge value itself is still tracked/shown for
                    # every account regardless, just silently.
                    if verify_watch and on_verification_change:
                        try:
                            await on_verification_change(user_id, account_key, entry, old_verified, new_verified, result, time_taken_str)
                        except Exception:
                            logger.exception(f"on_verification_change handler failed for @{username}")
                elif "is_verified" not in entry:
                    entry["is_verified"] = new_verified

        # Re-read again right before saving — minimizes (does not fully
        # eliminate, without true atomic transactions) the chance of
        # clobbering a change made in the few hundred ms since we last read.
        save_data_snapshot = await load_data()
        save_user_accounts = save_data_snapshot.get(user_key)
        if save_user_accounts is None or account_key not in save_user_accounts:
            continue  # removed while we were checking it — don't re-add it
        save_user_accounts[account_key] = entry
        save_data_snapshot[user_key] = save_user_accounts
        await save_data(save_data_snapshot)

# ============================================================================
# NOTIFICATION DELIVERY (DM always + optional per-user configured channel)
# ============================================================================
async def notify_user(client: discord.Client, user_id: int, kind: str, embed: discord.Embed,
                       card_path: Optional[str] = None, view: Optional[discord.ui.View] = None) -> None:
    """Posts to the channel the user has configured for this `kind`
    ('ban'/'unban'/'verify') via /config, and mirrors to their linked
    Telegram chat via /telegram_relay if set up. DMs are no longer sent —
    if no channel is configured for this kind, the alert has nowhere to go
    and is dropped (logged as a warning so it's visible in your logs)."""
    channels = await get_user_channels(user_id)
    channel_id = channels.get(kind)

    if channel_id:
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
            if card_path:
                channel_file = discord.File(card_path, filename="card.png")
                embed.set_image(url="attachment://card.png")
                await channel.send(embed=embed, file=channel_file, view=view)
            else:
                await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            logger.warning(f"Cannot post to channel {channel_id} (missing permissions)")
        except discord.HTTPException as e:
            logger.error(f"Error posting to channel {channel_id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error posting to notification channel: {e}")
    else:
        logger.warning(f"User {user_id} has no channel configured for '{kind}' alerts — alert dropped (run /config)")

    telegram_cfg = channels.get("telegram")
    if telegram_cfg and telegram_cfg.get("token") and telegram_cfg.get("chat_id"):
        caption = discord_embed_to_telegram_html(embed)
        await send_telegram_relay(telegram_cfg["token"], telegram_cfg["chat_id"], caption, card_path)

# ============================================================================
# TELEGRAM ALERT RELAY
#
# Lets a client mirror every alert this bot sends (whatever kinds this tier
# supports — ban/unban, or ban/unban/verify) to their own Telegram bot, via
# Telegram's plain HTTP API. No Telegram bot process needs to run for this —
# a valid bot token + chat_id is all sendMessage/sendPhoto needs.
#
# IMPORTANT: Telegram will not let ANY bot message a user who hasn't first
# sent that bot a message (e.g. pressed Start). test_telegram_relay() below
# catches this immediately at setup time instead of failing silently later.
# ============================================================================
def _md_to_telegram_html(text: str) -> str:
    """Converts the small subset of Discord markdown we use (**bold**,
    `code`) into Telegram HTML tags, escaping everything else first so
    raw text can't break Telegram's HTML parse mode."""
    if not text:
        return text
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped

def discord_embed_to_telegram_html(embed: discord.Embed) -> str:
    """Best-effort conversion of one of this bot's own embeds (built by
    build_event_embed) into Telegram HTML, including the Discord markdown
    (**bold**, `code`) those embeds use — not just a plain-text passthrough."""
    lines = []
    title = _md_to_telegram_html(embed.title or "")
    if embed.url:
        lines.append(f'<a href="{embed.url}"><b>{title}</b></a>')
    else:
        lines.append(f"<b>{title}</b>")
    if embed.description:
        lines.append("")
        lines.append(_md_to_telegram_html(embed.description))
    if embed.footer and embed.footer.text:
        lines.append("")
        lines.append(f"<i>{_md_to_telegram_html(embed.footer.text)}</i>")
    return "\n".join(lines)

async def send_telegram_relay(token: str, chat_id: str, caption_html: str, card_path: Optional[str] = None) -> None:
    base = f"https://api.telegram.org/bot{token}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if card_path:
                data = aiohttp.FormData()
                data.add_field("chat_id", str(chat_id))
                data.add_field("caption", caption_html)
                data.add_field("parse_mode", "HTML")
                with open(card_path, "rb") as f:
                    data.add_field("photo", f, filename="card.png", content_type="image/png")
                    async with session.post(f"{base}/sendPhoto", data=data) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.error(f"Telegram relay sendPhoto failed ({resp.status}): {body[:200]}")
            else:
                payload = {"chat_id": chat_id, "text": caption_html, "parse_mode": "HTML"}
                async with session.post(f"{base}/sendMessage", json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Telegram relay sendMessage failed ({resp.status}): {body[:200]}")
    except Exception as e:
        logger.error(f"Telegram relay error: {e}")

async def test_telegram_relay(token: str, chat_id: str):
    """Validates the token and confirms the bot can actually message this
    chat_id, before saving anything. Returns (ok: bool, error_message: str)."""
    base = f"https://api.telegram.org/bot{token}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/getMe") as resp:
                if resp.status != 200:
                    return False, "That doesn't look like a valid bot token."

            payload = {
                "chat_id": chat_id,
                "text": "✅ This Telegram chat is now linked to your Instagram Monitor alerts.",
                "parse_mode": "HTML",
            }
            async with session.post(f"{base}/sendMessage", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return False, (
                        "Couldn't send a message to that chat ID. Make sure you've sent "
                        "your bot a /start message first, and that the chat ID is correct. "
                        f"(Telegram said: {body[:150]})"
                    )
    except Exception as e:
        return False, str(e)
    return True, ""

async def set_user_telegram(user_id: int, token: Optional[str], chat_id: Optional[str]) -> None:
    async with _channel_lock:
        data = await _redis_get_json(CHANNEL_CONFIG_REDIS_KEY) if USE_REDIS else _load_channel_config_file()
        user_key = get_user_key(user_id)
        if user_key not in data:
            data[user_key] = {}
        if not token or not chat_id:
            data[user_key].pop("telegram", None)
        else:
            data[user_key]["telegram"] = {"token": token, "chat_id": chat_id}
        if USE_REDIS:
            await _redis_set_json(CHANNEL_CONFIG_REDIS_KEY, data)
        else:
            _save_channel_config_file(data)

class TelegramRelayModal(discord.ui.Modal):
    """Modal (private popup, not a visible chat message) so the bot token
    never appears as plaintext in a channel — unlike a slash command option,
    which Discord shows in the invocation itself."""
    def __init__(self):
        super().__init__(title="Telegram Alert Relay")
        self.token_input = discord.ui.TextInput(
            label="Telegram Bot Token (from @BotFather)",
            placeholder="123456789:AAExampleTokenGoesHere",
            max_length=100,
        )
        self.chat_id_input = discord.ui.TextInput(
            label="Your Telegram Chat/User ID",
            placeholder="e.g. 123456789 — get it from @userinfobot",
            max_length=32,
        )
        self.add_item(self.token_input)
        self.add_item(self.chat_id_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        token = self.token_input.value.strip()
        chat_id = self.chat_id_input.value.strip()
        ok, error = await test_telegram_relay(token, chat_id)
        if not ok:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        await set_user_telegram(interaction.user.id, token, chat_id)
        await interaction.followup.send(
            "✅ Telegram relay is active — every alert this bot sends you will now also go to that Telegram chat.",
            ephemeral=True,
        )

async def disable_telegram_relay(interaction: discord.Interaction):
    await set_user_telegram(interaction.user.id, None, None)
    await interaction.response.send_message("✅ Telegram relay disabled.", ephemeral=True)

# ============================================================================
# /config — per-user channel picker (shared by both bots; each bot decides
# which alert kinds it exposes by passing a different `kinds` list)
# ============================================================================
CHANNEL_KIND_LABELS = {
    "ban": "Ban Alerts",
    "unban": "Unban Alerts",
    "verify": "Verification Alerts",
}

class ChannelConfigView(discord.ui.View):
    def __init__(self, user_id: int, kinds: list):
        super().__init__(timeout=300)
        self.user_id = user_id
        for key in kinds:
            label = CHANNEL_KIND_LABELS[key]
            select = discord.ui.ChannelSelect(
                placeholder=f"Select channel for {label}",
                channel_types=[discord.ChannelType.text],
                min_values=1, max_values=1,
            )

            async def _cb(interaction: discord.Interaction, select=select, key=key, label=label):
                channel = select.values[0]
                resolved = channel.resolve() or await channel.fetch()
                perms = resolved.permissions_for(resolved.guild.me)
                if not (perms.send_messages and perms.embed_links):
                    await interaction.response.send_message(
                        f"I don't have Send Messages / Embed Links permission in {channel.mention}. "
                        f"Fix permissions on that channel and try again.",
                        ephemeral=True,
                    )
                    return
                await set_user_channel(self.user_id, key, resolved.id)
                await interaction.response.send_message(
                    f"✅ **{label}** will now be sent to {channel.mention} (in addition to your DMs).",
                    ephemeral=True,
                )

            select.callback = _cb
            self.add_item(select)

async def send_config_panel(interaction: discord.Interaction, kinds: list):
    current = await get_user_channels(interaction.user.id)
    lines = []
    for key in kinds:
        label = CHANNEL_KIND_LABELS[key]
        cid = current.get(key)
        lines.append(f"**{label}:** {f'<#{cid}>' if cid else '_not set (DM only)_'}")
    embed = discord.Embed(
        title="⚙️ Alert Channel Configuration",
        description="Pick a channel for each alert type below. DMs are always sent regardless of this setting.\n\n"
                     + "\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=ChannelConfigView(interaction.user.id, kinds), ephemeral=True)

# ============================================================================
# SHARED UI: MODALS AND CONFIRM VIEWS
# ============================================================================
class UsernameModal(discord.ui.Modal):
    def __init__(self, title: str, on_submit_callback):
        super().__init__(title=title)
        self.on_submit_callback = on_submit_callback
        self.username_input = discord.ui.TextInput(
            label="Instagram username", placeholder="e.g. nasa", max_length=30
        )
        self.add_item(self.username_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_submit_callback(interaction, self.username_input.value)

class BulkUsernameModal(discord.ui.Modal):
    def __init__(self, title: str, on_submit_callback):
        super().__init__(title=title)
        self.on_submit_callback = on_submit_callback
        self.usernames_input = discord.ui.TextInput(
            label="Instagram usernames",
            placeholder="One per line (or comma-separated), e.g.\nnasa\nspacex\nvercel",
            style=discord.TextStyle.paragraph,
            max_length=2000,
        )
        self.add_item(self.usernames_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_submit_callback(interaction, self.usernames_input.value)

class RemoveConfirmView(discord.ui.View):
    def __init__(self, username_key: str):
        super().__init__(timeout=300)
        self.username_key = username_key

    @discord.ui.button(label="Yes, remove", style=discord.ButtonStyle.danger)
    async def confirm_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        all_data = await load_data()
        user_key = get_user_key(user_id)
        if user_key in all_data and self.username_key in all_data[user_key]:
            removed = all_data[user_key][self.username_key]["username"]
            del all_data[user_key][self.username_key]
            await save_data(all_data)
            await interaction.response.edit_message(content=f"🗑️ Removed @{removed}.", embed=None, view=None)
        else:
            await interaction.response.edit_message(content="Already removed.", embed=None, view=None)

    @discord.ui.button(label="No, keep it", style=discord.ButtonStyle.secondary)
    async def cancel_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="👍 Kept this account.", embed=None, view=None)

class ClearAllConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Yes, clear everything", style=discord.ButtonStyle.danger)
    async def confirm_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        all_data = await load_data()
        user_key = get_user_key(user_id)
        if user_key in all_data:
            del all_data[user_key]
            await save_data(all_data)
        await interaction.response.edit_message(content="✅ Cleared all accounts.", view=None)

    @discord.ui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="👍 Kept all accounts.", view=None)

class WatchlistItemView(discord.ui.View):
    def __init__(self, username_key: str):
        super().__init__(timeout=None)
        self.username_key = username_key
        remove_button = discord.ui.Button(
            label="🗑️ Remove this account",
            style=discord.ButtonStyle.danger,
            custom_id=f"remove_{username_key}",
        )
        remove_button.callback = self.remove_callback
        self.add_item(remove_button)

    async def remove_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        all_data = await load_data()
        user_key = get_user_key(user_id)
        if user_key in all_data and self.username_key in all_data[user_key]:
            removed = all_data[user_key][self.username_key]["username"]
            del all_data[user_key][self.username_key]
            await save_data(all_data)
            await interaction.response.edit_message(content=f"🗑️ Removed @{removed}.", embed=None, view=None)
        else:
            await interaction.response.edit_message(content="Already removed.", embed=None, view=None)

# ============================================================================
# KEEP-ALIVE SERVER (for Render free tier) — unchanged
# ============================================================================
async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")

async def start_keepalive_server() -> None:
    app = web.Application()
    app.router.add_get("/", healthz)
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Keep-alive server listening on 0.0.0.0:{PORT} (/healthz)")
