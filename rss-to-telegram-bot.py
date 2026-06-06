import asyncio
import hashlib
import json
import logging
import os
import re
import traceback
from pathlib import Path
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot, InputMediaPhoto, MessageOriginChannel
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

RSS_URL, BOT_TOKEN, CHANNEL_ID, GROUP_ID, ADMIN_ID, SEEN_ITEMS_FILE = (os.environ["RSS_URL"], os.environ["TG_BOT_TOKEN"], os.environ["TG_CHANNEL_ID"], os.getenv("TG_GROUP_ID", ""), os.getenv("TG_ADMIN_ID", ""), "seen_items.json" ,)
MAX_CAPTION_LENGTH  = 1024
MAX_MESSAGE_LENGTH  = 4096
MAX_IMAGES_PER_POST = 10

_SUFFIX_VISIBLE = "\nবাকি অংশ কমেন্টে..."
_SUFFIX_HTML    = "\n<b>বাকি অংশ কমেন্টে...</b>"

# ─────────────────────────────────────────────
#  LOGGING  (console only — errors go to DM)
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

_errors: list[str] = []   # collected during the run; sent as DM at the end


def record_error(msg: str) -> None:
    """Log + collect an error message to be DM'd at the end."""
    log.error(msg)
    _errors.append(msg)


# ─────────────────────────────────────────────
#  SEEN-ITEMS PERSISTENCE
# ─────────────────────────────────────────────

def load_seen_ids() -> set:
    if Path(SEEN_ITEMS_FILE).exists():
        try:
            with open(SEEN_ITEMS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            record_error(f"Could not load seen_items.json: {e}")
    return set()


def save_seen_ids(seen: set) -> None:
    try:
        with open(SEEN_ITEMS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
    except Exception as e:
        record_error(f"Could not save seen_items.json: {e}")


def item_id(entry) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(raw.encode()).hexdigest()


# ─────────────────────────────────────────────
#  RSS PARSING
# ─────────────────────────────────────────────

def fetch_feed(url: str) -> list:
    log.info("Fetching feed: %s", url)
    feed = feedparser.parse(url)
    if feed.bozo:
        log.warning("Feed may be malformed: %s", feed.bozo_exception)
    entries = feed.entries or []
    log.info("Found %d entries in feed", len(entries))
    return entries


def _is_image_url(url: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png|gif|webp)(\?.*)?$", url, re.IGNORECASE))


def extract_images(entry) -> list:
    urls = []
    for m in entry.get("media_content", []):
        u = m.get("url", "")
        if u and _is_image_url(u):
            urls.append(u)
    for m in entry.get("media_thumbnail", []):
        u = m.get("url", "")
        if u and _is_image_url(u):
            urls.append(u)
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image/"):
            urls.append(enc.get("url", ""))
    for field in ("content", "summary"):
        html = ""
        val = entry.get(field)
        if isinstance(val, list):
            html = " ".join(v.get("value", "") for v in val)
        elif isinstance(val, str):
            html = val
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if src and _is_image_url(src):
                    urls.append(src)
    seen: set = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def build_content(entry) -> str:
    raw = ""
    for c in entry.get("content", []):
        raw = c.get("value", "")
        if raw:
            break
    if not raw:
        raw = entry.get("summary", "")
    raw = re.sub(r'<span[^>]*?>.*?FetchRSS.*?</span>', '', raw, flags=re.IGNORECASE | re.DOTALL )
    plain = BeautifulSoup(raw, "html.parser").get_text(separator="\n").strip()
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    return _escape_html(plain)


# ─────────────────────────────────────────────
#  SMART CAPTION SPLIT
# ─────────────────────────────────────────────

def smart_split(text: str) -> tuple:
    if len(text) <= MAX_CAPTION_LENGTH:
        return text, ""
    available = MAX_CAPTION_LENGTH - len(_SUFFIX_VISIBLE)
    if available <= 0:
        return text[:MAX_CAPTION_LENGTH], text[MAX_CAPTION_LENGTH:]
    window_start = max(0, available - max(50, int(available * 0.35)))
    pos = text.rfind("\n\n", window_start, available)
    if pos == -1:
        pos = text.rfind("\n\n", 0, available)
    if pos != -1:
        return text[:pos] + _SUFFIX_HTML, text[pos:].lstrip("\n")
    pos = text.rfind("\n", window_start, available)
    if pos == -1:
        pos = text.rfind("\n", 0, available)
    if pos != -1:
        return text[:pos] + _SUFFIX_HTML, text[pos:].lstrip("\n")
    pos = text.rfind(" ", 0, available)
    if pos == -1:
        pos = available
    return text[:pos] + _SUFFIX_HTML, text[pos:].lstrip()


# ─────────────────────────────────────────────
#  UPDATE OFFSET / GROUP MSG ID FINDER
# ─────────────────────────────────────────────

_update_offset: int = 0


async def _sync_offset(bot: Bot) -> None:
    global _update_offset
    try:
        updates = await bot.get_updates(offset=_update_offset, timeout=1,
                                        read_timeout=10, write_timeout=10)
        if updates:
            _update_offset = updates[-1].update_id + 1
    except TelegramError:
        pass


async def _find_group_msg_id(bot: Bot, channel_msg_id: int,
                              wait_max: int = 30) -> int | None:
    global _update_offset
    deadline = asyncio.get_event_loop().time() + wait_max
    log.info("Waiting for group forward of channel msg %d…", channel_msg_id)
    while asyncio.get_event_loop().time() < deadline:
        try:
            updates = await bot.get_updates(
                offset=_update_offset, timeout=3,
                read_timeout=15, write_timeout=15,
            )
        except TelegramError as e:
            log.warning("get_updates error: %s", e)
            await asyncio.sleep(2)
            continue
        for upd in updates:
            _update_offset = upd.update_id + 1
            msg = upd.message
            if msg is None:
                continue
            if str(msg.chat.id) != str(GROUP_ID):
                continue
            origin = msg.forward_origin
            if isinstance(origin, MessageOriginChannel):
                if origin.message_id == channel_msg_id:
                    log.info("Found group msg id=%d", msg.message_id)
                    return msg.message_id
        await asyncio.sleep(1)
    log.warning("Timed out waiting for group forward of channel msg %d", channel_msg_id)
    return None


# ─────────────────────────────────────────────
#  TELEGRAM POSTING
# ─────────────────────────────────────────────

async def post_entry(bot: Bot, entry) -> None:
    images = extract_images(entry)
    if not images:
        log.info("Skipping (no images): '%s'", entry.get("title", "—")[:60])
        return

    content = build_content(entry)
    caption, remainder = smart_split(content)
    channel_images  = images[:MAX_IMAGES_PER_POST]
    overflow_images = images[MAX_IMAGES_PER_POST:]

    log.info("Posting: '%s' | imgs=%d overflow=%d spillover_text=%s",
             entry.get("title", "—")[:60], len(channel_images),
             len(overflow_images), bool(remainder))

    channel_msg = await _send_photos(bot, CHANNEL_ID, channel_images, caption, reply_to=None)

    if not GROUP_ID:
        if remainder or overflow_images:
            log.warning("DISCUSSION_GROUP_ID not set – overflow not posted.")
        return

    if channel_msg is None:
        return

    if not remainder and not overflow_images:
        await _sync_offset(bot)
        return

    group_msg_id = await _find_group_msg_id(bot, channel_msg.message_id)
    if group_msg_id is None:
        record_error(f"Could not find group msg for channel msg {channel_msg.message_id} "
                     f"— overflow/comment skipped for: {entry.get('title','?')[:60]}")
        return

    if remainder:
        await _send_comment(bot, remainder, group_msg_id)
        await asyncio.sleep(1)

    if overflow_images:
        await _send_photos_comment(bot, overflow_images, group_msg_id)


async def _send_photos(bot: Bot, chat_id: str, image_urls: list,
                       caption: str, reply_to) -> object:
    kwargs = {}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to

    if len(image_urls) == 1:
        try:
            return await bot.send_photo(
                chat_id=chat_id, photo=image_urls[0],
                caption=caption, parse_mode=ParseMode.HTML,
                read_timeout=60, write_timeout=60, connect_timeout=30,
                **kwargs,
            )
        except TelegramError as e:
            record_error(f"send_photo failed: {e}")
            return None

    media_group = [
        InputMediaPhoto(
            media=url,
            caption=caption if i == 0 else None,
            parse_mode=ParseMode.HTML if i == 0 else None,
        )
        for i, url in enumerate(image_urls)
    ]
    try:
        msgs = await bot.send_media_group(
            chat_id=chat_id, media=media_group,
            read_timeout=60, write_timeout=60, connect_timeout=30,
            **kwargs,
        )
        return msgs[0] if msgs else None
    except TelegramError as e:
        log.warning("send_media_group failed (%s) – retrying with first image only", e)
        await asyncio.sleep(3)
        try:
            return await bot.send_photo(
                chat_id=chat_id, photo=image_urls[0],
                caption=caption, parse_mode=ParseMode.HTML,
                read_timeout=60, write_timeout=60,
                **kwargs,
            )
        except TelegramError as e2:
            record_error(f"send_photo fallback also failed: {e2}")
            return None


async def _send_comment(bot: Bot, text: str, reply_to_msg_id: int) -> None:
    for chunk in _split_long_text(text):
        for attempt in range(3):
            try:
                await bot.send_message(
                    chat_id=GROUP_ID, text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to_msg_id,
                )
                await asyncio.sleep(1)
                break
            except TelegramError as e:
                wait = (attempt + 1) * 5
                log.warning("Comment failed (attempt %d/3): %s – retry in %ds",
                            attempt + 1, e, wait)
                await asyncio.sleep(wait)
        else:
            record_error(f"Comment permanently failed after 3 attempts: {text[:80]}…")


async def _send_photos_comment(bot: Bot, image_urls: list, reply_to_msg_id: int) -> None:
    for i in range(0, len(image_urls), MAX_IMAGES_PER_POST):
        batch = image_urls[i:i + MAX_IMAGES_PER_POST]
        await _send_photos(bot, GROUP_ID, batch, caption="",
                           reply_to=reply_to_msg_id)
        await asyncio.sleep(1)


def _split_long_text(text: str) -> list:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    chunks = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break
        pos = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if pos == -1:
            pos = MAX_MESSAGE_LENGTH
        chunks.append(text[:pos].strip())
        text = text[pos:].strip()
    return chunks


# ─────────────────────────────────────────────
#  DM REPORT
# ─────────────────────────────────────────────

async def send_dm_report(bot: Bot, posted: int, skipped: int) -> None:
    """Send a summary (and any errors) to the admin via DM."""
    if not ADMIN_ID:
        return

    lines = [f"✅ <b>RSS bot run complete</b>",
             f"• Posted: {posted}",
             f"• Skipped (no image): {skipped}"]

    if _errors:
        lines.append(f"\n⚠️ <b>{len(_errors)} error(s):</b>")
        for err in _errors:
            lines.append(f"<code>{_escape_html(err[:300])}</code>")
    else:
        lines.append("• No errors 🎉")

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        log.error("Could not send DM report: %s", e)


# ─────────────────────────────────────────────
#  MAIN  (one-shot)
# ─────────────────────────────────────────────

async def main() -> None:
    posted = 0
    skipped = 0

    async with Bot(token=BOT_TOKEN) as bot:
        me = await bot.get_me()
        log.info("Authenticated as @%s", me.username)

        # Sync offset so we don't process stale updates
        await _sync_offset(bot)

        seen_ids = load_seen_ids()
        log.info("Loaded %d seen IDs", len(seen_ids))

        try:
            entries = fetch_feed(RSS_URL)
        except Exception as e:
            record_error(f"Feed fetch failed: {traceback.format_exc()}")
            await send_dm_report(bot, posted, skipped)
            return

        new_entries = [e for e in reversed(entries) if item_id(e) not in seen_ids]
        log.info("%d new entries to process", len(new_entries))

        for entry in new_entries:
            eid = item_id(entry)
            images = extract_images(entry)

            if not images:
                log.info("Skipping (no images): '%s'", entry.get("title", "—")[:60])
                skipped += 1
                seen_ids.add(eid)   # mark as seen so we don't re-check every run
                save_seen_ids(seen_ids)
                continue

            try:
                await post_entry(bot, entry)
                posted += 1
            except Exception as e:
                record_error(f"Failed to post '{entry.get('title','?')[:60]}': "
                             f"{traceback.format_exc()}")

            seen_ids.add(eid)
            save_seen_ids(seen_ids)
            await asyncio.sleep(2)

        # Trim seen_ids to only entries still present in the feed.
        # Hashes for items that have rolled off the feed are useless —
        # this keeps seen_items.json small (≈ feed size) forever.
        current_ids = {item_id(e) for e in entries}
        seen_ids &= current_ids
        save_seen_ids(seen_ids)
        log.info("seen_items.json trimmed to %d active entries", len(seen_ids))

        log.info("Done. Posted=%d Skipped=%d Errors=%d", posted, skipped, len(_errors))
        await send_dm_report(bot, posted, skipped)


if __name__ == "__main__":
    asyncio.run(main())
