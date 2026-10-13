import asyncio
import html
import logging
import os
import secrets
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
LASTFM_KEY = os.environ["LASTFM_API_KEY"]
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
DEEZER_URL = "https://api.deezer.com"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
http: httpx.AsyncClient
sem: asyncio.Semaphore


# ---------- models & in-memory state ----------


@dataclass
class Track:
    id: int
    title: str
    artist: str
    album: str
    cover: str
    duration: int
    rank: int
    link: str
    match: float | None = None


@dataclass
class Session:
    label: str
    tracks: list[Track]


sessions: OrderedDict[str, Session] = OrderedDict()
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
deezer_cache: dict[tuple, Track | None] = {}


def put(s: Session) -> str:
    sid = secrets.token_urlsafe(4)
    sessions[sid] = s
    while len(sessions) > 300:
        sessions.popitem(last=False)
    return sid


def get(sid: str) -> Session | None:
    s = sessions.get(sid)
    if s:
        sessions.move_to_end(sid)
    return s


esc = html.escape


# ---------- Deezer ----------


async def deezer(path: str, **params) -> dict:
    async with sem:
        r = await http.get(f"{DEEZER_URL}{path}", params=params)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "error" in data:
        return {}
    return data


def to_track(d: dict, match: float | None = None) -> Track | None:
    alb = d.get("album") or {}
    cover = alb.get("cover_xl") or alb.get("cover_big") or ""
    if not cover or "id" not in d:
        return None
    return Track(
        id=d["id"],
        title=d.get("title_short") or d["title"],
        artist=d["artist"]["name"],
        album=alb.get("title", ""),
        cover=cover,
        duration=d.get("duration", 0),
        rank=d.get("rank", 0),
        link=d.get("link", ""),
        match=match,
    )


async def search_deezer(query: str, limit: int = 5) -> list[Track]:
    data = await deezer("/search", q=query, limit=limit)
    out = [to_track(d) for d in data.get("data", [])]
    return [t for t in out if t]


async def find_on_deezer(title: str | None, artist: str, match: float | None):
    key = (title or "", artist.lower())
    if key in deezer_cache:
        t = deezer_cache[key]
        return Track(**{**t.__dict__, "match": match}) if t else None

    artist_q = artist.replace('"', "")
    queries = []
    if title:
        queries.append(f'artist:"{artist_q}" track:"{title.replace(chr(34), "")}"')
        queries.append(f"{artist_q} {title}")
    else:
        queries.append(f'artist:"{artist_q}"')

    found = None
    for q in queries:
        try:
            data = await deezer("/search", q=q, limit=1)
        except httpx.HTTPError:
            continue
        items = data.get("data", [])
        if items:
            found = to_track(items[0], match)
            if found:
                break
    deezer_cache[key] = found
    return found


# ---------- Last.fm ----------


async def lastfm(method: str, **params) -> dict:
    params.update(method=method, api_key=LASTFM_KEY, format="json")
    r = await http.get(LASTFM_URL, params=params)
    r.raise_for_status()
    return r.json()


async def similar_candidates(seed: Track) -> list[tuple[str | None, str, float]]:
    data = await lastfm(
        "track.getSimilar", track=seed.title, artist=seed.artist,
        limit=25, autocorrect=1,
    )
    items = data.get("similartracks", {}).get("track", [])
    if items:
        return [
            (t["name"], t["artist"]["name"], float(t.get("match") or 0))
            for t in items
        ]
    # fallback: similar artists, then their top Deezer track
    data = await lastfm(
        "artist.getSimilar", artist=seed.artist, limit=15, autocorrect=1
    )
    arts = data.get("similarartists", {}).get("artist", [])
    return [(None, a["name"], float(a.get("match") or 0)) for a in arts]


async def recommend(seed: Track) -> list[Track]:
    cands = await similar_candidates(seed)
    resolved = await asyncio.gather(
        *(find_on_deezer(t, a, m) for t, a, m in cands)
    )
    out, seen = [], {seed.id}
    for t in resolved:
        if t and t.id not in seen:
            seen.add(t.id)
            out.append(t)
        if len(out) == 10:
            break
    return out


# ---------- UI builders ----------


def caption(t: Track, i: int, n: int, label: str) -> str:
    m, s = divmod(t.duration, 60)
    lines = [f"🎵 <b>{esc(t.title)}</b>", f"👤 {esc(t.artist)}"]
    if t.album:
        lines.append(f"💿 {esc(t.album)}")
    lines.append(f"⏱ {m}:{s:02d}")
    if t.match is not None:
        lines.append(f"🎯 شباهت: {round(t.match * 100)}٪")
    if t.rank:
        stars = max(1, min(5, round(t.rank / 200_000)))
        lines.append("🔥 محبوبیت: " + "●" * stars + "○" * (5 - stars))
    lines.append(f"\n{i + 1} از {n} • شبیه <i>{esc(label)}</i>")
    return "\n".join(lines)


def card_kb(sid: str, i: int, n: int, t: Track) -> InlineKeyboardMarkup:
    yt = "https://www.youtube.com/results?search_query=" + quote_plus(
        f"{t.artist} {t.title}"
    )
    links = [Btn(text="▶️ YouTube", url=yt)]
    if t.link.startswith("http"):
        links.insert(0, Btn(text="🎼 Deezer", url=t.link))
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            Btn(text="⬅️", callback_data=f"n:{sid}:{(i - 1) % n}"),
            Btn(text=f"{i + 1}/{n}", callback_data="noop"),
            Btn(text="➡️", callback_data=f"n:{sid}:{(i + 1) % n}"),
        ],
        [
            Btn(text="🎧 پیش‌نمایش", callback_data=f"p:{sid}:{i}"),
            Btn(text="🔎 مشابه این", callback_data=f"s:{sid}:{i}"),
        ],
        links,
        [Btn(text="📋 لیست کامل", callback_data=f"l:{sid}")],
    ])


async def show_card(msg: Message, sid: str, i: int):
    s = get(sid)
    t = s.tracks[i]
    cap = caption(t, i, len(s.tracks), s.label)
    kb = card_kb(sid, i, len(s.tracks), t)
    if msg.photo:
        try:
            await msg.edit_media(
                InputMediaPhoto(media=t.cover, caption=cap, parse_mode="HTML"),
                reply_markup=kb,
            )
            return
        except TelegramBadRequest:
            pass
    await msg.delete()
    await msg.answer_photo(t.cover, caption=cap, reply_markup=kb)


async def run_recs(status: Message, seed: Track, user_id: int):
    """status: a text message we can edit/delete."""
    await status.edit_text("🎧 دارم شبیه‌هاشو پیدا میکنم...")
    try:
        recs = await recommend(seed)
    except httpx.HTTPError:
        await status.edit_text("یه مشکلی پیش اومد. یه دقیقه دیگه دوباره امتحان کن.")
        return
    if not recs:
        await status.edit_text("برای این اهنگ چیزی پیدا نکردم 😭")
        return
    history[user_id].appendleft(seed)
    label = f"{seed.title} - {seed.artist}"
    sid = put(Session(label=label, tracks=recs))
    await status.delete()
    t = recs[0]
    await status.answer_photo(
        t.cover,
        caption=caption(t, 0, len(recs), label),
        reply_markup=card_kb(sid, 0, len(recs), t),
    )


async def do_search(m: Message, query: str):
    status = await m.answer("🔍 دارم میگردم...")
    try:
        results = await search_deezer(query)
    except httpx.HTTPError:
        await status.edit_text("یه مشکلی پیش اومد. دوباره امتحان کن.")
        return
    if not results:
        await status.edit_text("چیزی پیدا نکردم. اسم رو یه جور دیگه بنویس.")
        return
    sid = put(Session(label=query, tracks=results))
    rows = [
        [Btn(text=f"{t.title} — {t.artist}"[:60], callback_data=f"c:{sid}:{i}")]
        for i, t in enumerate(results)
    ]
    await status.edit_text(
        "کدوم یکیه؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


# ---------- commands ----------

HELP = (
    "<b>راهنما</b>\n\n"
    "اسم یه اهنگ بفرست (بهتره خواننده رو هم بنویسی)، "
    "۱۰ تا اهنگ شبیهش رو با کاور و اطلاعات کامل میدم.\n\n"
    "/search - جستجوی اهنگ\n"
    "/history - اهنگایی که قبلا سرچ کردی\n"
    "/help - همین راهنما\n"
    "/about - درباره بات\n\n"
    "زیر هر کارت: ⬅️ ➡️ برای ورق زدن، 🎧 برای شنیدن ۳۰ ثانیه، "
    "🔎 برای پیدا کردن شبیه همون اهنگ."
)


@dp.message(CommandStart())
async def cmd_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="📖 راهنما", callback_data="help")]
    ])
    await m.answer(
        "سلام حاجی 🎧\nاسم یه اهنگ بفرست، ۱۰ تا شبیهش رو بهت میدم.",
        reply_markup=kb,
    )


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP)


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.answer()
    await c.message.answer(HELP)


@dp.message(Command("about"))
async def cmd_about(m: Message):
    await m.answer(
        "دیتای شباهت از Last.fm و کاور و اطلاعات اهنگ از Deezer میاد."
    )


@dp.message(Command("search"))
async def cmd_search(m: Message, command: CommandObject):
    if command.args:
        await do_search(m, command.args.strip())
    else:
        await m.answer("اسم اهنگ رو بفرست =)")


@dp.message(Command("history"))
async def cmd_history(m: Message):
    items = list(history[m.from_user.id])
    if not items:
        await m.answer("هنوز چیزی سرچ نکردی.")
        return
    sid = put(Session(label="history", tracks=items))
    rows = [
        [Btn(text=f"{t.title} — {t.artist}"[:60], callback_data=f"c:{sid}:{i}")]
        for i, t in enumerate(items)
    ]
    await m.answer(
        "آخرین سرچات:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    await do_search(m, m.text.strip())


# ---------- callbacks ----------

EXPIRED = "این لیست قدیمیه، دوباره سرچ کن."


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith("c:"))
async def cb_choose(c: CallbackQuery):
    _, sid, i = c.data.split(":")
    s = get(sid)
    if not s:
        await c.answer(EXPIRED, show_alert=True)
        return
    await c.answer()
    await run_recs(c.message, s.tracks[int(i)], c.from_user.id)


@dp.callback_query(F.data.startswith("n:"))
async def cb_nav(c: CallbackQuery):
    _, sid, i = c.data.split(":")
    if not get(sid):
        await c.answer(EXPIRED, show_alert=True)
        return
    await c.answer()
    await show_card(c.message, sid, int(i))


@dp.callback_query(F.data.startswith("s:"))
async def cb_similar(c: CallbackQuery):
    _, sid, i = c.data.split(":")
    s = get(sid)
    if not s:
        await c.answer(EXPIRED, show_alert=True)
        return
    await c.answer()
    status = await c.message.answer("🎧 ...")
    await run_recs(status, s.tracks[int(i)], c.from_user.id)


@dp.callback_query(F.data.startswith("l:"))
async def cb_list(c: CallbackQuery):
    sid = c.data.split(":")[1]
    s = get(sid)
    if not s:
        await c.answer(EXPIRED, show_alert=True)
        return
    await c.answer()
    lines = [f"📋 شبیه <b>{esc(s.label)}</b>:\n"]
    for i, t in enumerate(s.tracks, 1):
        pct = f" • {round(t.match * 100)}٪" if t.match is not None else ""
        lines.append(f"{i}. <b>{esc(t.title)}</b> - {esc(t.artist)}{pct}")
    rows, row = [], []
    for i in range(len(s.tracks)):
        row.append(Btn(text=str(i + 1), callback_data=f"n:{sid}:{i}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await c.message.delete()
    await c.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.callback_query(F.data.startswith("p:"))
async def cb_preview(c: CallbackQuery):
    _, sid, i = c.data.split(":")
    s = get(sid)
    if not s:
        await c.answer(EXPIRED, show_alert=True)
        return
    t = s.tracks[int(i)]
    try:
        fresh = await deezer(f"/track/{t.id}")  # preview links expire, refetch
    except httpx.HTTPError:
        await c.answer("مشکل پیش اومد.", show_alert=True)
        return
    url = fresh.get("preview")
    if not url:
        await c.answer("برای این اهنگ پیش‌نمایش نداره 😭", show_alert=True)
        return
    await c.answer("⏳")
    try:
        await c.message.answer_audio(
            url, title=t.title, performer=t.artist, duration=30
        )
    except TelegramBadRequest:
        await c.message.answer("نتونستم فایل رو بفرستم.")


# ---------- startup ----------


async def main():
    global http, sem
    http = httpx.AsyncClient(timeout=15)
    sem = asyncio.Semaphore(6)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="شروع"),
        BotCommand(command="search", description="جستجوی اهنگ"),
        BotCommand(command="history", description="سرچ‌های قبلی"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="about", description="درباره بات"),
    ])
    await bot.set_my_short_description("پیدا کردن ۱۰ اهنگ شبیه اهنگ مورد علاقه‌ت 🎧")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
