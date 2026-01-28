import os
import time
import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEEPL_AUTH_KEY = os.getenv("DEEPL_AUTH_KEY")

TRIGGER_EMOJI = os.getenv("TRIGGER_EMOJI", "🌐")

DEFAULT_TARGET_EN = os.getenv("DEFAULT_TARGET_EN", "EN-GB")  # translate to English
DEFAULT_TARGET_ZH = os.getenv("DEFAULT_TARGET_ZH", "ZH")     # translate to Chinese

AUTO_CHANNEL_IDS_RAW = os.getenv("AUTO_CHANNEL_IDS", "").strip()
AUTO_CHANNEL_IDS = set()
if AUTO_CHANNEL_IDS_RAW:
    for x in AUTO_CHANNEL_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            AUTO_CHANNEL_IDS.add(int(x))

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "8"))

# ---- discord intents ----
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- simple per-user cooldown ----
_last_used = {}  # user_id -> timestamp

def cooldown_ok(user_id: int) -> bool:
    now = time.time()
    last = _last_used.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_used[user_id] = now
    return True

def deepl_endpoint() -> str:
    # Free plan: api-free.deepl.com
    # Pro plan: api.deepl.com
    # Most users on free: keep api-free
    return "https://api-free.deepl.com/v2/translate"

def deepl_translate(text: str, target_lang: str):
    """
    Returns (translated_text, detected_source_language)
    Uses new header-based auth.
    """
    url = deepl_endpoint()
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_AUTH_KEY}"}
    data = {"text": text, "target_lang": target_lang}

    r = requests.post(url, headers=headers, data=data, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"DeepL {r.status_code}: {r.text}")

    t = r.json()["translations"][0]
    return t["text"], t.get("detected_source_language")

def pick_two_way_target(detected_src: str) -> str:
    """
    Two-way mode:
    - If source is English -> translate to Chinese
    - Else -> translate to English
    """
    if (detected_src or "").upper().startswith("EN"):
        return DEFAULT_TARGET_ZH
    return DEFAULT_TARGET_EN

def make_embed(src_lang: str, tgt_lang: str, translated: str, requester: discord.abc.User | None = None):
    title = f"Translation ({src_lang} → {tgt_lang})" if src_lang else f"Translation (→ {tgt_lang})"
    embed = discord.Embed(title=title, description=translated)
    if requester:
        embed.set_footer(text=f"Requested by {requester.display_name}")
    return embed

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Slash commands synced: {len(synced)}")
    except Exception as e:
        print("⚠️ Slash sync failed:", repr(e))

# --------------------------
#  A) Reaction-based translate
# --------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        print("RAW:", str(payload.emoji), "name:", getattr(payload.emoji, "name", None))

        # ignore bot users
        if payload.member and payload.member.bot:
            return

        # accept both the unicode emoji and its name
        emoji_ok = (str(payload.emoji) == TRIGGER_EMOJI) or (getattr(payload.emoji, "name", "") == "globe_with_meridians")
        if not emoji_ok:
            return

        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)

        if msg.author.bot:
            return

        content = (msg.content or "").strip()
        if not content:
            await msg.reply("No text to translate (only stickers/attachments).")
            return

        # remove/disable cooldown while testing (optional)
        # if not cooldown_ok(payload.user_id): return

        translated, src = deepl_translate(content, DEFAULT_TARGET_EN)
        embed = make_embed(src or "?", DEFAULT_TARGET_EN, translated, requester=payload.member)
        await msg.reply(embed=embed)

    except Exception as e:
        print("REACTION ERROR:", repr(e))
        # try to report in-channel too
        try:
            channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
            await channel.send(f"Reaction translate failed: `{repr(e)}`")
        except:
            pass

# --------------------------
#  B) Auto-translate channels
# --------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Only auto-translate in configured channels
    if message.channel.id in AUTO_CHANNEL_IDS:
        content = (message.content or "").strip()
        if content and len(content) <= 1500:  # keep auto mode lighter
            try:
                en_text, src = deepl_translate(content, DEFAULT_TARGET_EN)

                # If already English, skip (prevents spam)
                if (src or "").upper().startswith("EN"):
                    pass
                else:
                    embed = make_embed(src or "?", DEFAULT_TARGET_EN, en_text)
                    await message.reply(embed=embed)

            except Exception as e:
                print("Auto-translate error:", repr(e))

    await bot.process_commands(message)

# --------------------------
#  C) Slash command /translate
# --------------------------
@bot.tree.command(name="translate", description="Translate text with DeepL (auto-detect source).")
async def translate_cmd(interaction: discord.Interaction, text: str, target: str = ""):
    """
    /translate text:"..." target:"EN-GB"  (target optional)
    If target is omitted -> two-way mode (EN<->ZH)
    """
    await interaction.response.defer(thinking=True)

    try:
        if target:
            tgt = target.strip().upper()
            translated, src = deepl_translate(text, tgt)
            embed = make_embed(src or "?", tgt, translated, requester=interaction.user)
            await interaction.followup.send(embed=embed)
            return

        # two-way default:
        en_text, src = deepl_translate(text, DEFAULT_TARGET_EN)
        tgt = pick_two_way_target(src)
        if tgt == DEFAULT_TARGET_EN:
            translated = en_text
        else:
            translated, _ = deepl_translate(text, tgt)

        embed = make_embed(src or "?", tgt, translated, requester=interaction.user)
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print("Slash translate error:", repr(e))
        await interaction.followup.send(f"Translation failed: `{repr(e)}`")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing. Check your .env file.")
if not DEEPL_AUTH_KEY:
    raise RuntimeError("DEEPL_AUTH_KEY missing. Check your .env file.")

bot.run(DISCORD_TOKEN)