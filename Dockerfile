FROM python:3.12-slim

# Instalar ffmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el bot
COPY bot.py .

# Directorio temporal para descargas (en RAM si es posible)
VOLUME ["/tmp"]

CMD ["python3", "-u", "bot.py"]
