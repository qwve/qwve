#!/usr/bin/env python3
"""
================================================================================
 Instagram Status Monitor — ADVANCED tier (bot_advanced.py)
================================================================================
Ban/unban + verification-status monitoring, stat-card images, custom emoji
support, and a /config command letting each client route Ban / Unban /
Verification alerts to their own channels (DMs are always sent too).

Custom emojis: set the EMOJI_* env vars to your own server's emoji strings
(e.g. "<:verified:123456789012345678>"). Any not set fall back to a plain
unicode emoji, so the bot works with zero setup and looks better once you've
uploaded your own.

Run: python bot_advanced.py  (same env vars / deploy method as before — see
DEPLOYMENT_GUIDE.md)
================================================================================
"""
import os
import re
import random
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

import bot_core as core

ADVANCED_CHANNEL_KINDS = ["ban", "unban", "verify"]

# ============================================================================
# CUSTOM EMOJIS (optional — falls back to unicode if unset)
# ============================================================================
EMOJI = {
    "verified": os.getenv("EMOJI_VERIFIED", "✅"),
    "trophy": os.getenv("EMOJI_TROPHY", "🏆"),
    "clock": os.getenv("EMOJI_CLOCK", "⏰"),
    "skull": os.getenv("EMOJI_SKULL", "💀"),
    "grave": os.getenv("EMOJI_GRAVE", "⚰️"),
    "warning": os.getenv("EMOJI_WARNING", "⚠️"),
}

BRAND_FOOTER = core.BRAND_FOOTER

# ============================================================================
# EMBEDS
# ============================================================================
def build_event_embed(username: str, event: str, followers=None, time_taken_str=None,
                       is_verified=False) -> discord.Embed:
    """event: 'ban' | 'unban' | 'verify_on' | 'verify_off'
    Format matches the reference: "<emoji> Account <Status> | `@username`"
    title, with bold values in the description."""
    status_words = {
        "ban": "Account Banned",
        "unban": "Account Recovered",
        "verify_on": "Account Verified",
        "verify_off": "Verification Expired",
    }
    status_emojis = {
        "ban": "❌",
        "unban": "✅",
        "verify_on": "🔵",
        "verify_off": EMOJI["warning"],
    }
    colors = {
        "ban": discord.Color.red(),
        "unban": discord.Color.green(),
        "verify_on": discord.Color.blue(),
        "verify_off": discord.Color.orange(),
    }
    title = f"{status_emojis[event]} {status_words[event]} | `@{username}`"
    embed = discord.Embed(title=title, url=core.instagram_url(username), color=colors[event])

    desc_parts = []
    if event in ("unban", "verify_on", "verify_off") and followers is not None:
        desc_parts.append(f"👥 Followers: **{followers:,}**")
    if time_taken_str:
        desc_parts.append(f"{EMOJI['clock']} Time: **{time_taken_str}**")
    if desc_parts:
        embed.description = " | ".join(desc_parts)

    embed.set_footer(text=BRAND_FOOTER)
    embed.timestamp = datetime.now(timezone.utc)
    return embed

# Maps each alert event to its card's (status_label, ring_color) — shared by
# the real poll-cycle alerts and /fake, so both render identically.
EVENT_CARD_STYLE = {
    "ban": ("BANNED", core.RING_COLOR_RED),
    "unban": ("RECOVERED", core.RING_COLOR_GREEN),
    "verify_on": ("VERIFIED", core.RING_COLOR_BLUE),
    "verify_off": ("EXPIRED", core.RING_COLOR_YELLOW),
}

def build_added_embed(username: str, result: core.CheckResult) -> discord.Embed:
    if result.status == "active":
        embed = discord.Embed(title=f"📋 Added @{username} to WatchList", url=core.instagram_url(username),
                               color=discord.Color.green())
        desc = [f"Followers: {result.followers:,}" if result.followers is not None else "Followers: N/A"]
        if result.is_verified:
            desc.append(EMOJI["verified"])
        embed.description = " | ".join(desc)
    else:
        embed = discord.Embed(title=f"{EMOJI['warning']} @{username} not found", color=discord.Color.orange())
    embed.set_footer(text=BRAND_FOOTER)
    return embed

def build_check_embed(username: str, result: core.CheckResult) -> discord.Embed:
    if result.status == "active":
        embed = discord.Embed(title=f"@{username}", url=core.instagram_url(username), color=discord.Color.green())
        desc = [f"Followers: {result.followers:,}" if result.followers is not None else "Followers: N/A"]
        if result.is_verified:
            desc.append(EMOJI["verified"])
        embed.description = " | ".join(desc)
    else:
        embed = discord.Embed(title=f"{EMOJI['warning']} @{username} not found", color=discord.Color.orange())
    embed.set_footer(text=BRAND_FOOTER)
    return embed

def build_watchlist_embed(entry: dict) -> discord.Embed:
    status_line = "🔴 Suspended" if entry["status"] == "suspended" else "🟢 Active"
    color = discord.Color.red() if entry["status"] == "suspended" else discord.Color.green()
    embed = discord.Embed(title=f"@{entry['username']}", url=core.instagram_url(entry["username"]), color=color)
    embed.add_field(name="Status", value=status_line, inline=True)
    embed.add_field(name="Last Checked", value=core.relative_time(entry.get("last_checked", "")), inline=True)
    if entry.get("is_verified"):
        embed.add_field(name="Verified", value=EMOJI["verified"], inline=True)
    embed.add_field(name="Verification Monitoring", value="🔐 On" if entry.get("verify_watch") else "Off", inline=True)
    embed.set_footer(text=BRAND_FOOTER)
    return embed

# ============================================================================
# ACTIONS
# ============================================================================
async def perform_add(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    key = username.lower()
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        await interaction.followup.send("⚠️ Invalid Instagram username format.", ephemeral=True)
        return

    user_id = interaction.user.id
    all_data = await core.load_data()
    user_key = core.get_user_key(user_id)
    if user_key not in all_data:
        all_data[user_key] = {}
    if key in all_data[user_key]:
        await interaction.followup.send(f"ℹ️ @{all_data[user_key][key]['username']} is already monitored.", ephemeral=True)
        return

    result = await core.check_instagram_status(core.generate_case_variant(username, 0))
    now = datetime.now(timezone.utc).isoformat()
    all_data[user_key][key] = {
        "username": username, "status": result.status, "pending_status": None,
        "pending_count": 0, "last_checked": now, "added_at": now, "case_index": 1,
        "is_verified": result.is_verified, "verify_watch": False,
    }
    await core.save_data(all_data)

    embed = build_added_embed(username, result)
    profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
    card_path = await asyncio.to_thread(core.generate_stat_card, username, result.status, result.followers, result.following,
                                         result.posts, profile_pic_bytes, result.is_verified)
    file = discord.File(card_path, filename="card.png")
    embed.set_image(url="attachment://card.png")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)

async def perform_bulk_add(interaction: discord.Interaction, raw_text: str):
    user_id = interaction.user.id
    raw_tokens = re.split(r"[,\n]+", raw_text)
    tokens = []
    for chunk in raw_tokens:
        tokens.extend(chunk.split())

    seen, usernames = set(), []
    for tok in tokens:
        name = tok.lstrip("@").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        usernames.append(name)

    if not usernames:
        await interaction.followup.send("⚠️ No usernames found in that input.", ephemeral=True)
        return

    truncated = len(usernames) > core.MAX_BULK_ADD
    usernames = usernames[:core.MAX_BULK_ADD]

    all_data = await core.load_data()
    user_key = core.get_user_key(user_id)
    if user_key not in all_data:
        all_data[user_key] = {}

    added, skipped_existing, invalid, failed = [], [], [], []
    for i, username in enumerate(usernames):
        key = username.lower()
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
            invalid.append(username)
            continue
        if key in all_data[user_key]:
            skipped_existing.append(all_data[user_key][key]["username"])
            continue
        if i > 0:
            await asyncio.sleep(core.INTER_CHECK_DELAY_MS / 1000 + random.uniform(0, 1))
        try:
            result = await core.check_instagram_status(core.generate_case_variant(username, 0))
        except Exception:
            failed.append(username)
            continue
        now = datetime.now(timezone.utc).isoformat()
        all_data[user_key][key] = {
            "username": username, "status": result.status, "pending_status": None,
            "pending_count": 0, "last_checked": now, "added_at": now, "case_index": 1,
            "is_verified": result.is_verified, "verify_watch": False,
        }
        icon = "🟢" if result.status == "active" else "🔴"
        vbadge = f" {EMOJI['verified']}" if result.is_verified else ""
        added.append(f"{icon} @{username}{vbadge}")

    if added:
        await core.save_data(all_data)

    embed = discord.Embed(title="📥 Bulk Add Results", color=discord.Color.green() if added else discord.Color.orange())
    if added:
        embed.add_field(name=f"✅ Added ({len(added)})", value="\n".join(added), inline=False)
    if skipped_existing:
        embed.add_field(name=f"ℹ️ Already Monitored ({len(skipped_existing)})", value="\n".join(f"@{u}" for u in skipped_existing), inline=False)
    if invalid:
        embed.add_field(name=f"⚠️ Invalid Format ({len(invalid)})", value="\n".join(f"@{u}" for u in invalid), inline=False)
    if failed:
        embed.add_field(name=f"❌ Failed to Check ({len(failed)})", value="\n".join(f"@{u}" for u in failed), inline=False)
    if truncated:
        embed.set_footer(text=f"Only the first {core.MAX_BULK_ADD} usernames were processed.")
    else:
        embed.set_footer(text=BRAND_FOOTER)
    await interaction.followup.send(embed=embed, ephemeral=True)

async def perform_check(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    result = await core.check_instagram_status(username)
    embed = build_check_embed(username, result)
    profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
    card_path = await asyncio.to_thread(core.generate_stat_card, username, result.status, result.followers, result.following,
                                         result.posts, profile_pic_bytes, result.is_verified)
    file = discord.File(card_path, filename="card.png")
    embed.set_image(url="attachment://card.png")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)

async def perform_verify_add(interaction: discord.Interaction, username: str):
    """Turns ON verification monitoring for an account — separate from
    perform_add so a plain ban/unban add never silently starts tracking
    verification. Works whether the account is already monitored (just
    flips the flag) or brand new (adds it with the flag already on).
    Always re-checks first to seed the current verified state as the
    baseline, so turning this on never fires a false initial alert."""
    username = username.lstrip("@").strip()
    key = username.lower()
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        await interaction.followup.send("⚠️ Invalid Instagram username format.", ephemeral=True)
        return

    user_id = interaction.user.id
    all_data = await core.load_data()
    user_key = core.get_user_key(user_id)
    if user_key not in all_data:
        all_data[user_key] = {}

    existing = all_data[user_key].get(key)
    if existing and existing.get("verify_watch"):
        await interaction.followup.send(f"ℹ️ Verification monitoring is already ON for @{existing['username']}.", ephemeral=True)
        return

    result = await core.check_instagram_status(core.generate_case_variant(username, existing.get("case_index", 0) if existing else 0))

    if existing:
        existing["is_verified"] = result.is_verified
        existing["verify_watch"] = True
        all_data[user_key][key] = existing
        title = f"🔐 Verification monitoring enabled for @{username}"
    else:
        now = datetime.now(timezone.utc).isoformat()
        all_data[user_key][key] = {
            "username": username, "status": result.status, "pending_status": None,
            "pending_count": 0, "last_checked": now, "added_at": now, "case_index": 1,
            "is_verified": result.is_verified, "verify_watch": True,
        }
        title = f"🔐 Added @{username} with verification monitoring"

    await core.save_data(all_data)

    embed = discord.Embed(title=title, url=core.instagram_url(username), color=discord.Color.gold())
    embed.description = f"Currently {'verified ' + EMOJI['verified'] if result.is_verified else 'not verified'}."
    embed.set_footer(text=BRAND_FOOTER)
    profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
    card_path = await asyncio.to_thread(core.generate_stat_card, username, result.status, result.followers, result.following,
                                         result.posts, profile_pic_bytes, result.is_verified)
    file = discord.File(card_path, filename="card.png")
    embed.set_image(url="attachment://card.png")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)

async def show_watchlist(interaction: discord.Interaction):
    user_id = interaction.user.id
    all_data = await core.load_data()
    user_key = core.get_user_key(user_id)
    if user_key not in all_data or not all_data[user_key]:
        await interaction.followup.send("📭 You're not monitoring any accounts yet.", ephemeral=True)
        return
    await interaction.followup.send("📋 **WatchList**", ephemeral=True)
    for account_key, entry in sorted(all_data[user_key].items(), key=lambda kv: kv[1]["username"].lower()):
        await interaction.followup.send(embed=build_watchlist_embed(entry), view=core.WatchlistItemView(account_key), ephemeral=True)

# ============================================================================
# UI: MAIN PANEL
# ============================================================================
class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.primary, custom_id="adv_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_add(modal_interaction, username):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_add(modal_interaction, username)
        await interaction.response.send_modal(core.UsernameModal("Add account to monitor", handle_add))

    @discord.ui.button(label="📥 Bulk Add", style=discord.ButtonStyle.primary, custom_id="adv_bulk_add")
    async def bulk_add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_bulk_add(modal_interaction, raw_text):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_bulk_add(modal_interaction, raw_text)
        await interaction.response.send_modal(core.BulkUsernameModal("Bulk add accounts to monitor", handle_bulk_add))

    @discord.ui.button(label="🔐 Verify Add", style=discord.ButtonStyle.primary, custom_id="adv_verify_add")
    async def verify_add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_verify_add(modal_interaction, username):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_verify_add(modal_interaction, username)
        await interaction.response.send_modal(core.UsernameModal("Add / enable verification monitoring", handle_verify_add))

    @discord.ui.button(label="📋 WatchList", style=discord.ButtonStyle.secondary, custom_id="adv_watchlist")
    async def watchlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await show_watchlist(interaction)

    @discord.ui.button(label="🔍 Check", style=discord.ButtonStyle.secondary, custom_id="adv_check")
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_check(modal_interaction, username):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_check(modal_interaction, username)
        await interaction.response.send_modal(core.UsernameModal("Check account status", handle_check))

    @discord.ui.button(label="🗑️ Clear All", style=discord.ButtonStyle.danger, custom_id="adv_clear")
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        all_data = await core.load_data()
        user_key = core.get_user_key(interaction.user.id)
        if user_key not in all_data or not all_data[user_key]:
            await interaction.response.send_message("You have no accounts to clear.", ephemeral=True)
            return
        await interaction.response.send_message("⚠️ This will remove ALL accounts from monitoring. Sure?",
                                                  view=core.ClearAllConfirmView(), ephemeral=True)

# ============================================================================
# DISCORD CLIENT
# ============================================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@tree.command(name="panel", description="Open the Instagram monitor control panel")
async def panel_command(interaction: discord.Interaction):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    embed = discord.Embed(title="📱 Instagram Monitor — Advanced", description="Use the buttons below to manage your WatchList.",
                           color=discord.Color.blurple())
    embed.set_footer(text=BRAND_FOOTER)
    await interaction.response.send_message(embed=embed, view=MainPanelView())

@tree.command(name="config", description="Set which channels get your ban/unban/verification alerts")
async def config_command(interaction: discord.Interaction):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    await core.send_config_panel(interaction, ADVANCED_CHANNEL_KINDS)

@tree.command(name="tg", description="Mirror your ban/unban/verification alerts to a Telegram bot too")
async def telegram_relay_command(interaction: discord.Interaction):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    await interaction.response.send_modal(core.TelegramRelayModal())

@tree.command(name="telegram_relay_off", description="Stop mirroring alerts to Telegram")
async def telegram_relay_off_command(interaction: discord.Interaction):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    await core.disable_telegram_relay(interaction)

@tree.command(name="fake", description="Preview an alert using a real account's current stats (test only)")
@app_commands.describe(event="Which alert to preview", username="A real Instagram username to pull stats/avatar from",
                        time_taken="Example elapsed time, e.g. '3h 2m'", followers="Fallback follower count if the account can't be fetched")
@app_commands.choices(event=[
    app_commands.Choice(name="Ban alert", value="ban"),
    app_commands.Choice(name="Unban alert", value="unban"),
    app_commands.Choice(name="Verification gained", value="verify_on"),
    app_commands.Choice(name="Verification expired", value="verify_off"),
])
async def preview_command(interaction: discord.Interaction, event: app_commands.Choice[str],
                           username: str = "example_user", time_taken: str = "3h 2m", followers: int = 19614):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    username = username.lstrip("@").strip()
    status_label, ring_color = EVENT_CARD_STYLE[event.value]

    if event.value == "ban":
        # Nothing real to pull for a banned/removed account — same
        # synthetic "not found" card a real ban alert uses.
        embed = build_event_embed(username, "ban", time_taken_str=time_taken)
        card_path = await asyncio.to_thread(core.generate_stat_card, username, "suspended", status_label=status_label, ring_color=ring_color)
    else:
        # Recovery / verification-gained / verification-expired previews
        # pull the account's real current stats + avatar so the preview is
        # identical to what the actual alert would show.
        result = await core.check_instagram_status(username)
        profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
        real_followers = result.followers if result.followers is not None else followers
        # The verify badge reflects the event being previewed (that's the
        # whole point of "gained"/"expired"), not the account's live status.
        if event.value == "verify_on":
            is_verified = True
        elif event.value == "verify_off":
            is_verified = False
        else:
            is_verified = result.is_verified

        embed = build_event_embed(username, event.value, real_followers, time_taken, is_verified)
        card_path = await asyncio.to_thread(core.generate_stat_card, username, "active", real_followers, result.following, result.posts,
                                             profile_pic_bytes, is_verified,
                                             status_label=status_label, ring_color=ring_color)

    file = discord.File(card_path, filename="card.png")
    embed.set_image(url="attachment://card.png")
    # Ephemeral (visible only to you) and never routed through notify_user,
    # so it can never land in a client's real alert channel or DMs — it's
    # only ever posted here, plus mirrored to Telegram below if linked.
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    channels = await core.get_user_channels(interaction.user.id)
    telegram_cfg = channels.get("telegram")
    if telegram_cfg and telegram_cfg.get("token") and telegram_cfg.get("chat_id"):
        caption = core.discord_embed_to_telegram_html(embed)
        await core.send_telegram_relay(telegram_cfg["token"], telegram_cfg["chat_id"], caption, card_path)

@tree.command(name="help", description="See every command and what it does")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Instagram Monitor — Advanced | Help", color=discord.Color.blurple())
    embed.description = "Everything below is ephemeral — only you see it, no matter which channel you run it in."
    embed.add_field(
        name="/panel",
        value=(
            "Opens the control panel with buttons for everything below:\n"
            "**➕ Add** — add an account to your WatchList (ban/unban alerts only)\n"
            "**📥 Bulk Add** — add several usernames at once (one per line)\n"
            "**🔐 Verify Add** — add an account with verification-badge monitoring too\n"
            "**📋 WatchList** — list every account you're monitoring and its status\n"
            "**🔍 Check** — instantly check one account's current status\n"
            "**🗑️ Clear All** — remove every account from your WatchList (asks to confirm)"
        ),
        inline=False,
    )
    embed.add_field(
        name="/config",
        value="Choose which Discord channels get your ban / unban / verification alerts.",
        inline=False,
    )
    embed.add_field(
        name="/tg",
        value="Link a Telegram bot + chat so alerts get mirrored there too, in addition to Discord.",
        inline=False,
    )
    embed.add_field(
        name="/telegram_relay_off",
        value="Stop mirroring alerts to Telegram.",
        inline=False,
    )
    embed.add_field(
        name="/fake",
        value=(
            "Preview exactly what a real alert looks like (ban, unban, verification gained/expired), "
            "pulling a real account's current stats and avatar. Also mirrors to Telegram if you've "
            "linked one with /tg. Never posts to your real alert channels — test only."
        ),
        inline=False,
    )
    embed.set_footer(text=BRAND_FOOTER)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# POLLING LOOP
# ============================================================================
async def on_status_change(user_id, account_key, entry, old_status, new_status, result, time_taken_str):
    username = entry["username"]
    if old_status == "suspended" and new_status == "active":
        embed = build_event_embed(username, "unban", result.followers, time_taken_str, result.is_verified)
        profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
        status_label, ring_color = EVENT_CARD_STYLE["unban"]
        card_path = await asyncio.to_thread(core.generate_stat_card, username, "active", result.followers, result.following,
                                             result.posts, profile_pic_bytes, result.is_verified,
                                             status_label=status_label, ring_color=ring_color)
        view = core.RemoveConfirmView(account_key)
        await core.notify_user(client, user_id, "unban", embed, card_path, view)
        core.logger.info(f"User {user_id}: @{username} UNBANNED")
    elif old_status == "active" and new_status == "suspended":
        embed = build_event_embed(username, "ban", time_taken_str=time_taken_str)
        status_label, ring_color = EVENT_CARD_STYLE["ban"]
        card_path = await asyncio.to_thread(core.generate_stat_card, username, "suspended", status_label=status_label, ring_color=ring_color)
        view = core.RemoveConfirmView(account_key)
        await core.notify_user(client, user_id, "ban", embed, card_path, view)
        core.logger.info(f"User {user_id}: @{username} BANNED")

async def on_verification_change(user_id, account_key, entry, old_verified, new_verified, result, time_taken_str):
    username = entry["username"]
    event = "verify_on" if new_verified else "verify_off"
    embed = build_event_embed(username, event, result.followers, time_taken_str, new_verified)
    profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
    status_label, ring_color = EVENT_CARD_STYLE[event]
    card_path = await asyncio.to_thread(core.generate_stat_card, username, "active", result.followers, result.following,
                                         result.posts, profile_pic_bytes, new_verified,
                                         status_label=status_label, ring_color=ring_color)
    await core.notify_user(client, user_id, "verify", embed, card_path)
    core.logger.info(f"User {user_id}: @{username} verification -> {new_verified}")

@tasks.loop(seconds=core.POLL_INTERVAL_SECONDS)
async def polling_loop():
    try:
        await core.run_poll_cycle(on_status_change, on_verification_change, track_verification=True)
    except Exception as e:
        core.logger.exception(f"Polling error: {e}")

@polling_loop.before_loop
async def before_polling_loop():
    await client.wait_until_ready()

# ============================================================================
# LIFECYCLE
# ============================================================================
_bot_setup_complete = False

@client.event
async def on_ready():
    global _bot_setup_complete
    core.logger.info(f"Logged in as {client.user} (on_ready fired)")
    if _bot_setup_complete:
        core.logger.info("Setup already completed previously; skipping re-setup.")
        return
    client.add_view(MainPanelView())
    await core.start_keepalive_server()
    await tree.sync()
    if not polling_loop.is_running():
        polling_loop.start()
    _bot_setup_complete = True
    core.logger.info("Advanced bot startup complete.")

def main() -> None:
    core.logger.info("Starting Discord bot (ADVANCED tier)...")
    client.run(core.DISCORD_BOT_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
