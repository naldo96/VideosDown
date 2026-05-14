import os
import re
import json
import asyncio
import tempfile
import logging
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import yt_dlp

# ── Config desde variables de entorno (.env) ──────────────────
BOT_TOKEN = os.environ["8130330805:AAHebma9WyWokLBFnTGfUj9ECW84roBtN00"]
ADMIN_ID  = int(os.environ["501203904"])
DATA_DIR  = Path("/app/data")
DATA_FILE = DATA_DIR / "users.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  GESTIÓN DE USUARIOS
# ══════════════════════════════════════════════

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"approved": [], "pending": [], "banned": []}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_status(user_id: int) -> str:
    d = load_data()
    uid = str(user_id)
    if uid in d["approved"]: return "approved"
    if uid in d["banned"]:   return "banned"
    if uid in d["pending"]:  return "pending"
    return "unknown"

def set_status(user_id: int, status: str):
    d = load_data()
    uid = str(user_id)
    for key in ["approved", "pending", "banned"]:
        if uid in d[key]: d[key].remove(uid)
    if status != "removed":
        d[status].append(uid)
    save_data(d)

def add_pending(user_id: int):
    d = load_data()
    uid = str(user_id)
    if uid not in d["pending"] and uid not in d["approved"] and uid not in d["banned"]:
        d["pending"].append(uid)
        save_data(d)

# ══════════════════════════════════════════════
#  DESCARGA — /tmp montado como tmpfs (solo RAM)
# ══════════════════════════════════════════════

URL_REGEX = re.compile(r"https?://[^\s]+")

def is_url(text: str) -> bool:
    return bool(URL_REGEX.match(text.strip()))

async def download_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("⏳ Obteniendo información del video...")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "max_filesize": 2_000_000_000,
        }
        try:
            loop = asyncio.get_event_loop()
            await msg.edit_text("⬇️ Descargando en máxima calidad disponible...")
            info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))

            files = list(Path(tmpdir).glob("*"))
            if not files:
                await msg.edit_text("❌ No se encontró el archivo.")
                return

            filepath = files[0]
            size_mb  = filepath.stat().st_size / (1024 * 1024)

            if size_mb > 49:
                await msg.edit_text(
                    f"⚠️ El video pesa *{size_mb:.1f} MB* y supera el límite de Telegram (50 MB).\n"
                    "Intenta con un video más corto.", parse_mode="Markdown")
                return

            title    = info.get("title", "video")
            duration = info.get("duration", 0) or 0
            res      = info.get("height", "?")
            ext      = info.get("ext", "mp4")

            caption = (
                f"🎬 *{title}*\n"
                f"📐 Resolución: `{res}p`\n"
                f"⏱ Duración: `{duration//60}:{duration%60:02d}`\n"
                f"💾 Tamaño: `{size_mb:.1f} MB`\n"
                f"🔗 Formato: `{ext.upper()}`"
            )

            await msg.edit_text("📤 Enviando a Telegram...")
            with open(filepath, "rb") as vf:
                await update.message.reply_video(
                    video=vf, caption=caption, parse_mode="Markdown",
                    supports_streaming=True, read_timeout=300, write_timeout=300,
                )
            await msg.delete()
            log.info(f"Enviado a {update.effective_user.id}: {url}")

        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            if "Unsupported URL" in err:
                txt = "❌ URL no compatible. Prueba con YouTube, TikTok, Instagram, Twitter/X, Facebook, Vimeo, etc."
            elif "Private" in err or "age" in err.lower():
                txt = "❌ Video privado o con restricción de edad."
            else:
                txt = f"❌ Error al descargar:\n`{err[:300]}`"
            await msg.edit_text(txt, parse_mode="Markdown")
        except Exception as e:
            await msg.edit_text(f"❌ Error inesperado:\n`{str(e)[:300]}`", parse_mode="Markdown")
            log.error(e)

# ══════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id

    if user_id == ADMIN_ID:
        await update.message.reply_text(
            "👑 *Panel de Administrador*\n\nEnvíame cualquier enlace y lo descargaré.\n\n• `/usuarios` — Gestionar usuarios",
            parse_mode="Markdown")
        return

    status = get_status(user_id)
    if status == "approved":
        await update.message.reply_text("✅ Acceso *activo*. Envíame cualquier enlace de video. 🎬", parse_mode="Markdown")
    elif status == "banned":
        await update.message.reply_text("🚫 Tu acceso ha sido *revocado*.", parse_mode="Markdown")
    elif status == "pending":
        await update.message.reply_text("⏳ Solicitud *pendiente de aprobación*.", parse_mode="Markdown")
    else:
        add_pending(user_id)
        await update.message.reply_text(
            f"👋 Hola *{user.first_name}*!\n\nSolicitud enviada al administrador. Espera la aprobación. ✅",
            parse_mode="Markdown")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aprobar", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("🚫 Banear",  callback_data=f"ban_{user_id}"),
        ]])
        await context.bot.send_message(
            ADMIN_ID,
            f"🔔 *Nueva solicitud*\n\n👤 {user.full_name}\n🆔 `{user_id}`\n📛 @{user.username or 'sin username'}",
            parse_mode="Markdown", reply_markup=kb)

async def cmd_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Solo el administrador puede usar este comando.")
        return

    d = load_data()

    async def info(uid):
        try:
            c = await context.bot.get_chat(int(uid))
            return c.full_name, f"@{c.username}" if c.username else ""
        except:
            return "Desconocido", ""

    text = "👥 *Panel de Usuarios*\n\n"
    text += f"✅ *Aprobados* ({len(d['approved'])}):\n"
    for uid in d["approved"]:
        n, u = await info(uid)
        text += f"  • `{uid}` — {n} {u}\n"

    text += f"\n⏳ *Pendientes* ({len(d['pending'])}):\n"
    for uid in d["pending"]:
        n, u = await info(uid)
        text += f"  • `{uid}` — {n} {u}\n"

    text += f"\n🚫 *Baneados* ({len(d['banned'])}):\n"
    for uid in d["banned"]:
        n, _ = await info(uid)
        text += f"  • `{uid}` — {n}\n"

    if not any([d["approved"], d["pending"], d["banned"]]):
        text += "_Sin usuarios registrados._\n"

    buttons = []
    for uid in d["pending"]:
        n, _ = await info(uid)
        buttons.append([
            InlineKeyboardButton(f"✅ {n[:18]}", callback_data=f"approve_{uid}"),
            InlineKeyboardButton("🚫 Banear",    callback_data=f"ban_{uid}"),
        ])
    for uid in d["approved"]:
        n, _ = await info(uid)
        buttons.append([InlineKeyboardButton(f"🚫 Revocar — {n[:15]}", callback_data=f"ban_{uid}")])
    for uid in d["banned"]:
        buttons.append([InlineKeyboardButton(f"♻️ Restaurar {uid}", callback_data=f"approve_{uid}")])

    await update.message.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return

    action, uid = query.data.split("_", 1)
    uid_int = int(uid)

    if action == "approve":
        set_status(uid_int, "approved")
        await query.edit_message_text(f"✅ Usuario `{uid}` *aprobado*.", parse_mode="Markdown")
        try: await context.bot.send_message(uid_int, "✅ ¡Acceso *aprobado*! Envíame cualquier enlace. 🎬", parse_mode="Markdown")
        except: pass
    elif action == "ban":
        set_status(uid_int, "banned")
        await query.edit_message_text(f"🚫 Usuario `{uid}` *bloqueado*.", parse_mode="Markdown")
        try: await context.bot.send_message(uid_int, "🚫 Tu acceso ha sido *revocado*.", parse_mode="Markdown")
        except: pass

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text or ""

    if user_id != ADMIN_ID:
        status = get_status(user_id)
        if status == "banned":
            await update.message.reply_text("🚫 Tu acceso ha sido revocado."); return
        if status != "approved":
            add_pending(user_id)
            await update.message.reply_text("⏳ Sin acceso. Usa /start para solicitarlo."); return

    if is_url(text.strip()):
        await download_and_send(update, context, text.strip())
    else:
        await update.message.reply_text("🔗 Envíame un enlace de video válido.\n\nCompatible con YouTube, TikTok, Instagram, Twitter/X, Facebook, Vimeo y más de 1000 sitios.")

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("usuarios", cmd_usuarios))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    log.info("🤖 VideoBot iniciado.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
