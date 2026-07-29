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
    "skull": os.getenv("EMOJI_SKULL", "❌"),
    "grave": os.getenv("EMOJI_GRAVE", "⚰️"),
    "warning": os.getenv("EMOJI_WARNING", "⚠️"),
}

BRAND_FOOTER = os.getenv("BOT_FOOTER_TEXT", "MosviQ Monitoring")

# ============================================================================
# EMBEDS
# ============================================================================
def build_event_embed(username: str, event: str, followers=None, time_taken_str=None,
                       is_verified=False) -> discord.Embed:
    """event: 'ban' | 'unban' | 'verify_on' | 'verify_off'"""
    titles = {
        "ban": f"{EMOJI['skull']} Account has been wiped — @{username}",
        "unban": f"{EMOJI['grave']} Account has returned from the grave — @{username}",
        "verify_on": f"{EMOJI['verified']} Account Verified — @{username}",
        "verify_off": f"{EMOJI['warning']} Verification Expired — @{username}",
    }
    colors = {
        "ban": discord.Color.red(),
        "unban": discord.Color.green(),
        "verify_on": discord.Color.gold(),
        "verify_off": discord.Color.orange(),
    }
    embed = discord.Embed(title=titles[event], url=core.instagram_url(username), color=colors[event])

    desc_parts = []
    if event in ("unban", "verify_on", "verify_off") and followers is not None:
        desc_parts.append(f"Followers: {followers:,}")
    if event == "unban":
        desc_parts.append(EMOJI["trophy"])
    if is_verified and event != "verify_off":
        desc_parts.append(EMOJI["verified"])
    if time_taken_str:
        desc_parts.append(f"{EMOJI['clock']} Time Taken: {time_taken_str}")
    if desc_parts:
        embed.description = " | ".join(desc_parts)

    embed.set_footer(text=BRAND_FOOTER)
    embed.timestamp = datetime.now(timezone.utc)
    return embed

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
    card_path = core.generate_stat_card(username, result.status, result.followers, result.following,
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
    card_path = core.generate_stat_card(username, result.status, result.followers, result.following,
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
    card_path = core.generate_stat_card(username, result.status, result.followers, result.following,
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

@tree.command(name="preview", description="Preview what an alert looks like (test only — never a real detection)")
@app_commands.describe(event="Which alert to preview", username="Example username to show",
                        time_taken="Example elapsed time, e.g. '3h 2m'", followers="Example follower count")
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

    is_verified = event.value in ("verify_on",)
    show_followers = followers if event.value != "ban" else None
    embed = build_event_embed(username, event.value, show_followers, time_taken, is_verified)
    embed.title = f"🧪 PREVIEW — {embed.title}"
    embed.color = discord.Color.purple()
    embed.add_field(name="⚠️ This is a preview", value="Sample formatting only — not a real detection. Only visible to you.", inline=False)

    card_status = "active" if event.value != "ban" else "suspended"
    card_path = core.generate_stat_card(username, card_status, followers, following=289, posts=118,
                                         profile_pic_bytes=None, is_verified=is_verified)
    file = discord.File(card_path, filename="card.png")
    embed.set_image(url="attachment://card.png")
    # Ephemeral (visible only to you) and never routed through notify_user,
    # so it can never land in a client's real alert channel or DMs.
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)

# ============================================================================
# POLLING LOOP
# ============================================================================
async def on_status_change(user_id, account_key, entry, old_status, new_status, result, time_taken_str):
    username = entry["username"]
    if old_status == "suspended" and new_status == "active":
        embed = build_event_embed(username, "unban", result.followers, time_taken_str, result.is_verified)
        profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
        card_path = core.generate_stat_card(username, "active", result.followers, result.following,
                                             result.posts, profile_pic_bytes, result.is_verified)
        view = core.RemoveConfirmView(account_key)
        await core.notify_user(client, user_id, "unban", embed, card_path, view)
        core.logger.info(f"User {user_id}: @{username} UNBANNED")
    elif old_status == "active" and new_status == "suspended":
        embed = build_event_embed(username, "ban", time_taken_str=time_taken_str)
        card_path = core.generate_stat_card(username, "suspended")
        view = core.RemoveConfirmView(account_key)
        await core.notify_user(client, user_id, "ban", embed, card_path, view)
        core.logger.info(f"User {user_id}: @{username} BANNED")

async def on_verification_change(user_id, account_key, entry, old_verified, new_verified, result, time_taken_str):
    username = entry["username"]
    event = "verify_on" if new_verified else "verify_off"
    embed = build_event_embed(username, event, result.followers, time_taken_str, new_verified)
    profile_pic_bytes = await core.fetch_profile_pic_bytes(result.profile_pic_url)
    card_path = core.generate_stat_card(username, "active", result.followers, result.following,
                                         result.posts, profile_pic_bytes, new_verified)
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
