# Telegram Video Bot (modo autónomo)

Descarga videos (YouTube, TikTok, Instagram, X, Facebook, etc.) y los reenvía por Telegram.

**Sin cookies. Sin sesiones de navegador. Sin login manual.**

Aprobación de usuarios con MongoDB. Descargas en RAM (`tmpfs`) → no deja archivos en el VPS.

## Credenciales (hardcodeadas en `bot.py`)

| Clave | Valor |
|---|---|
| `BOT_TOKEN` | `8919924327:AAHMrSgNVRf-d4vzvu4Lzy9mPjhgGYDY1OM` |
| `ADMIN_CHAT_ID` | `501203904` |
| `MONGO_URI` | `mongodb+srv://BotiCAM:Tito1996@cluster0.zxzdojv.mongodb.net/` |

## Cómo es autónomo

| Plataforma | Estrategia (cascada, sin cookies) |
|---|---|
| **YouTube** | clientes `android` → `ios` → `tv` → `mweb` + **PO Token** (`bgutil-provider`) |
| **TikTok** | UA móvil / app + reintentos |
| **Instagram** | solo posts **públicos** abiertos (sin login no hay magia) |
| **X / FB / otros** | UA móvil + fallback desktop |

Si una estrategia falla, prueba la siguiente automáticamente.

## Archivos

```
telegram-video-bot/
├── bot.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
└── README.md
```

No hay `cookies.txt`. No hace falta montar nada.

## Despliegue en el VPS

```bash
# Sube la carpeta
scp -r telegram-video-bot user@TU_VPS:~/

# En el VPS
cd ~/telegram-video-bot
docker compose up -d --build
docker compose logs -f bot
```

Logs esperados:

```
Modo AUTÓNOMO: sin cookies / sin sesiones de navegador
PO Token provider: http://bgutil-provider:4416
Bot iniciado, esperando mensajes...
```

## Uso

1. `/start` en Telegram  
2. Admin (`501203904`) acepta/rechaza  
3. Usuarios aprobados mandan un enlace → reciben el video  

## Límites reales (sin cookies)

- **YouTube público**: suele ir bien con android/ios + PO Token. Age-restricted / privados: no.
- **TikTok público**: suele ir. Algunos regionales bloquean IP de VPS.
- **Instagram**: muchos reels/posts exigen login → pueden fallar. Es limitación del sitio, no del bot.
- **Telegram Bot API**: máx **50 MB** por video.

## Comandos útiles

```bash
docker compose ps
docker compose logs -f bot
docker compose logs -f bgutil-provider
docker compose restart bot
docker compose down
docker compose up -d --build
```
