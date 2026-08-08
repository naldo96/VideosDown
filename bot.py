"""
Bot de Telegram: descarga video desde un enlace (YouTube, TikTok, Instagram,
Twitter/X, Facebook, etc.) y lo reenvía por Telegram SIN dejar ningún
archivo guardado en el VPS.

Autónomo: NO depende de cookies ni sesiones de navegador.
Estrategia:
- YouTube: clientes android / ios / tv + PO Token (bgutil-provider).
- TikTok / resto: yt-dlp con headers de app móvil y reintentos.
- Cascada de estrategias: si una falla, prueba la siguiente.

Aprobación de usuarios en MongoDB (solo admin aprueba).
Descargas en tmp efímero + tmpfs → nada queda en disco del VPS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
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

# --- Configuración (hardcodeada a pedido del usuario) ---
BOT_TOKEN = "8919924327:AAHMrSgNVRf-d4vzvu4Lzy9mPjhgGYDY1OM"
ADMIN_CHAT_ID = 501203904
MONGO_URI = "mongodb+srv://BotiCAM:Tito1996@cluster0.zxzdojv.mongodb.net/"
MAX_FILE_SIZE_MB = 50
TMP_PREFIX = "tgbot_"
BGUTIL_BASE_URL = os.environ.get("BGUTIL_BASE_URL", "http://bgutil-provider:4416")

URL_REGEX = re.compile(r"https?://\S+")

# --- MongoDB ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_video_bot"]
users_col = db["users"]


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


def _detect_platform(url: str) -> str:
    u = url.lower()
    if any(x in u for x in ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")):
        return "youtube"
    if any(x in u for x in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")):
        return "tiktok"
    if any(x in u for x in ("instagram.com", "instagr.am")):
        return "instagram"
    if any(x in u for x in ("twitter.com", "x.com", "t.co")):
        return "twitter"
    if any(x in u for x in ("facebook.com", "fb.watch", "fb.com", "fb.me")):
        return "facebook"
    return "generic"


def _base_ydl_opts(tmp_dir: str) -> dict[str, Any]:
    return {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": (
            f"bv*[filesize<{MAX_FILE_SIZE_MB}M]+ba[filesize<{MAX_FILE_SIZE_MB}M]/"
            f"bv*[filesize_approx<{MAX_FILE_SIZE_MB}M]+ba[filesize_approx<{MAX_FILE_SIZE_MB}M]/"
            f"b[filesize<{MAX_FILE_SIZE_MB}M]/b[filesize_approx<{MAX_FILE_SIZE_MB}M]/"
            f"bv*[height<=720]+ba/b[height<=720]/"
            f"bv*[height<=480]+ba/b[height<=480]/best"
        ),
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
        "file_access_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "paths": {"home": tmp_dir, "temp": tmp_dir},
        # Nunca cookies / nunca browser session
        "cookiefile": None,
        "cookiesfrombrowser": None,
    }


def _strategies_for(url: str) -> list[tuple[str, dict[str, Any]]]:
    """
    Cascada de estrategias SIN cookies.
    Cada ítem: (nombre, overrides sobre _base_ydl_opts).
    """
    platform = _detect_platform(url)
    strategies: list[tuple[str, dict[str, Any]]] = []

    if platform == "youtube":
        # Clientes que NO requieren cookies de cuenta.
        # PO Token vía bgutil-provider (companion en docker-compose).
        strategies.append(
            (
                "yt-android+pot",
                {
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["android", "android_sdkless"],
                            "player_skip": ["webpage", "configs"],
                        },
                        "youtubepot-bgutilhttp": {"base_url": BGUTIL_BASE_URL},
                    },
                },
            )
        )
        strategies.append(
            (
                "yt-ios+pot",
                {
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["ios", "ios_music"],
                            "player_skip": ["webpage", "configs"],
                        },
                        "youtubepot-bgutilhttp": {"base_url": BGUTIL_BASE_URL},
                    },
                },
            )
        )
        strategies.append(
            (
                "yt-tv",
                {
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["tv", "tv_embedded", "mediaconnect"],
                            "player_skip": ["webpage"],
                        },
                        "youtubepot-bgutilhttp": {"base_url": BGUTIL_BASE_URL},
                    },
                },
            )
        )
        strategies.append(
            (
                "yt-mweb+pot",
                {
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["mweb", "web_safari"],
                        },
                        "youtubepot-bgutilhttp": {"base_url": BGUTIL_BASE_URL},
                    },
                },
            )
        )
    elif platform == "tiktok":
        strategies.append(
            (
                "tt-mobile",
                {
                    "http_headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Mobile/15E148 Safari/604.1"
                        ),
                        "Referer": "https://www.tiktok.com/",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    "extractor_args": {
                        "tiktok": {
                            "api_hostname": "api16-normal-c-useast1a.tiktokv.com",
                        }
                    },
                },
            )
        )
        strategies.append(
            (
                "tt-android-ua",
                {
                    "http_headers": {
                        "User-Agent": (
                            "com.zhiliaoapp.musically/2023501030 "
                            "(Linux; U; Android 13; en_US; Pixel 7; "
                            "Build/TQ3A.230901.001; "
                            "Cronet/58.0.2991.0)"
                        ),
                        "Referer": "https://www.tiktok.com/",
                    },
                },
            )
        )
        strategies.append(("tt-default", {}))
    elif platform == "instagram":
        # Público sin login: a veces funciona; privados/reels restringidos no.
        strategies.append(
            (
                "ig-mobile",
                {
                    "http_headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Mobile/15E148 Safari/604.1"
                        ),
                        "Referer": "https://www.instagram.com/",
                        "X-IG-App-ID": "936619743392459",
                    },
                },
            )
        )
        strategies.append(("ig-default", {}))
    elif platform == "twitter":
        strategies.append(
            (
                "x-mobile",
                {
                    "http_headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Mobile/15E148 Safari/604.1"
                        ),
                        "Referer": "https://x.com/",
                    },
                },
            )
        )
        strategies.append(("x-default", {}))
    else:
        strategies.append(("generic-mobile", {}))
        strategies.append(
            (
                "generic-desktop",
                {
                    "http_headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        ),
                    },
                },
            )
        )

    return strategies


def _resolve_filename(info: dict, filename: str) -> str:
    if os.path.exists(filename):
        return filename
    base, _ = os.path.splitext(filename)
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    # A veces yt-dlp deja el archivo con otro id; busca cualquier media en el dir
    directory = os.path.dirname(filename) or "."
    for name in os.listdir(directory):
        if name.startswith("_"):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path) and name.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            return path
    return filename


def _download_once(url: str, ydl_opts: dict[str, Any]) -> tuple[dict, str]:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("yt-dlp no devolvió metadata")
        # playlists / entries
        if "entries" in info and info["entries"]:
            info = next(e for e in info["entries"] if e)
        filename = ydl.prepare_filename(info)
        filename = _resolve_filename(info, filename)
        return info, filename


def download_video(url: str, tmp_dir: str) -> tuple[dict, str, str]:
    """
    Intenta varias estrategias sin cookies.
    Devuelve (info, filename, strategy_name).
    """
    errors: list[str] = []
    strategies = _strategies_for(url)
    platform = _detect_platform(url)
    logger.info("Plataforma=%s estrategias=%s", platform, [s[0] for s in strategies])

    for name, overrides in strategies:
        opts = _base_ydl_opts(tmp_dir)
        # merge profundo simple de extractor_args / headers
        for key, value in overrides.items():
            if key == "http_headers":
                opts["http_headers"] = {**opts.get("http_headers", {}), **value}
            elif key == "extractor_args":
                base_ea = opts.get("extractor_args", {})
                merged = dict(base_ea)
                for ek, ev in value.items():
                    if isinstance(ev, dict) and isinstance(merged.get(ek), dict):
                        merged[ek] = {**merged[ek], **ev}
                    else:
                        merged[ek] = ev
                opts["extractor_args"] = merged
            else:
                opts[key] = value

        # Limpia restos de intentos previos en el mismo tmp
        for leftover in os.listdir(tmp_dir):
            path = os.path.join(tmp_dir, leftover)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

        try:
            logger.info("Probando estrategia: %s", name)
            info, filename = _download_once(url, opts)
            if not os.path.exists(filename):
                raise FileNotFoundError(f"archivo no generado ({filename})")
            logger.info("OK con estrategia %s → %s", name, filename)
            return info, filename, name
        except Exception as e:
            err = f"{name}: {e}"
            logger.warning("Falló %s", err)
            errors.append(err)
            time.sleep(0.4)
            continue

    joined = " | ".join(errors[-4:])  # últimas causas
    raise RuntimeError(f"Todas las estrategias fallaron. Últimos errores: {joined}")


def _friendly_download_error(exc: Exception, url: str) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    platform = _detect_platform(url)

    if any(
        k in low
        for k in (
            "sign in to confirm",
            "not a bot",
            "login required",
            "private video",
            "this video is private",
            "http error 403",
            "only images are available",
            "requested format is not available",
        )
    ):
        extra = {
            "youtube": (
                "YouTube bloqueó la IP del VPS o el video es restringido. "
                "El bot ya reintenta con android/ios/tv + PO Token; "
                "si sigue fallando, suele ser rate-limit temporal o video age-restricted."
            ),
            "tiktok": (
                "TikTok a veces bloquea IPs de datacenter en videos regionales. "
                "Prueba otro enlace público; no se usan cookies."
            ),
            "instagram": (
                "Instagram suele exigir login para muchos reels/posts. "
                "Este bot es autónomo (sin sesión): solo posts públicos abiertos."
            ),
        }.get(platform, "El sitio rechazó la descarga anónima.")
        return f"❌ No se pudo descargar (sin cookies/sesión).\n{extra}\n\nDetalle: {msg}"

    if any(k in low for k in ("unsupported url", "no video formats", "is not a valid url")):
        return f"❌ Enlace no soportado o sin video descargable.\nDetalle: {msg}"

    if "todas las estrategias fallaron" in low:
        return (
            "❌ No pude bajar el video con ninguna estrategia autónoma "
            f"(plataforma: {platform}).\n\n{msg}"
        )

    return f"❌ Error al descargar: {msg}"


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    if chat_id == ADMIN_CHAT_ID:
        await asyncio.to_thread(_ensure_admin_approved)
        await update.message.reply_text(
            "👑 Admin listo.\n"
            "Modo: autónomo (sin cookies / sin sesiones).\n"
            "YouTube usa clientes android/ios/tv + PO Token (bgutil).\n"
            "Mándame un enlace o espera solicitudes de acceso."
        )
        return

    existing = await asyncio.to_thread(_get_user, chat_id)

    if existing and existing.get("status") == "approved":
        await update.message.reply_text(
            "✅ Ya tienes acceso. Envíame el enlace de un video y te lo mando aquí."
        )
        return

    if existing and existing.get("status") == "pending":
        await update.message.reply_text("⏳ Tu solicitud ya fue enviada, espera la aprobación del admin.")
        return

    username = user.username or "(sin usuario)"
    first_name = user.first_name or ""
    if existing and existing.get("status") == "rejected":
        await asyncio.to_thread(_set_status, chat_id, "pending")
    else:
        await asyncio.to_thread(_create_pending_user, chat_id, username, first_name)

    await update.message.reply_text(
        "📨 Solicitud enviada al administrador. Te aviso cuando responda."
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Aceptar", callback_data=f"approve_{chat_id}"),
                InlineKeyboardButton("❌ Rechazar", callback_data=f"reject_{chat_id}"),
            ]
        ]
    )
    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🔔 Nueva solicitud de acceso\n"
            f"Nombre: {first_name}\n"
            f"Usuario: @{username}\n"
            f"Chat ID: {chat_id}",
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
    target_chat_id = int(chat_id_str)
    new_status = "approved" if action == "approve" else "rejected"

    await asyncio.to_thread(_set_status, target_chat_id, new_status)
    await query.answer()

    label = "✅ Aceptado" if new_status == "approved" else "❌ Rechazado"
    try:
        await query.edit_message_text(f"{query.message.text}\n\n{label}")
    except Exception:
        pass

    try:
        if new_status == "approved":
            await context.bot.send_message(
                target_chat_id, "✅ ¡Fuiste aceptado! Ya puedes enviarme el enlace de un video."
            )
        else:
            await context.bot.send_message(
                target_chat_id, "❌ Tu solicitud de acceso fue rechazada por el administrador."
            )
    except Exception as e:
        logger.error("No pude notificar al usuario %s: %s", target_chat_id, e)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Mándame un enlace válido de un video 🎬, o usa /start si aún no tienes acceso."
        )
        return

    if chat_id != ADMIN_CHAT_ID:
        user = await asyncio.to_thread(_get_user, chat_id)
        if not user or user.get("status") != "approved":
            await update.message.reply_text("🔒 No tienes acceso todavía. Usa /start para solicitar acceso.")
            return

    url = match.group(0).rstrip(").,]>'\"")
    platform = _detect_platform(url)
    status_msg = await update.message.reply_text(f"⏳ Descargando ({platform})…")

    tmp_dir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    try:
        info, filename, strategy = await asyncio.to_thread(download_video, url, tmp_dir)

        if not os.path.exists(filename):
            await status_msg.edit_text("❌ No pude generar el archivo de video.")
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
        logger.error("Error procesando %s: %s", url, e)
        try:
            await status_msg.edit_text(_friendly_download_error(e, url))
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta BOT_TOKEN")

    _cleanup_stale_tmp_dirs()
    _ensure_admin_approved()

    logger.info("Modo AUTÓNOMO: sin cookies / sin sesiones de navegador")
    logger.info("PO Token provider: %s", BGUTIL_BASE_URL)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_admin_decision, pattern=r"^(approve|reject)_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado, esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
