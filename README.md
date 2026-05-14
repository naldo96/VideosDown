# 🤖 VideoBot — Bot de Telegram para descargar videos

Descarga videos en máxima calidad desde YouTube, TikTok, Instagram, Twitter/X,
Facebook, Vimeo y más de 1000 sitios. Nada se guarda en disco: las descargas
ocurren en RAM (tmpfs) y se envían directo a Telegram.

---

## 📁 Estructura de archivos

```
videobot/
├── bot.py               # Código principal del bot
├── Dockerfile           # Imagen Docker
├── docker-compose.yml   # Orquestación
├── requirements.txt     # Dependencias Python
├── .env.example         # Plantilla de variables de entorno
└── data/                # Creado automáticamente (users.json)
```

---

## 🚀 Despliegue rápido

### 1. Configurar variables de entorno

```bash
cp .env.example .env
nano .env
```

Rellena:
```
BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_ID=123456789
```

**¿Cómo obtener cada valor?**
- `BOT_TOKEN` → Habla con [@BotFather](https://t.me/BotFather), crea un bot con `/newbot`
- `ADMIN_ID`  → Habla con [@userinfobot](https://t.me/userinfobot), te da tu ID numérico

---

### 2. Construir y arrancar

```bash
docker compose up -d --build
```

### 3. Ver logs en tiempo real

```bash
docker compose logs -f
```

### 4. Detener el bot

```bash
docker compose down
```

---

## 🔄 Actualizar yt-dlp (cuando fallen descargas)

```bash
docker compose build --no-cache
docker compose up -d
```

O sin reconstruir:

```bash
docker compose exec videobot pip install -U yt-dlp
```

---

## 📋 Comandos del bot

| Comando / Acción | Quién | Descripción |
|---|---|---|
| `/start` | Usuarios | Solicitar acceso |
| `/usuarios` | Solo admin | Ver y gestionar todos los usuarios |
| Pegar un enlace | Usuarios aprobados | Descarga el video en máxima calidad |
| Botones inline | Solo admin | Aprobar / banear desde el chat |

---

## ⚙️ Cómo funciona el almacenamiento

```
/tmp  →  montado como tmpfs (RAM, 2 GB máx)
         Los videos se descargan aquí y se borran solos al enviarse.
         NUNCA tocan el disco del VPS.

/app/data/users.json  →  montado en ./data (sí persiste en disco)
                          Solo guarda IDs de Telegram aprobados/baneados.
```

---

## ⚠️ Límite de Telegram

Los bots de Telegram tienen un límite de **50 MB por archivo**.
Videos muy largos o en 4K pueden superarlo. En ese caso el bot avisa al usuario.
