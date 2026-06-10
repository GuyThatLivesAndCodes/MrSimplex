# MrSimplex

A Discord bot that streams audio straight from your
[data.guythatlives.net](https://data.guythatlives.net) (Simplex) storage.

**You** pick which folders are browsable and playable (in `config.json`).
**Your users** browse those folders and stream tracks into a voice channel
with slash commands. Nothing else in your storage is exposed.

---

## What users can do (slash commands)

| Command            | What it does                                                |
|--------------------|-------------------------------------------------------------|
| `/folders`         | List the folders you've made available                      |
| `/browse <folder>` | View the audio tracks inside a folder                       |
| `/play <folder> <track>` | Join your voice channel and stream a track (queues if busy) |
| `/playfolder <folder> [shuffle]` | Queue a whole folder; pass `shuffle: true` to randomize |
| `/search`          | Open a search box to find allowed folders/tracks, then play one |
| `/queue`           | Show what's playing and what's next                         |
| `/nowplaying`      | Show the current track                                      |
| `/skip`            | Skip the current track                                      |
| `/pause` `/resume` | Pause / resume                                              |
| `/stop`            | Stop, clear the queue, and leave the channel                |

`folder` and `track` have **autocomplete**, so users just type and pick.

---

## Setup (one time)

### 1. Install prerequisites

- **Python 3.9+** — <https://python.org>
- **FFmpeg** — required for audio streaming. Make sure `ffmpeg` is on your PATH.
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg` (or download from ffmpeg.org)

### 2. Create your Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Open the **Bot** tab → **Reset Token** → copy the token.
3. No privileged intents are required (this bot doesn't read message content).
4. Go to **OAuth2 → URL Generator**, tick **`bot`** and
   **`applications.commands`**, then under "Bot Permissions" tick
   **Connect** and **Speak**. Open the generated URL to invite the bot to
   your server.

### 3. Add your secrets

Copy the example env file and fill it in:

```bash
cp .env.example .env
```

Edit `.env`:

```
DISCORD_TOKEN=your-discord-bot-token
SIMPLEX_API_KEY=your-data.guythatlives.net-api-key
```

### 4. Choose which folders to expose

First, list your Simplex folders to get their IDs:

```bash
python bot.py --list-folders
```

(or `./run.sh --list-folders` — see below). You'll see something like:

```
📁 Lofi Beats    id=fld_abc123
  🎵 rainy night.mp3    id=...
📁 Sound Effects    id=fld_def456
```

Now copy the example config and add the folders you want available:

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "allowed_folders": [
    { "name": "Lofi Beats", "id": "fld_abc123" },
    { "name": "Sound Effects", "id": "fld_def456" }
  ],
  "guild_id": 123456789012345678,
  "auto_disconnect_seconds": 120,
  "max_queue": 100
}
```

- `name` is just the label users see — call it whatever you like.
- `id` is the folder ID from `--list-folders`.
- `guild_id` (optional) — set it to **your Discord server's ID** so slash
  commands update **instantly**. Leave it `null` for global commands, which
  can take up to ~1 hour to appear. (Enable Developer Mode in Discord, then
  right-click your server → Copy Server ID.)

---

## Running

The easy way (creates a virtualenv and installs deps automatically on first run):

```bash
./run.sh           # Linux / macOS
run.bat            # Windows
```

Or do it manually:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

When it's running you'll see `logged in as ...` and your slash commands
will be available in Discord.

### Handy commands

```bash
python bot.py                 # run the bot
python bot.py --list-folders  # print your Simplex folders + IDs (for setup)
python bot.py --check         # verify your token and API key work
python bot.py --sync          # force a slash-command resync, then exit
```

(All of these also work via `./run.sh ...` / `run.bat ...`.)

---

## Editing / extending the bot

The code is split into two readable files:

- **`bot.py`** — Discord bot, slash commands, and the per-server music
  player/queue. Add or change commands inside `register_commands()`.
- **`simplex.py`** — the small async client for the data.guythatlives.net
  API. If the API returns fields under different names, the normalizer here
  is where you'd adjust.

A few things you might want to tweak:

- **Add a command** — copy one of the `@bot.tree.command(...)` blocks in
  `register_commands()`.
- **Change idle timeout** — `auto_disconnect_seconds` in `config.json`.
- **What counts as "audio"** — `SimplexFile.is_audio` in `simplex.py`.
- **Restrict who can use commands** — add a check at the top of a command,
  e.g. `interaction.user.guild_permissions.manage_guild`.

---

## Troubleshooting

- **Slash commands don't appear** — set `guild_id` in `config.json` to your
  server ID for instant sync, then restart. Global commands are slow to
  propagate.
- **"ffmpeg was not found"** — install FFmpeg and ensure it's on your PATH
  (`ffmpeg -version` should work in a terminal).
- **Bot joins but plays silence** — confirm `SIMPLEX_API_KEY` is valid
  (`python bot.py --check`) and that the file is real audio.
- **`PyNaCl` / voice errors** — `pip install -r requirements.txt` again;
  the `discord.py[voice]` extra is required for voice.
- **`RuntimeError: davey library needed in order to use voice`** — you have
  discord.py 2.6+, which needs the new voice backend. Reinstall with the
  voice extra: `pip install -U "discord.py[voice]"` (or just rerun
  `pip install -r requirements.txt`), then restart the bot.
- **Folder is empty in `/browse`** — make sure the folder ID is correct and
  actually contains audio files (`python bot.py --list-folders`).
