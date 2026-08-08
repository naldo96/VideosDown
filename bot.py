"""
Bot de Telegram — TikTok + Instagram + Facebook (sin cookies / sin sesiones).

Probado en consola (2026-08-08):
- TikTok  → TikWM API (autónomo) + yt-dlp backup
- Instagram públicos → yt-dlp (sin impersonate roto)
- Facebook públicos → yt-dlp formato sd/worst (<50MB Telegram)

Aprobación de usuarios en MongoDB.
Descargas en tmp + tmpfs → nada queda en el VPS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import yt_dlp
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Credenciales hardcodeadas (pedido del usuario) ---
BOT_TOKEN = "8919924327:AAHMrSgNVRf-d4vzvu4Lzy9mPjhgGYDY1OM"
ADMIN_CHAT_ID = 501203904
MONGO_URI = "mongodb+srv://BotiCAM:Tito1996@cluster0.zxzdojv.mongodb.net/"
MAX_FILE_SIZE_MB = 50
TMP_PREFIX = "tgbot_"

URL_REGEX = re.compile(r"https?://\S+")
TIKTOK_ID_RE = re.compile(r"(?:video|photo)/(\d{8,})")
TIKTOK_SHORT_RE = re.compile(r"https?://(?:vm|vt)\.tiktok\.com/\S+", re.I)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_video_bot"]
users_col = db["users"]


# ---------- Mongo ----------

def _get_user(chat_id: int):
    return users_col.find_one({"_id": chat_id})


def _create_pending_user(chat_id: int, username: str, first_name: str):
    users_col.update_one(
        {"_id": chat_id},
        {
            "$setOnInsert": {
                "_id": chat_id,
                "username": username,
                "first_name": first_name,
                "status": "pending",
                "requested_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return _get_user(chat_id)


def _set_status(chat_id: int, status: str):
    users_col.update_one(
        {"_id": chat_id},
        {"$set": {"status": status, "decided_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def _ensure_admin_approved():
    users_col.update_one(
        {"_id": ADMIN_CHAT_ID},
        {"$set": {"status": "approved", "username": "admin", "is_admin": True}},
        upsert=True,
    )


def _cleanup_stale_tmp_dirs() -> None:
    base = tempfile.gettempdir()
    for name in os.listdir(base):
        if name.startswith(TMP_PREFIX):
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)


# ---------- Platform / download ----------

def _detect_platform(url: str) -> str:
    u = url.lower()
    if any(x in u for x in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")):
        return "tiktok"
    if any(x in u for x in ("instagram.com", "instagr.am")):
        return "instagram"
    if any(x in u for x in ("facebook.com", "fb.watch", "fb.com", "fb.me", "fb.gg")):
        return "facebook"
    if any(x in u for x in ("youtube.com", "youtu.be", "music.youtube.com")):
        return "youtube"
    if any(x in u for x in ("twitter.com", "x.com", "t.co")):
        return "twitter"
    return "generic"


def _format_selector(platform: str) -> str:
    """
    FB solo expone sd/hd sin filesize fiable.
    En pruebas: hd ~450MB (no cabe), sd/worst ~40MB (sí).
    Preferir sd/worst y luego límites de peso/altura.
    """
    m = MAX_FILE_SIZE_MB
    if platform == "facebook":
        return (
            f"sd/worst/"
            f"best[filesize<{m}M]/best[filesize_approx<{m}M]/"
            f"bv*[height<=480]+ba/b[height<=480]/best"
        )
    return (
        f"bv*[filesize<{m}M]+ba[filesize<{m}M]/"
        f"bv*[filesize_approx<{m}M]+ba[filesize_approx<{m}M]/"
        f"b[filesize<{m}M]/b[filesize_approx<{m}M]/"
        f"bv*[height<=720]+ba/b[height<=720]/"
        f"bv*[height<=480]+ba/b[height<=480]/best"
    )


def _base_ydl_opts(tmp_dir: str, platform: str) -> dict[str, Any]:
    return {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": _format_selector(platform),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "cachedir": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeinfojson": False,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 40,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "paths": {"home": tmp_dir, "temp": tmp_dir},
        # Sin cookies / sin browser session
        "cookiefile": None,
        "cookiesfrombrowser": None,
        # NO usar impersonate genérico: en varios entornos rompe yt-dlp.
        # IG/FB públicos funcionan sin él (probado).
    }


def _clean_tmp(tmp_dir: str) -> None:
    for name in os.listdir(tmp_dir):
        path = os.path.join(tmp_dir, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _pick_media_file(tmp_dir: str, preferred: str | None = None) -> str | None:
    if preferred and os.path.isfile(preferred) and os.path.getsize(preferred) > 1000:
        return preferred
    candidates: list[tuple[int, str]] = []
    for name in os.listdir(tmp_dir):
        if name.startswith("_"):
            continue
        path = os.path.join(tmp_dir, name)
        if not os.path.isfile(path):
            continue
        low = name.lower()
        if low.endswith((".mp4", ".mkv", ".webm", ".mov", ".m4v")):
            candidates.append((os.path.getsize(path), path))
    if not candidates:
        return None
    # El más grande que quepa; si todos caben, el más grande
    under = [(s, p) for s, p in candidates if s <= MAX_FILE_SIZE_MB * 1024 * 1024]
    pool = under or candidates
    pool.sort(reverse=True)
    return pool[0][1]


def _http_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.tikwm.com/",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _http_download(url: str, dest: str, timeout: int = 120) -> None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 256)


def _expand_tiktok_url(url: str) -> str:
    if not TIKTOK_SHORT_RE.match(url):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.geturl() or url
    except Exception as e:
        logger.warning("No pude expandir short TikTok: %s", e)
        return url


def _download_tiktok_via_tikwm(url: str, tmp_dir: str) -> tuple[dict, str]:
    """Fallback/preferido TikTok sin cookies (probado OK en VPS y aquí)."""
    expanded = _expand_tiktok_url(url)
    api_url = "https://www.tikwm.com/api/?" + urllib.parse.urlencode({"url": expanded, "hd": "1"})
    logger.info("TikWM: %s", api_url)
    payload = _http_json(api_url)
    if not isinstance(payload, dict):
        raise RuntimeError("TikWM respuesta inválida")
    code = payload.get("code")
    if code not in (0, "0"):
        raise RuntimeError(f"TikWM error: {payload.get('msg') or payload}")
    data = payload.get("data") or {}
    if not data:
        raise RuntimeError(f"TikWM sin data: {payload.get('msg') or payload}")
    if data.get("images") and not (data.get("play") or data.get("hdplay")):
        raise RuntimeError("Este TikTok es un carrusel de fotos, no un video")
    play = data.get("hdplay") or data.get("play") or data.get("wmplay")
    if not play or not isinstance(play, str):
        raise RuntimeError("TikWM no devolvió URL de video")
    m = TIKTOK_ID_RE.search(expanded)
    vid = str(data.get("id") or (m.group(1) if m else int(time.time())))
    dest = os.path.join(tmp_dir, f"{vid}.mp4")
    _http_download(play, dest)
    if not os.path.exists(dest) or os.path.getsize(dest) < 1000:
        raise RuntimeError("TikWM descargó archivo vacío")
    author = data.get("author") or {}
    info = {
        "id": vid,
        "title": data.get("title") or f"tiktok_{vid}",
        "uploader": author.get("unique_id") or author.get("nickname") or "",
        "webpage_url": expanded,
        "extractor": "tikwm",
    }
    return info, dest


def _download_with_ytdlp(url: str, tmp_dir: str, platform: str) -> tuple[dict, str]:
    opts = _base_ydl_opts(tmp_dir, platform)
    # Headers un poco más específicos por plataforma
    if platform == "instagram":
        opts["http_headers"] = {
            **opts["http_headers"],
            "Referer": "https://www.instagram.com/",
            "X-IG-App-ID": "936619743392459",
        }
    elif platform == "facebook":
        opts["http_headers"] = {
            **opts["http_headers"],
            "Referer": "https://www.facebook.com/",
        }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("yt-dlp no devolvió metadata")
        # Carrusel IG: varias entries → bajamos todas y elegimos un video
        if "entries" in info and info["entries"]:
            entries = [e for e in info["entries"] if e]
            info = entries[0]
        filename = ydl.prepare_filename(info)
        picked = _pick_media_file(tmp_dir, filename)
        if not picked:
            # a veces la extensión cambia tras merge
            base, _ = os.path.splitext(filename)
            for ext in (".mp4", ".mkv", ".webm", ".mov"):
                if os.path.exists(base + ext):
                    picked = base + ext
                    break
        if not picked or not os.path.exists(picked):
            raise FileNotFoundError("yt-dlp no generó archivo de video")
        return info, picked


def download_video(url: str, tmp_dir: str) -> tuple[dict, str, str]:
    platform = _detect_platform(url)
    errors: list[str] = []
    logger.info("Descarga platform=%s url=%s", platform, url)

    if platform == "youtube":
        raise RuntimeError(
            "YouTube está desactivado en este bot. Usa TikTok, Instagram o Facebook."
        )

    # --- TikTok: TikWM primero (lo que te funcionó en el VPS) ---
    if platform == "tiktok":
        try:
            _clean_tmp(tmp_dir)
            info, path = _download_tiktok_via_tikwm(url, tmp_dir)
            return info, path, "tt-tikwm"
        except Exception as e:
            logger.warning("TikWM falló: %s", e)
            errors.append(f"tt-tikwm: {e}")

    # --- IG / FB / TikTok backup: yt-dlp ---
    strategies: list[tuple[str, str]] = []
    if platform == "instagram":
        strategies = [("ig-ytdlp", url)]
    elif platform == "facebook":
        strategies = [("fb-ytdlp", url)]
        # Normalizar fb.watch se deja a yt-dlp
    elif platform == "tiktok":
        strategies = [("tt-ytdlp", url)]
    else:
        strategies = [("generic-ytdlp", url)]

    for name, u in strategies:
        try:
            _clean_tmp(tmp_dir)
            logger.info("Probando %s", name)
            info, path = _download_with_ytdlp(u, tmp_dir, platform)
            logger.info("OK %s → %s (%s bytes)", name, path, os.path.getsize(path))
            return info, path, name
        except Exception as e:
            logger.warning("Falló %s: %s", name, e)
            errors.append(f"{name}: {e}")

    raise RuntimeError(
        "Todas las estrategias fallaron. Últimos errores: " + " | ".join(errors[-4:])
    )


def _friendly_error(exc: Exception, url: str) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    platform = _detect_platform(url)

    if platform == "youtube":
        return "❌ YouTube no está habilitado. Manda TikTok, Instagram o Facebook."

    if "login" in low or "cookies" in low or "registered users" in low:
        return (
            f"❌ Ese contenido de {platform} requiere cuenta/login.\n"
            "Este bot es autónomo (sin cookies). Prueba un post/video **público**.\n"
            f"Detalle: {msg}"
        )
    if "empty media" in low or "not granting access" in low:
        return (
            "❌ Instagram no entregó el media (post privado, age-gate o restringido).\n"
            "Prueba un reel/post abierto sin login en el navegador.\n"
            f"Detalle: {msg}"
        )
    if "cannot parse data" in low:
        return (
            "❌ Facebook no dejó parsear ese enlace (a veces pasa con posts viejos "
            "o solo-amigos).\nPrueba un facebook.com/watch/?v=... público.\n"
            f"Detalle: {msg}"
        )
    if "carrusel de fotos" in low:
        return "❌ Ese TikTok es un carrusel de fotos, no un video."
    if "todas las estrategias" in low:
        return f"❌ No pude bajar el video ({platform}).\n\n{msg}"
    return f"❌ Error al descargar: {msg}"


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    if chat_id == ADMIN_CHAT_ID:
        await asyncio.to_thread(_ensure_admin_approved)
        await update.message.reply_text(
            "👑 Admin listo.\n"
            "Plataformas: TikTok (TikWM) · Instagram · Facebook\n"
            "Modo autónomo: sin cookies / sin sesiones\n"
            "Límite: 50MB (Telegram Bot API)\n"
            "Mándame un enlace o espera solicitudes de acceso."
        )
        return

    existing = await asyncio.to_thread(_get_user, chat_id)
    if existing and existing.get("status") == "approved":
        await update.message.reply_text(
            "✅ Ya tienes acceso.\n"
            "Envíame un enlace de TikTok, Instagram o Facebook."
        )
        return
    if existing and existing.get("status") == "pending":
        await update.message.reply_text("⏳ Solicitud pendiente de aprobación del admin.")
        return

    username = user.username or "(sin usuario)"
    first_name = user.first_name or ""
    if existing and existing.get("status") == "rejected":
        await asyncio.to_thread(_set_status, chat_id, "pending")
    else:
        await asyncio.to_thread(_create_pending_user, chat_id, username, first_name)

    await update.message.reply_text("📨 Solicitud enviada al admin. Te aviso cuando responda.")
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Aceptar", callback_data=f"approve_{chat_id}"),
            InlineKeyboardButton("❌ Rechazar", callback_data=f"reject_{chat_id}"),
        ]]
    )
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🔔 Nueva solicitud\nNombre: {first_name}\nUsuario: @{username}\nChat ID: {chat_id}",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error("No pude notificar al admin: %s", e)


async def handle_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("No autorizado.", show_alert=True)
        return

    action, chat_id_str = query.data.split("_", 1)
    target = int(chat_id_str)
    new_status = "approved" if action == "approve" else "rejected"
    await asyncio.to_thread(_set_status, target, new_status)
    await query.answer()
    label = "✅ Aceptado" if new_status == "approved" else "❌ Rechazado"
    try:
        await query.edit_message_text(f"{query.message.text}\n\n{label}")
    except Exception:
        pass
    try:
        if new_status == "approved":
            await context.bot.send_message(
                target, "✅ ¡Aceptado! Mándame un enlace de TikTok, Instagram o Facebook."
            )
        else:
            await context.bot.send_message(target, "❌ Solicitud rechazada.")
    except Exception as e:
        logger.error("No pude notificar al usuario %s: %s", target, e)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Mándame un enlace de TikTok, Instagram o Facebook 🎬\n"
            "O usa /start si aún no tienes acceso."
        )
        return

    if chat_id != ADMIN_CHAT_ID:
        user = await asyncio.to_thread(_get_user, chat_id)
        if not user or user.get("status") != "approved":
            await update.message.reply_text("🔒 Sin acceso. Usa /start para solicitarlo.")
            return

    url = match.group(0).rstrip(").,]>'\"")
    platform = _detect_platform(url)
    status_msg = await update.message.reply_text(f"⏳ Descargando ({platform})…")

    tmp_dir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    try:
        info, filename, strategy = await asyncio.to_thread(download_video, url, tmp_dir)

        if not os.path.exists(filename):
            await status_msg.edit_text("❌ No se generó el archivo de video.")
            return

        size_mb = os.path.getsize(filename) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"❌ El video pesa {size_mb:.1f}MB y supera el límite de {MAX_FILE_SIZE_MB}MB "
                "de la Bot API de Telegram."
            )
            return

        await status_msg.edit_text(f"📤 Enviando… ({strategy}, {size_mb:.1f}MB)")
        caption = (info.get("title") or "")[:900]
        with open(filename, "rb") as f:
            await update.message.reply_video(video=f, caption=caption)
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error("Error %s: %s", url, e)
        try:
            await status_msg.edit_text(_friendly_error(e, url))
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta BOT_TOKEN")

    _cleanup_stale_tmp_dirs()
    _ensure_admin_approved()

    logger.info("Bot TT/IG/FB autónomo (sin cookies)")
    logger.info("Límite envío: %sMB", MAX_FILE_SIZE_MB)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_admin_decision, pattern=r"^(approve|reject)_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado, esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
