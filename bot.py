#!/usr/bin/env python3
"""
MrSimplex — a Discord bot that streams audio from data.guythatlives.net.

You (the owner) decide which folders are browsable/playable by editing
config.json. Discord users then use slash commands to browse those folders
and stream tracks into a voice channel.

Run it:
    python bot.py                 # start the bot
    python bot.py --list-folders  # print your Simplex folders + their IDs
    python bot.py --check         # verify your token / API key work
    python bot.py --sync          # force-resync slash commands and exit

Configuration lives in two files (both editable by you):
    .env          -> secrets: DISCORD_TOKEN, SIMPLEX_API_KEY
    config.json   -> which folders are allowed, plus a few options

See README.md for full setup instructions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is optional; env vars still work without it.
    pass

from simplex import SimplexClient, SimplexFile


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

@dataclass
class AllowedFolder:
    name: str
    id: str


@dataclass
class Config:
    allowed_folders: list[AllowedFolder] = field(default_factory=list)
    auto_disconnect_seconds: int = 120
    max_queue: int = 100
    # If empty, slash commands sync globally (can take ~1 hour to appear).
    # Put your server's ID here for instant updates while developing.
    guild_id: Optional[int] = None

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            print(f"[config] {CONFIG_PATH.name} not found — using defaults. "
                  f"Copy config.example.json to config.json to customize.")
            return cls()
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        folders = [
            AllowedFolder(name=str(f["name"]), id=str(f["id"]))
            for f in data.get("allowed_folders", [])
            if f.get("id")
        ]
        gid = data.get("guild_id")
        return cls(
            allowed_folders=folders,
            auto_disconnect_seconds=int(data.get("auto_disconnect_seconds", 120)),
            max_queue=int(data.get("max_queue", 100)),
            guild_id=int(gid) if gid else None,
        )


def get_secret(name: str) -> str:
    val = os.environ.get(name, "").strip()
    return val


DISCORD_TOKEN = get_secret("DISCORD_TOKEN")
SIMPLEX_API_KEY = get_secret("SIMPLEX_API_KEY")


# --------------------------------------------------------------------------- #
# A tiny TTL cache so autocomplete doesn't hammer the API                     #
# --------------------------------------------------------------------------- #

class TTLCache:
    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._store: dict[str, tuple[float, list[SimplexFile]]] = {}

    def get(self, key: str) -> Optional[list[SimplexFile]]:
        hit = self._store.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.monotonic() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: list[SimplexFile]) -> None:
        self._store[key] = (time.monotonic(), value)


# --------------------------------------------------------------------------- #
# Per-guild music player                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Track:
    file_id: str
    title: str
    requested_by: str


# FFmpeg flags. The auth header is injected per-track in build_source().
FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"


class MusicPlayer:
    """Owns the queue + playback loop for a single guild."""

    def __init__(self, bot: "MrSimplex", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.next_event = asyncio.Event()
        self.current: Optional[Track] = None
        self.voice: Optional[discord.VoiceClient] = None
        self._task = bot.loop.create_task(self._run())
        self._queued_titles: list[str] = []  # for /queue display

    def _build_source(self, track: Track) -> discord.AudioSource:
        url = self.bot.simplex.raw_url(track.file_id)
        # Pass the bearer token to FFmpeg's http reader. CRLF terminates the
        # header line exactly as HTTP expects.
        header = f'Authorization: Bearer {self.bot.api_key}\r\n'
        before = f'{FFMPEG_BEFORE} -headers "{header}"'
        return discord.FFmpegPCMAudio(url, before_options=before, options=FFMPEG_OPTIONS)

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            self.next_event.clear()
            try:
                track = await asyncio.wait_for(
                    self.queue.get(),
                    timeout=self.bot.config.auto_disconnect_seconds,
                )
            except asyncio.TimeoutError:
                # Idle too long — leave the channel and tear down.
                await self.disconnect()
                self.bot.drop_player(self.guild.id)
                return

            if self._queued_titles:
                self._queued_titles.pop(0)
            self.current = track

            if not self.voice or not self.voice.is_connected():
                # Lost the voice connection; drop this track.
                self.current = None
                continue

            source = self._build_source(track)

            def _after(err: Optional[Exception]) -> None:
                if err:
                    print(f"[player] playback error: {err}", file=sys.stderr)
                self.bot.loop.call_soon_threadsafe(self.next_event.set)

            self.voice.play(source, after=_after)
            await self.next_event.wait()
            self.current = None

    def enqueue(self, track: Track) -> None:
        self.queue.put_nowait(track)
        self._queued_titles.append(track.title)

    def upcoming(self) -> list[str]:
        return list(self._queued_titles)

    def skip(self) -> bool:
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()  # triggers _after -> next track
            return True
        return False

    def pause(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            return True
        return False

    def clear(self) -> None:
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queued_titles.clear()

    async def disconnect(self) -> None:
        self.clear()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect(force=True)
        self.voice = None

    def stop_task(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


# --------------------------------------------------------------------------- #
# The bot                                                                      #
# --------------------------------------------------------------------------- #

class MrSimplex(commands.Bot):
    def __init__(self, config: Config, api_key: str):
        intents = discord.Intents.default()
        # Voice + guild info is all we need; no message content intent required.
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.api_key = api_key
        self.simplex = SimplexClient(api_key)
        self.players: dict[int, MusicPlayer] = {}
        self.cache = TTLCache(ttl=30.0)

    # ---- player lifecycle ---------------------------------------------- #

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = MusicPlayer(self, guild)
            self.players[guild.id] = player
        return player

    def drop_player(self, guild_id: int) -> None:
        player = self.players.pop(guild_id, None)
        if player:
            player.stop_task()

    # ---- helpers -------------------------------------------------------- #

    def folder_by_name(self, name: str) -> Optional[AllowedFolder]:
        for f in self.config.allowed_folders:
            if f.name.lower() == name.lower():
                return f
        # also allow passing a raw folder id
        for f in self.config.allowed_folders:
            if f.id == name:
                return f
        return None

    async def audio_in_folder(self, folder_id: str) -> list[SimplexFile]:
        cached = self.cache.get(folder_id)
        if cached is not None:
            return cached
        files = await self.simplex.list_files(parent=folder_id)
        audio = [f for f in files if f.is_audio]
        self.cache.set(folder_id, audio)
        return audio

    # ---- discord lifecycle --------------------------------------------- #

    async def setup_hook(self) -> None:
        register_commands(self)
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"[discord] slash commands synced to guild {self.config.guild_id}")
        else:
            await self.tree.sync()
            print("[discord] slash commands synced globally "
                  "(may take up to ~1 hour to appear)")

    async def on_ready(self) -> None:
        print(f"[discord] logged in as {self.user} (id {self.user.id})")
        print(f"[discord] serving {len(self.config.allowed_folders)} allowed folder(s)")

    async def close(self) -> None:
        for pid in list(self.players):
            self.drop_player(pid)
        await self.simplex.close()
        await super().close()


# --------------------------------------------------------------------------- #
# Slash commands                                                               #
# --------------------------------------------------------------------------- #

def register_commands(bot: MrSimplex) -> None:

    async def folder_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        return [
            app_commands.Choice(name=f.name, value=f.name)
            for f in bot.config.allowed_folders
            if current in f.name.lower()
        ][:25]

    async def track_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        folder_name = getattr(interaction.namespace, "folder", None)
        folder = bot.folder_by_name(folder_name) if folder_name else None
        if not folder:
            return []
        try:
            audio = await bot.audio_in_folder(folder.id)
        except Exception:
            return []
        current = current.lower()
        choices = []
        for f in audio:
            if current in f.name.lower():
                # value is the file id; name shown is the filename
                choices.append(app_commands.Choice(name=f.name[:100], value=f.id))
            if len(choices) >= 25:
                break
        return choices

    async def ensure_voice(interaction: discord.Interaction) -> Optional[MusicPlayer]:
        """Connect to the caller's voice channel; return the guild player."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This only works inside a server.", ephemeral=True)
            return None
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message(
                "Join a voice channel first, then try again.", ephemeral=True)
            return None

        player = bot.get_player(interaction.guild)
        channel = user.voice.channel
        if player.voice and player.voice.is_connected():
            if player.voice.channel.id != channel.id:
                await player.voice.move_to(channel)
        else:
            player.voice = await channel.connect()
        return player

    async def start_playback(
        interaction: discord.Interaction,
        files: list[SimplexFile],
        shuffle: bool = False,
        source_label: Optional[str] = None,
    ) -> None:
        """Join voice, enqueue one or more tracks, and reply with a summary.

        Shared by /play, /playfolder, and the /search picker. Responds to the
        interaction itself, so callers shouldn't also respond.
        """
        player = await ensure_voice(interaction)
        if player is None:
            return  # ensure_voice already sent an error response

        items = list(files)
        if shuffle:
            random.shuffle(items)

        was_idle = player.current is None and player.queue.qsize() == 0
        added = 0
        for f in items:
            if player.queue.qsize() >= bot.config.max_queue:
                break
            player.enqueue(Track(
                file_id=f.id,
                title=f.name,
                requested_by=interaction.user.display_name,
            ))
            added += 1

        if added == 0:
            await interaction.response.send_message(
                "Nothing to play — the queue may be full.", ephemeral=True)
            return

        if added == 1:
            only = items[0]
            if was_idle:
                msg = f"▶️ Now playing **{only.name}**"
            else:
                msg = f"➕ Queued **{only.name}** (position {player.queue.qsize()})"
        else:
            label = f" from **{source_label}**" if source_label else ""
            shuffled = " 🔀 shuffled" if shuffle else ""
            lead = "▶️ Playing" if was_idle else "➕ Queued"
            msg = f"{lead} **{added}** tracks{label}{shuffled}."
        await interaction.response.send_message(msg)

    # ---- /folders ------------------------------------------------------- #
    @bot.tree.command(description="List the folders you can browse and play from.")
    async def folders(interaction: discord.Interaction):
        if not bot.config.allowed_folders:
            await interaction.response.send_message(
                "No folders are configured yet. The bot owner sets these up in "
                "`config.json`.", ephemeral=True)
            return
        lines = [f"• **{f.name}**" for f in bot.config.allowed_folders]
        embed = discord.Embed(
            title="Available folders",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- /browse -------------------------------------------------------- #
    @bot.tree.command(description="View the audio tracks inside a folder.")
    @app_commands.describe(folder="Which folder to look inside")
    @app_commands.autocomplete(folder=folder_autocomplete)
    async def browse(interaction: discord.Interaction, folder: str):
        target = bot.folder_by_name(folder)
        if not target:
            await interaction.response.send_message(
                "That folder isn't available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            audio = await bot.audio_in_folder(target.id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't read that folder: `{e}`")
            return
        if not audio:
            await interaction.followup.send(f"**{target.name}** has no audio tracks.")
            return
        lines = [f"{i+1}. {f.name}" for i, f in enumerate(audio[:50])]
        more = "" if len(audio) <= 50 else f"\n…and {len(audio) - 50} more"
        embed = discord.Embed(
            title=f"{target.name} — {len(audio)} track(s)",
            description="\n".join(lines) + more,
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    # ---- /play ---------------------------------------------------------- #
    @bot.tree.command(description="Stream a track from a folder into your voice channel.")
    @app_commands.describe(folder="Folder to play from", track="Track to play")
    @app_commands.autocomplete(folder=folder_autocomplete, track=track_autocomplete)
    async def play(interaction: discord.Interaction, folder: str, track: str):
        target = bot.folder_by_name(folder)
        if not target:
            await interaction.response.send_message(
                "That folder isn't available.", ephemeral=True)
            return

        # Resolve the track. `track` is a file id from autocomplete, but a user
        # may type a name; fall back to matching by name within the folder.
        try:
            audio = await bot.audio_in_folder(target.id)
        except Exception as e:
            await interaction.response.send_message(
                f"Couldn't read that folder: `{e}`", ephemeral=True)
            return

        chosen = next((f for f in audio if f.id == track), None)
        if chosen is None:
            chosen = next(
                (f for f in audio if track.lower() in f.name.lower()), None)
        if chosen is None:
            await interaction.response.send_message(
                "Couldn't find that track in the folder.", ephemeral=True)
            return

        await start_playback(interaction, [chosen])

    # ---- /playfolder ---------------------------------------------------- #
    @bot.tree.command(
        description="Play a whole folder — pass shuffle:true to randomize the order.")
    @app_commands.describe(
        folder="Folder to play", shuffle="Shuffle the tracks before playing")
    @app_commands.autocomplete(folder=folder_autocomplete)
    async def playfolder(
        interaction: discord.Interaction, folder: str, shuffle: bool = False
    ):
        target = bot.folder_by_name(folder)
        if not target:
            await interaction.response.send_message(
                "That folder isn't available.", ephemeral=True)
            return
        try:
            audio = await bot.audio_in_folder(target.id)
        except Exception as e:
            await interaction.response.send_message(
                f"Couldn't read that folder: `{e}`", ephemeral=True)
            return
        if not audio:
            await interaction.response.send_message(
                f"**{target.name}** has no audio tracks.", ephemeral=True)
            return
        await start_playback(
            interaction, audio, shuffle=shuffle, source_label=target.name)

    # ---- /search (modal) ----------------------------------------------- #
    class TrackPlaySelect(discord.ui.Select):
        """Dropdown of matching tracks; picking one plays it."""

        def __init__(self, hits: list[tuple[AllowedFolder, SimplexFile]]):
            self.hits = {f.id: (fld, f) for fld, f in hits}
            options = [
                discord.SelectOption(
                    label=f.name[:100],
                    description=f"in {fld.name}"[:100],
                    value=f.id,
                )
                for fld, f in hits[:25]
            ]
            super().__init__(
                placeholder="Pick a track to play…",
                min_values=1, max_values=1, options=options,
            )

        async def callback(self, interaction: discord.Interaction):
            fld, f = self.hits[self.values[0]]
            await start_playback(interaction, [f], source_label=fld.name)

    class SearchResultView(discord.ui.View):
        def __init__(self, hits: list[tuple[AllowedFolder, SimplexFile]]):
            super().__init__(timeout=120)
            if hits:
                self.add_item(TrackPlaySelect(hits))

    class SearchModal(discord.ui.Modal, title="Search MrSimplex"):
        query = discord.ui.TextInput(
            label="Find a folder or track",
            placeholder="e.g. lofi, rain, intro…",
            required=True, max_length=100,
        )

        async def on_submit(self, interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            q = str(self.query.value).strip().lower()

            # Match allowed folder names...
            folder_matches = [
                fld for fld in bot.config.allowed_folders
                if q in fld.name.lower()
            ]
            # ...and audio tracks inside the allowed folders.
            track_hits: list[tuple[AllowedFolder, SimplexFile]] = []
            for fld in bot.config.allowed_folders:
                try:
                    audio = await bot.audio_in_folder(fld.id)
                except Exception:
                    continue
                for f in audio:
                    if q in f.name.lower():
                        track_hits.append((fld, f))

            if not folder_matches and not track_hits:
                await interaction.followup.send(
                    f"No allowed folders or tracks match **{q}**.", ephemeral=True)
                return

            lines: list[str] = []
            if folder_matches:
                lines.append("**Folders** (play with `/playfolder`):")
                lines += [f"• {fld.name}" for fld in folder_matches]
            if track_hits:
                lines.append(f"\n**Tracks** ({len(track_hits)} found):")
                lines += [
                    f"• {f.name}  —  *{fld.name}*"
                    for fld, f in track_hits[:25]
                ]
                if len(track_hits) > 25:
                    lines.append(f"…and {len(track_hits) - 25} more "
                                 "(narrow your search to see them).")

            embed = discord.Embed(
                title=f"Search results for “{q}”",
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
            )
            view = SearchResultView(track_hits)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @bot.tree.command(description="Search the allowed folders and tracks (opens a box).")
    async def search(interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal())

    # ---- /queue --------------------------------------------------------- #
    @bot.tree.command(description="Show what's playing and what's up next.")
    async def queue(interaction: discord.Interaction):
        player = bot.players.get(interaction.guild.id) if interaction.guild else None
        if not player or (player.current is None and not player.upcoming()):
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True)
            return
        lines = []
        if player.current:
            lines.append(f"**Now:** {player.current.title}")
        upcoming = player.upcoming()
        if upcoming:
            lines.append("**Up next:**")
            lines += [f"{i+1}. {t}" for i, t in enumerate(upcoming[:20])]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- transport controls -------------------------------------------- #
    @bot.tree.command(description="Skip the current track.")
    async def skip(interaction: discord.Interaction):
        player = bot.players.get(interaction.guild.id) if interaction.guild else None
        if player and player.skip():
            await interaction.response.send_message("⏭️ Skipped.")
        else:
            await interaction.response.send_message(
                "Nothing to skip.", ephemeral=True)

    @bot.tree.command(description="Pause playback.")
    async def pause(interaction: discord.Interaction):
        player = bot.players.get(interaction.guild.id) if interaction.guild else None
        if player and player.pause():
            await interaction.response.send_message("⏸️ Paused.")
        else:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True)

    @bot.tree.command(description="Resume playback.")
    async def resume(interaction: discord.Interaction):
        player = bot.players.get(interaction.guild.id) if interaction.guild else None
        if player and player.resume():
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message(
                "Nothing is paused.", ephemeral=True)

    @bot.tree.command(description="Stop, clear the queue, and leave the channel.")
    async def stop(interaction: discord.Interaction):
        player = bot.players.get(interaction.guild.id) if interaction.guild else None
        if not player:
            await interaction.response.send_message(
                "I'm not playing anything.", ephemeral=True)
            return
        await player.disconnect()
        bot.drop_player(interaction.guild.id)
        await interaction.response.send_message("⏹️ Stopped and left the channel.")

    @bot.tree.command(name="nowplaying", description="Show the current track.")
    async def nowplaying(interaction: discord.Interaction):
        player = bot.players.get(interaction.guild.id) if interaction.guild else None
        if player and player.current:
            await interaction.response.send_message(
                f"🎵 **{player.current.title}** "
                f"(requested by {player.current.requested_by})")
        else:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True)


# --------------------------------------------------------------------------- #
# CLI entry points                                                             #
# --------------------------------------------------------------------------- #

async def cli_list_folders() -> None:
    """Print the folder tree with IDs so you can fill in config.json."""
    if not SIMPLEX_API_KEY:
        print("SIMPLEX_API_KEY is not set (put it in .env).", file=sys.stderr)
        sys.exit(1)
    async with SimplexClient(SIMPLEX_API_KEY) as client:
        try:
            who = await client.me()
            label = who.get("email") or who.get("name") or who.get("id") or "ok"
            print(f"Connected to Simplex as: {label}\n")
        except Exception as e:
            print(f"Could not reach the API: {e}", file=sys.stderr)
            sys.exit(1)

        async def walk(parent: Optional[str], depth: int) -> None:
            files = await client.list_files(parent=parent)
            for f in sorted(files, key=lambda x: (not x.is_folder, x.name.lower())):
                indent = "  " * depth
                if f.is_folder:
                    print(f"{indent}📁 {f.name}    id={f.id}")
                    if depth < 4:
                        await walk(f.id, depth + 1)
                else:
                    tag = "🎵" if f.is_audio else "  "
                    print(f"{indent}{tag} {f.name}    id={f.id}")

        print("Your Simplex folders and files (copy a folder id into config.json):\n")
        await walk(None, 0)


async def cli_check() -> None:
    ok = True
    if not DISCORD_TOKEN:
        print("✗ DISCORD_TOKEN is missing (.env)")
        ok = False
    else:
        print("✓ DISCORD_TOKEN is set")
    if not SIMPLEX_API_KEY:
        print("✗ SIMPLEX_API_KEY is missing (.env)")
        ok = False
    else:
        async with SimplexClient(SIMPLEX_API_KEY) as client:
            try:
                await client.me()
                print("✓ SIMPLEX_API_KEY works (reached /me)")
            except Exception as e:
                print(f"✗ SIMPLEX_API_KEY failed: {e}")
                ok = False
    cfg = Config.load()
    print(f"• config.json: {len(cfg.allowed_folders)} allowed folder(s)")
    sys.exit(0 if ok else 1)


def run_bot(sync_only: bool = False) -> None:
    if not DISCORD_TOKEN:
        print("DISCORD_TOKEN is not set. Put it in .env (see .env.example).",
              file=sys.stderr)
        sys.exit(1)
    if not SIMPLEX_API_KEY:
        print("SIMPLEX_API_KEY is not set. Put it in .env (see .env.example).",
              file=sys.stderr)
        sys.exit(1)

    config = Config.load()
    bot = MrSimplex(config, SIMPLEX_API_KEY)

    if sync_only:
        async def _sync_and_quit():
            async with bot:
                await bot.login(DISCORD_TOKEN)
                await bot.setup_hook()
        asyncio.run(_sync_and_quit())
        print("Done syncing.")
        return

    bot.run(DISCORD_TOKEN)


def main() -> None:
    parser = argparse.ArgumentParser(description="MrSimplex Discord audio bot")
    parser.add_argument("--list-folders", action="store_true",
                        help="Print your Simplex folders and their IDs, then exit.")
    parser.add_argument("--check", action="store_true",
                        help="Verify your token and API key, then exit.")
    parser.add_argument("--sync", action="store_true",
                        help="Resync slash commands and exit.")
    args = parser.parse_args()

    if args.list_folders:
        asyncio.run(cli_list_folders())
    elif args.check:
        asyncio.run(cli_check())
    elif args.sync:
        run_bot(sync_only=True)
    else:
        run_bot()


if __name__ == "__main__":
    main()
