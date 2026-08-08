"""
Bot de Telegram: descarga video desde un enlace (YouTube, TikTok, Instagram,
Twitter/X, Facebook, etc.) y lo reenvía por Telegram SIN dejar ningún
archivo guardado en el VPS.

Autónomo: NO depende de cookies ni sesiones de navegador.
Estrategia:
- YouTube: clientes android / ios / tv + PO Token (bgutil-provider).
- TikTok: yt-dlp con impersonate (curl_cffi) + API móvil + fallback TikWM.
- Resto: headers móviles y reintentos.
- Cascada: si una falla, prueba la siguiente.

Aprobación de usuarios en MongoDB (solo admin aprueba).
Descargas en tmp efímero + tmpfs → nada queda en disco del VPS.
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

# --- Configuración (hardcodeada a pedido del usuario) ---
BOT_TOKEN = "8919924327:AAHMrSgNVRf-d4vzvu4Lzy9mPjhgGYDY1OM"
ADMIN_CHAT_ID = 501203904
MONGO_URI = "mongodb+srv://BotiCAM:Tito1996@cluster0.zxzdojv.mongodb.net/"
MAX_FILE_SIZE_MB = 50
TMP_PREFIX = "tgbot_"
BGUTIL_BASE_URL = os.environ.get("BGUTIL_BASE_URL", "http://bgutil-provider:4416")

URL_REGEX = re.compile(r"https?://\S+")
TIKTOK_ID_RE = re.compile(r"(?:video|photo)/(\d{8,})")
TIKTOK_SHORT_RE = re.compile(r"https?://(?:vm|vt)\.tiktok\.com/\S+", re.I)

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


# Targets REALES de yt-dlp/curl_cffi (NO existen "chrome" ni "safari" genéricos).
# Ver: yt-dlp --list-impersonate-targets
_IMPERSONATE_PREF = (
    "chrome131",
    "chrome131_android",
    "chrome124",
    "chrome120",
    "chrome116",
    "chrome110",
    "safari17_2_ios",
    "safari17_0",
    "edge101",
)


def _impersonate_available() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except Exception:
        return False


def _pick_impersonate_target() -> str | None:
    """Elige un target válido instalado; None si no hay curl_cffi/targets."""
    if not _impersonate_available():
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        # Preferidos conocidos
        for name in _IMPERSONATE_PREF:
            try:
                # yt-dlp acepta string "chrome131" o ImpersonateTarget
                return name
            except Exception:
                continue
        return "chrome131"
    except Exception:
        return "chrome131"


_DEFAULT_IMPERSONATE = _pick_impersonate_target()


def _base_ydl_opts(tmp_dir: str) -> dict[str, Any]:
    opts: dict[str, Any] = {
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
        "cookiefile": None,
        "cookiesfrombrowser": None,
    }
    # Solo si hay target válido. Nunca "chrome"/"safari" genéricos.
    if _DEFAULT_IMPERSONATE:
        opts["impersonate"] = _DEFAULT_IMPERSONATE
    return opts


def _strategies_for(url: str) -> list[tuple[str, dict[str, Any]]]:
    platform = _detect_platform(url)
    strategies: list[tuple[str, dict[str, Any]]] = []

    if platform == "youtube":
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
                        "youtube": {"player_client": ["mweb", "web_safari"]},
                        "youtubepot-bgutilhttp": {"base_url": BGUTIL_BASE_URL},
                    },
                },
            )
        )
    elif platform == "tiktok":
        # Targets versionados reales (chrome/safari genéricos NO existen en yt-dlp).
        imp = _DEFAULT_IMPERSONATE  # p.ej. chrome131
        imp_android = "chrome131_android" if _DEFAULT_IMPERSONATE else None
        imp_ios = "safari17_2_ios" if _DEFAULT_IMPERSONATE else None

        if imp:
            strategies.append(
                (
                    f"tt-impersonate-{imp}",
                    {
                        "impersonate": imp,
                        "http_headers": {
                            "Referer": "https://www.tiktok.com/",
                            "Origin": "https://www.tiktok.com",
                        },
                    },
                )
            )
        if imp_android:
            strategies.append(
                (
                    "tt-impersonate-android",
                    {
                        "impersonate": imp_android,
                        "http_headers": {"Referer": "https://www.tiktok.com/"},
                    },
                )
            )
        if imp_ios:
            strategies.append(
                (
                    "tt-impersonate-ios",
                    {
                        "impersonate": imp_ios,
                        "http_headers": {"Referer": "https://www.tiktok.com/"},
                    },
                )
            )

        # API móvil con distintos hostnames
        for host in (
            "api16-normal-c-useast1a.tiktokv.com",
            "api16-normal-c-useast2a.tiktokv.com",
            "api19-normal-c-useast1a.tiktokv.com",
            "api22-normal-c-useast1a.tiktokv.com",
        ):
            ov: dict[str, Any] = {
                "extractor_args": {"tiktok": {"api_hostname": host}},
                "http_headers": {
                    "User-Agent": (
                        "com.zhiliaoapp.musically/2023501030 "
                        "(Linux; U; Android 13; en_US; Pixel 7; "
                        "Build/TQ3A.230901.001; Cronet/58.0.2991.0)"
                    ),
                    "Referer": "https://www.tiktok.com/",
                },
            }
            if imp_android or imp:
                ov["impersonate"] = imp_android or imp
            strategies.append((f"tt-api-{host.split('.')[0]}", ov))

        app_ov: dict[str, Any] = {
            "extractor_args": {
                "tiktok": {
                    "app_info": [
                        "trill/38.4.2/2023804020/1180",
                        "musical_ly/38.4.2/2023804020/1233",
                    ]
                }
            },
        }
        if imp:
            app_ov["impersonate"] = imp
        strategies.append(("tt-app-info", app_ov))

        mobile_ov: dict[str, Any] = {
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                "Referer": "https://www.tiktok.com/",
            },
        }
        if imp_ios or imp:
            mobile_ov["impersonate"] = imp_ios or imp
        strategies.append(("tt-mobile-ua", mobile_ov))

        # Último: sin impersonate forzado (por si el target falla)
        strategies.append(("tt-no-impersonate", {"impersonate": None}))
    elif platform == "instagram":
        ig_ov: dict[str, Any] = {
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                "Referer": "https://www.instagram.com/",
                "X-IG-App-ID": "936619743392459",
            },
        }
        if _DEFAULT_IMPERSONATE:
            ig_ov["impersonate"] = _DEFAULT_IMPERSONATE
        strategies.append(("ig-mobile", ig_ov))
        strategies.append(("ig-default", {}))
    elif platform == "twitter":
        x_ov: dict[str, Any] = {
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                "Referer": "https://x.com/",
            },
        }
        if _DEFAULT_IMPERSONATE:
            x_ov["impersonate"] = _DEFAULT_IMPERSONATE
        strategies.append(("x-mobile", x_ov))
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
        if "entries" in info and info["entries"]:
            info = next(e for e in info["entries"] if e)
        filename = ydl.prepare_filename(info)
        filename = _resolve_filename(info, filename)
        return info, filename


def _http_json(url: str, timeout: int = 25) -> dict:
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
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


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
    """Resuelve vm/vt.tiktok.com a URL larga (sin cookies)."""
    if not TIKTOK_SHORT_RE.match(url):
        return url
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.geturl() or url
    except Exception as e:
        logger.warning("No pude expandir short TikTok %s: %s", url, e)
        return url


def _download_tiktok_via_tikwm(url: str, tmp_dir: str) -> tuple[dict, str]:
    """
    Fallback autónomo (sin cookies): API pública de tikwm.com.
    No es sesión de usuario; es un proxy de metadata/CDN.
    """
    expanded = _expand_tiktok_url(url)
    qs = urllib.parse.urlencode({"url": expanded, "hd": "1"})
    api_url = f"https://www.tikwm.com/api/?{qs}"
    logger.info("TikTok fallback TikWM: %s", api_url)
    payload = _http_json(api_url)
    if not isinstance(payload, dict):
        raise RuntimeError("TikWM respuesta inválida")
    if payload.get("code") not in (0, "0", None) and payload.get("code") != 0:
        # algunos devuelven code=0 ok
        if int(payload.get("code", -1) or -1) != 0:
            raise RuntimeError(f"TikWM error: {payload.get('msg') or payload}")

    data = payload.get("data") or {}
    if not data:
        raise RuntimeError(f"TikWM sin data: {payload.get('msg') or payload}")

    # Preferir sin watermark / HD
    play = (
        data.get("hdplay")
        or data.get("play")
        or data.get("wmplay")
        or (data.get("images") or [None])[0]
    )
    if not play or not isinstance(play, str):
        raise RuntimeError("TikWM no devolvió URL de video")

    # slideshow de fotos: no es video
    if data.get("images") and not data.get("play") and not data.get("hdplay"):
        raise RuntimeError("Este TikTok es un carrusel de fotos, no un video")

    vid = str(data.get("id") or TIKTOK_ID_RE.search(expanded) and TIKTOK_ID_RE.search(expanded).group(1) or int(time.time()))
    dest = os.path.join(tmp_dir, f"{vid}.mp4")
    _http_download(play, dest)
    if not os.path.exists(dest) or os.path.getsize(dest) < 1000:
        raise RuntimeError("TikWM descargó un archivo vacío/inválido")

    info = {
        "id": vid,
        "title": data.get("title") or f"tiktok_{vid}",
        "uploader": (data.get("author") or {}).get("unique_id")
        or (data.get("author") or {}).get("nickname")
        or "",
        "webpage_url": expanded,
        "extractor": "tikwm",
    }
    return info, dest


def _clean_tmp(tmp_dir: str) -> None:
    for leftover in os.listdir(tmp_dir):
        path = os.path.join(tmp_dir, leftover)
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def download_video(url: str, tmp_dir: str) -> tuple[dict, str, str]:
    errors: list[str] = []
    platform = _detect_platform(url)
    strategies = _strategies_for(url)
    logger.info(
        "Plataforma=%s impersonate_target=%s estrategias=%s",
        platform,
        _DEFAULT_IMPERSONATE,
        [s[0] for s in strategies],
    )

    # TikTok: TikWM primero (en VPS es lo más fiable sin cookies).
    # Si falla, cae a yt-dlp con impersonate versionado.
    if platform == "tiktok":
        try:
            _clean_tmp(tmp_dir)
            logger.info("Probando TikWM (preferido en VPS)")
            info, filename = _download_tiktok_via_tikwm(url, tmp_dir)
            logger.info("OK con tikwm → %s", filename)
            return info, filename, "tt-tikwm"
        except Exception as e:
            err = f"tt-tikwm: {e}"
            logger.warning("Falló %s", err)
            errors.append(err)

    for name, overrides in strategies:
        opts = _base_ydl_opts(tmp_dir)
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
            elif key == "impersonate":
                if value is None:
                    opts.pop("impersonate", None)
                else:
                    opts["impersonate"] = value
            else:
                opts[key] = value

        # Si no hay curl_cffi, quitar impersonate para no romper
        if opts.get("impersonate") and not _impersonate_available():
            opts.pop("impersonate", None)

        _clean_tmp(tmp_dir)

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
            time.sleep(0.25)
            continue

    joined = " | ".join(errors[-5:])
    raise RuntimeError(f"Todas las estrategias fallaron. Últimos errores: {joined}")


def _friendly_download_error(exc: Exception, url: str) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    platform = _detect_platform(url)

    if "status code 0" in low or "video not available" in low:
        return (
            "❌ TikTok bloqueó la extracción directa (status code 0 = anti-bot).\n"
            "El bot ya reintenta con TLS impersonate + API móvil + TikWM.\n"
            "Si sigue fallando: IP del VPS marcada, o video regional/privado.\n"
            f"Detalle: {msg}"
        )

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
                "El bot reintenta android/ios/tv + PO Token."
            ),
            "tiktok": "TikTok rechazó la descarga anónima (IP VPS o video no público).",
            "instagram": "Instagram suele exigir login para muchos reels. Solo posts públicos abiertos.",
        }.get(platform, "El sitio rechazó la descarga anónima.")
        return f"❌ No se pudo descargar (sin cookies/sesión).\n{extra}\n\nDetalle: {msg}"

    if any(k in low for k in ("unsupported url", "no video formats", "is not a valid url")):
        return f"❌ Enlace no soportado o sin video descargable.\nDetalle: {msg}"

    if "todas las estrategias fallaron" in low:
        return (
            f"❌ No pude bajar el video con ninguna estrategia autónoma "
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
        imp = _DEFAULT_IMPERSONATE or ("NO" if not _impersonate_available() else "sin target")
        await update.message.reply_text(
            "👑 Admin listo.\n"
            "Modo: autónomo (sin cookies / sin sesiones).\n"
            f"TLS impersonate: {imp}\n"
            "TikTok: TikWM primero + yt-dlp.\n"
            "YouTube: android/ios/tv + PO Token.\n"
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
    logger.info(
        "TLS impersonate: available=%s target=%s",
        _impersonate_available(),
        _DEFAULT_IMPERSONATE,
    )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_admin_decision, pattern=r"^(approve|reject)_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado, esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
