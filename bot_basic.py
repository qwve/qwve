#!/usr/bin/env python3
"""
================================================================================
 Instagram Status Monitor — BASIC tier (bot_basic.py)
================================================================================
Ban/unban monitoring only. No verification tracking, no image cards — text
embeds with a colored side bar only (red for ban, green for unban). Bulk add
is supported. /config lets each client pick their own Ban Alerts / Unban
Alerts channels (DMs are always sent too).

Run: python bot_basic.py   (same env vars / deploy method as before — see
DEPLOYMENT_GUIDE.md)
================================================================================
"""
import re
import random
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

import bot_core as core

BASIC_CHANNEL_KINDS = ["ban", "unban"]

# ============================================================================
# EMBEDS (text-only, no images)
# ============================================================================
def build_watchlist_embed(entry: dict) -> discord.Embed:
    status_line = "🔴 Suspended" if entry["status"] == "suspended" else "🟢 Active"
    color = discord.Color.red() if entry["status"] == "suspended" else discord.Color.green()
    embed = discord.Embed(title=f"@{entry['username']}", url=core.instagram_url(entry["username"]), color=color)
    embed.add_field(name="Status", value=status_line, inline=True)
    embed.add_field(name="Last Checked", value=core.relative_time(entry.get("last_checked", "")), inline=True)
    embed.set_footer(text="Instagram Monitor — Basic")
    return embed

def build_event_embed(username: str, event: str, followers=None, time_taken_str=None) -> discord.Embed:
    """event: 'ban' or 'unban'. Colored side bar only — red/green — no image."""
    if event == "ban":
        embed = discord.Embed(title=f"🔴 Account Suspended — @{username}", url=core.instagram_url(username),
                               color=discord.Color.red())
    else:
        embed = discord.Embed(title=f"🟢 Account Recovered — @{username}", url=core.instagram_url(username),
                               color=discord.Color.green())
        if followers is not None:
            embed.add_field(name="Followers", value=f"{followers:,}", inline=True)
    if time_taken_str:
        embed.add_field(name="⏰ Time Taken", value=time_taken_str, inline=True)
    embed.set_footer(text="Instagram Monitor — Basic")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

def build_not_found_embed(username: str) -> discord.Embed:
    embed = discord.Embed(title=f"⚠️ @{username} not found", color=discord.Color.orange())
    embed.description = "0 posts · 0 followers · 0 following"
    embed.set_footer(text="Instagram Monitor — Basic")
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
    }
    await core.save_data(all_data)

    if result.status == "active":
        embed = discord.Embed(title=f"📋 Added @{username} to WatchList", url=core.instagram_url(username),
                               color=discord.Color.green())
        embed.description = f"Followers: {result.followers:,}" if result.followers is not None else "Followers: N/A"
    else:
        embed = build_not_found_embed(username)
    embed.set_footer(text="Instagram Monitor — Basic")
    await interaction.followup.send(embed=embed, ephemeral=True)

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
        }
        added.append(f"{'🟢' if result.status == 'active' else '🔴'} @{username}")

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
    await interaction.followup.send(embed=embed, ephemeral=True)

async def perform_check(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    result = await core.check_instagram_status(username)
    if result.status == "active":
        embed = discord.Embed(title=f"@{username}", url=core.instagram_url(username), color=discord.Color.green())
        embed.description = f"Followers: {result.followers:,}" if result.followers is not None else "Followers: N/A"
    else:
        embed = build_not_found_embed(username)
    await interaction.followup.send(embed=embed, ephemeral=True)

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

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.primary, custom_id="basic_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_add(modal_interaction, username):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_add(modal_interaction, username)
        await interaction.response.send_modal(core.UsernameModal("Add account to monitor", handle_add))

    @discord.ui.button(label="📥 Bulk Add", style=discord.ButtonStyle.primary, custom_id="basic_bulk_add")
    async def bulk_add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_bulk_add(modal_interaction, raw_text):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_bulk_add(modal_interaction, raw_text)
        await interaction.response.send_modal(core.BulkUsernameModal("Bulk add accounts to monitor", handle_bulk_add))

    @discord.ui.button(label="📋 WatchList", style=discord.ButtonStyle.secondary, custom_id="basic_watchlist")
    async def watchlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await show_watchlist(interaction)

    @discord.ui.button(label="🔍 Check", style=discord.ButtonStyle.secondary, custom_id="basic_check")
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not core.is_authorized(interaction.user.id):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return
        async def handle_check(modal_interaction, username):
            await modal_interaction.response.defer(ephemeral=True, thinking=True)
            await perform_check(modal_interaction, username)
        await interaction.response.send_modal(core.UsernameModal("Check account status", handle_check))

    @discord.ui.button(label="🗑️ Clear All", style=discord.ButtonStyle.danger, custom_id="basic_clear")
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
    embed = discord.Embed(title="📱 Instagram Monitor — Basic", description="Use the buttons below to manage your WatchList.",
                           color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=MainPanelView())

@tree.command(name="config", description="Set which channels get your ban/unban alerts")
async def config_command(interaction: discord.Interaction):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    await core.send_config_panel(interaction, BASIC_CHANNEL_KINDS)

@tree.command(name="preview", description="Preview what a ban/unban alert looks like (test only — never a real detection)")
@app_commands.describe(event="Which alert to preview", username="Example username to show", time_taken="Example elapsed time, e.g. '3h 2m'")
@app_commands.choices(event=[
    app_commands.Choice(name="Ban alert", value="ban"),
    app_commands.Choice(name="Unban alert", value="unban"),
])
async def preview_command(interaction: discord.Interaction, event: app_commands.Choice[str],
                           username: str = "example_user", time_taken: str = "3h 2m"):
    if not core.is_authorized(interaction.user.id):
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    followers = 19614 if event.value == "unban" else None
    embed = build_event_embed(username, event.value, followers, time_taken)
    embed.title = f"🧪 PREVIEW — {embed.title}"
    embed.color = discord.Color.purple()
    embed.add_field(name="⚠️ This is a preview", value="Sample formatting only — not a real ban/unban detection. Only visible to you.", inline=False)
    # Ephemeral (visible only to you) and never routed through notify_user,
    # so it can never land in a client's real alert channel or DMs.
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# POLLING LOOP
# ============================================================================
async def on_status_change(user_id, account_key, entry, old_status, new_status, result, time_taken_str):
    username = entry["username"]
    if old_status == "suspended" and new_status == "active":
        embed = build_event_embed(username, "unban", result.followers, time_taken_str)
        view = core.RemoveConfirmView(account_key)
        await core.notify_user(client, user_id, "unban", embed, view=view)
        core.logger.info(f"User {user_id}: @{username} UNBANNED")
    elif old_status == "active" and new_status == "suspended":
        embed = build_event_embed(username, "ban", time_taken_str=time_taken_str)
        view = core.RemoveConfirmView(account_key)
        await core.notify_user(client, user_id, "ban", embed, view=view)
        core.logger.info(f"User {user_id}: @{username} BANNED")

@tasks.loop(seconds=core.POLL_INTERVAL_SECONDS)
async def polling_loop():
    try:
        await core.run_poll_cycle(on_status_change, track_verification=False)
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
    core.logger.info("Basic bot startup complete.")

def main() -> None:
    core.logger.info("Starting Discord bot (BASIC tier)...")
    client.run(core.DISCORD_BOT_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
