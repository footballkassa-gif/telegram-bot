import os
import logging
import asyncio
import datetime
import subprocess
import tempfile
import re
from urllib.parse import quote
from pathlib import Path

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

# ─── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "8864912966:AAG1aqrxebT1qvXWs2v5YYo2mvi9cSzMrN8")
ALLOWED_CHANNELS    = ["@myposts_channel"]
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "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")
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct:free")
OPENROUTER_URL      = "https://openrouter.ai/api/v1/chat/completions"

# ─── Video / dubbing config ──────────────────────────────────────────────────
FFMPEG_PATH        = r"C:\ffmpeg\bin\ffmpeg.exe"
VIDEOS_DIR         = Path(os.path.expanduser("~")) / "Desktop" / "videos"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
TIKTOK_RE          = re.compile(r"https?://(vm\.|www\.)?tiktok\.com/\S+")

# ─── Weather config ─────────────────────────────────────────────────────────
WEATHER_CITY       = "Казань"
WEATHER_LAT        = 55.7887
WEATHER_LON        = 49.1221
WEATHER_HOUR       = 8   # во сколько утром (по местному времени сервера)
WEATHER_MINUTE     = 0

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Stores ─────────────────────────────────────────────────────────────────
drafts: dict[int, dict] = {}
chat_history: dict[int, list] = {}
MAX_HISTORY = 10


# ════════════════════════════════════════════════════════════════════════════
# Ollama helpers
# ════════════════════════════════════════════════════════════════════════════

async def ollama_chat(messages: list) -> str:
    """Генерация через OpenRouter API (совместим с OpenAI формату)."""
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/myposts_channel",
        "X-Title": "Telegram Post Bot",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def generate_post(idea: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "Ты профессиональный копирайтер для Telegram-каналов. "
                "Пиши развёрнутые посты 200-400 слов. Структура:\n"
                "• Яркий заголовок с эмодзи\n"
                "• Цепляющий первый абзац\n"
                "• 2-3 содержательных абзаца\n"
                "• Вывод или призыв к действию\n"
                "• 5-7 хэштегов\n\n"
                "Пиши на языке пользователя. Верни ТОЛЬКО текст поста."
            ),
        },
        {"role": "user", "content": idea},
    ]
    return await ollama_chat(messages)


async def generate_image_prompt(post_text: str) -> str:
    """Генерирует короткий английский промпт для картинки по тексту поста."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are an image prompt generator. "
                "Based on the post text, write a short English image prompt (max 20 words) "
                "for Stable Diffusion. Focus on visual elements, mood, and style. "
                "Return ONLY the prompt, nothing else."
            ),
        },
        {"role": "user", "content": f"Post text:\n{post_text}"},
    ]
    return await ollama_chat(messages)


async def generate_image(prompt: str) -> bytes:
    """Генерирует картинку через Pollinations.ai и возвращает байты."""
    encoded = quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def chat_reply(user_id: int, user_message: str) -> str:
    history = chat_history.setdefault(user_id, [])
    system = {
        "role": "system",
        "content": (
            "Ты умный и дружелюбный помощник в Telegram. "
            "Отвечай развёрнуто и по делу. "
            "Если просят создать пост — скажи написать /post. "
            "Общайся на языке пользователя."
        ),
    }
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY * 2:
        chat_history[user_id] = history[-(MAX_HISTORY * 2):]
    reply = await ollama_chat([system] + chat_history[user_id])
    chat_history[user_id].append({"role": "assistant", "content": reply})
    return reply


# ─── Users who want morning weather {user_id} ────────────────────────────────
weather_users: set[int] = set()


async def get_weather() -> str:
    """Получаем погоду через Open-Meteo (бесплатно, без ключа)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&current=temperature_2m,weathercode,windspeed_10m,relative_humidity_2m"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&timezone=Europe%2FMoscow&forecast_days=1"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    cur = data["current"]
    daily = data["daily"]
    temp     = cur["temperature_2m"]
    wind     = cur["windspeed_10m"]
    humidity = cur["relative_humidity_2m"]
    code     = cur["weathercode"]
    t_max    = daily["temperature_2m_max"][0]
    t_min    = daily["temperature_2m_min"][0]
    precip   = daily["precipitation_sum"][0]

    # Описание погоды по WMO коду
    weather_desc = {
        0: "☀️ Ясно",
        1: "🌤️ Преимущественно ясно", 2: "⛅ Переменная облачность", 3: "☁️ Пасмурно",
        45: "🌫️ Туман", 48: "🌫️ Изморозь",
        51: "🌦️ Лёгкая морось", 53: "🌦️ Морось", 55: "🌧️ Сильная морось",
        61: "🌧️ Небольшой дождь", 63: "🌧️ Дождь", 65: "🌧️ Сильный дождь",
        71: "🌨️ Небольшой снег", 73: "❄️ Снег", 75: "❄️ Сильный снег",
        80: "🌦️ Ливень", 81: "🌧️ Сильный ливень", 82: "⛈️ Очень сильный ливень",
        95: "⛈️ Гроза", 96: "⛈️ Гроза с градом", 99: "⛈️ Сильная гроза с градом",
    }.get(code, "🌡️ Переменно")

    now = datetime.datetime.now()
    return (
        f"🌅 *Доброе утро! Погода в {WEATHER_CITY}*\n"
        f"📅 {now.strftime('%d.%m.%Y')}\n\n"
        f"{weather_desc}\n"
        f"🌡️ Сейчас: *{temp}°C*\n"
        f"⬆️ Макс: {t_max}°C  ⬇️ Мин: {t_min}°C\n"
        f"💨 Ветер: {wind} км/ч\n"
        f"💧 Влажность: {humidity}%\n"
        f"🌧️ Осадки: {precip} мм\n\n"
        f"Хорошего дня! 😊"
    )


async def get_rates() -> str:
    """Курсы валют через ЦБ РФ + крипта через CoinGecko (бесплатно, без ключа)."""
    result_lines = ["📈 *Курсы валют и крипта:*\n"]

    # ── Валюты через ЦБ РФ ──────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://www.cbr-xml-daily.ru/daily_json.js")
            data = r.json()
        valutes = data.get("Valute", {})
        usd = valutes.get("USD", {})
        eur = valutes.get("EUR", {})
        cny = valutes.get("CNY", {})

        def arrow(val):
            prev = val.get("Previous", val.get("Value", 0))
            cur  = val.get("Value", 0)
            return "🔺" if cur > prev else "🔻" if cur < prev else "➡️"

        if usd:
            result_lines.append(f"🇺🇸 USD: *{usd['Value']:.2f} ₽* {arrow(usd)}")
        if eur:
            result_lines.append(f"🇪🇺 EUR: *{eur['Value']:.2f} ₽* {arrow(eur)}")
        if cny:
            result_lines.append(f"🇨🇳 CNY: *{cny['Value']:.2f} ₽* {arrow(cny)}")
    except Exception as e:
        result_lines.append(f"⚠️ Курсы ЦБ недоступны: {e}")

    result_lines.append("")

    # ── Крипта через CoinGecko ───────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin,ethereum,toncoin&vs_currencies=usd&include_24hr_change=true"
            )
            crypto = r.json()

        def crypto_arrow(change):
            return "🔺" if change > 0 else "🔻"

        btc = crypto.get("bitcoin", {})
        eth = crypto.get("ethereum", {})
        ton = crypto.get("toncoin", {})

        if btc:
            ch = btc.get("usd_24h_change", 0)
            result_lines.append(f"₿ BTC: *${btc['usd']:,.0f}* {crypto_arrow(ch)} {ch:+.1f}%")
        if eth:
            ch = eth.get("usd_24h_change", 0)
            result_lines.append(f"⟠ ETH: *${eth['usd']:,.0f}* {crypto_arrow(ch)} {ch:+.1f}%")
        if ton:
            ch = ton.get("usd_24h_change", 0)
            result_lines.append(f"💎 TON: *${ton['usd']:.2f}* {crypto_arrow(ch)} {ch:+.1f}%")
    except Exception as e:
        result_lines.append(f"⚠️ Крипта недоступна: {e}")

    return "\n".join(result_lines)


async def morning_weather_job(app):
    """Фоновая задача — отправляет погоду + курсы каждое утро."""
    while True:
        now = datetime.datetime.now()
        target = now.replace(hour=WEATHER_HOUR, minute=WEATHER_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"Next morning report in {wait_seconds/3600:.1f}h ({target.strftime('%H:%M')})")
        await asyncio.sleep(wait_seconds)

        if weather_users:
            try:
                weather_text = await get_weather()
                rates_text   = await get_rates()
                text = weather_text + "\n\n" + rates_text
            except Exception as e:
                logger.error(f"Morning report error: {e}")
                text = f"⚠️ Не удалось получить данные: {e}"
            for uid in list(weather_users):
                try:
                    await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Send to {uid} error: {e}")


async def cmd_weather(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показать погоду + курсы прямо сейчас."""
    thinking = await update.message.reply_text("⏳ Получаю данные…")
    try:
        weather_text = await get_weather()
        rates_text   = await get_rates()
        await thinking.edit_text(weather_text + "\n\n" + rates_text, parse_mode="Markdown")
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: {e}")


async def cmd_rates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показать только курсы валют и крипту."""
    thinking = await update.message.reply_text("⏳ Получаю курсы…")
    try:
        text = await get_rates()
        await thinking.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: {e}")


async def cmd_weather_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подписаться на утреннюю погоду."""
    weather_users.add(update.effective_user.id)
    await update.message.reply_text(
        f"✅ Подписка оформлена!\n"
        f"Каждое утро в *{WEATHER_HOUR:02d}:{WEATHER_MINUTE:02d}* буду присылать:\n"
        f"🌤️ Погоду в {WEATHER_CITY}\n"
        f"📈 Курсы USD, EUR, CNY, BTC, ETH, TON",
        parse_mode="Markdown",
    )


async def cmd_weather_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отписаться от утренней погоды."""
    weather_users.discard(update.effective_user.id)
    await update.message.reply_text("❌ Отписан от утренней погоды.")


# ════════════════════════════════════════════════════════════════════════════
# Video dubbing helpers
# ════════════════════════════════════════════════════════════════════════════

async def download_tiktok(url: str, out_dir: Path) -> Path:
    """Скачивает TikTok видео через yt-dlp, возвращает путь к файлу."""
    import yt_dlp
    out_template = str(out_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    loop = asyncio.get_event_loop()
    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # yt-dlp may change extension
            p = Path(filename)
            if not p.exists():
                for ext in ("mp4", "webm", "mkv"):
                    alt = p.with_suffix(f".{ext}")
                    if alt.exists():
                        return alt
            return p
    return await loop.run_in_executor(None, _download)


async def transcribe_audio(audio_path: Path) -> str:
    """Распознаёт речь через Whisper, возвращает текст."""
    import whisper
    loop = asyncio.get_event_loop()
    def _transcribe():
        model = whisper.load_model("base")
        result = model.transcribe(str(audio_path), fp16=False)
        return result["text"].strip()
    return await loop.run_in_executor(None, _transcribe)


async def translate_text(text: str) -> str:
    """Переводит текст на русский через Ollama."""
    messages = [
        {
            "role": "system",
            "content": (
                "Ты переводчик. Переведи текст на русский язык. "
                "Верни ТОЛЬКО перевод, без пояснений."
            ),
        },
        {"role": "user", "content": text},
    ]
    return await ollama_chat(messages)


async def synthesize_speech(text: str, out_path: Path) -> Path:
    """Синтезирует речь через edge-tts (Microsoft голоса), сохраняет mp3."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice="ru-RU-DmitryNeural")
    await communicate.save(str(out_path))
    return out_path


def _run_ffmpeg(*args):
    """Запускает ffmpeg с указанными аргументами."""
    cmd = [FFMPEG_PATH] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error:\n{result.stderr[-500:]}")
    return result


async def extract_audio(video_path: Path, audio_path: Path) -> Path:
    """Извлекает аудио из видео."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_ffmpeg,
        "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path)
    )
    return audio_path


async def mix_audio_with_video(video_path: Path, new_audio_path: Path, out_path: Path) -> Path:
    """Накладывает новый голос на видео, убирает оригинальный звук."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_ffmpeg,
        "-y",
        "-i", str(video_path),
        "-i", str(new_audio_path),
        "-map", "0:v:0",      # видео из первого файла
        "-map", "1:a:0",      # аудио из второго файла
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(out_path)
    )
    return out_path


async def process_tiktok_dub(url: str, status_msg) -> Path:
    """
    Полный пайплайн переозвучки TikTok:
    скачать → извлечь аудио → транскрибировать → перевести → озвучить → смикшировать
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        await status_msg.edit_text("📥 Скачиваю видео с TikTok…")
        video_path = await download_tiktok(url, tmp_dir)

        await status_msg.edit_text("🎙️ Извлекаю и распознаю речь (Whisper)…")
        audio_path = tmp_dir / "audio.wav"
        await extract_audio(video_path, audio_path)
        original_text = await transcribe_audio(audio_path)
        logger.info(f"Transcribed: {original_text[:100]}")

        await status_msg.edit_text(
            f"📝 Распознано:\n_{original_text[:200]}_\n\n🌍 Перевожу на русский…",
            parse_mode="Markdown"
        )
        russian_text = await translate_text(original_text)
        logger.info(f"Translated: {russian_text[:100]}")

        await status_msg.edit_text(
            f"🔊 Озвучиваю русским голосом…\n\n_{russian_text[:200]}_",
            parse_mode="Markdown"
        )
        tts_path = tmp_dir / "tts.mp3"
        await synthesize_speech(russian_text, tts_path)

        await status_msg.edit_text("🎬 Накладываю голос на видео…")
        video_name = video_path.stem
        out_path = VIDEOS_DIR / f"{video_name}_dubbed.mp4"
        await mix_audio_with_video(video_path, tts_path, out_path)

    return out_path


async def cmd_dub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Команда /dub <ссылка> — скачать и переозвучить TikTok."""
    url = " ".join(ctx.args).strip() if ctx.args else ""
    if not url:
        await update.message.reply_text(
            "🎬 Отправь ссылку на TikTok прямо в чат, или:\n"
            "`/dub https://vm.tiktok.com/...`",
            parse_mode="Markdown"
        )
        return
    await _handle_dub(update, url)


async def _handle_dub(update: Update, url: str):
    status = await update.message.reply_text("⚙️ Начинаю переозвучку…")
    try:
        out_path = await process_tiktok_dub(url, status)
        await status.edit_text("📤 Отправляю видео…")
        with open(out_path, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption="✅ Готово! Видео переозвучено на русский 🎙️",
                supports_streaming=True,
            )
        await status.delete()
    except Exception as e:
        logger.exception("Dub error")
        await status.edit_text(
            f"❌ Ошибка обработки видео:\n`{e}`\n\n"
            "Проверь что:\n"
            "• Ссылка рабочая (конкретное видео, не главная страница)\n"
            "• ffmpeg установлен в C:\\ffmpeg\n"
            "• Видео не слишком длинное (до 5 мин)",
            parse_mode="Markdown"
        )


async def check_ollama() -> bool:
    """Проверяем доступность OpenRouter."""
    try:
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
            return r.status_code == 200
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# Keyboards
# ════════════════════════════════════════════════════════════════════════════

def post_preview_keyboard(has_image: bool = False) -> InlineKeyboardMarkup:
    img_btn = "🖼️ Новая картинка" if has_image else "🎨 Сгенерировать картинку"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
            InlineKeyboardButton("✏️ Переписать",  callback_data="rewrite"),
        ],
        [
            InlineKeyboardButton("📝 Короче",  callback_data="shorter"),
            InlineKeyboardButton("📄 Длиннее", callback_data="longer"),
        ],
        [InlineKeyboardButton(img_btn, callback_data="gen_image")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def channel_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(ch, callback_data=f"ch:{ch}")] for ch in ALLOWED_CHANNELS]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def publish_confirm_keyboard(with_image: bool) -> InlineKeyboardMarkup:
    rows = []
    if with_image:
        rows.append([
            InlineKeyboardButton("🖼️ С картинкой", callback_data="do_publish_img"),
            InlineKeyboardButton("📝 Без картинки", callback_data="do_publish"),
        ])
    else:
        rows.append([InlineKeyboardButton("✅ Опубликовать", callback_data="do_publish")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_draft")])
    return InlineKeyboardMarkup(rows)


# ════════════════════════════════════════════════════════════════════════════
# Command handlers
# ════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await check_ollama()
    status = f"✅ OpenRouter ({OPENROUTER_MODEL}) готов" if ok else f"⚠️ OpenRouter недоступен! Проверь API ключ."
    await update.message.reply_text(
        f"👋 *Привет! Я бот для Telegram-каналов.*\n\n"
        f"Умею:\n"
        f"• 📝 Писать большие посты — /post\n"
        f"• 🎨 Генерировать картинки к постам\n"
        f"• 🎬 Переозвучка TikTok — просто скинь ссылку или /dub\n"
        f"• 🌤️ Утренняя погода в Казани — /weather\n"
        f"• 📈 Курсы валют и крипта — /rates\n"
        f"• 💬 Отвечать на вопросы\n"
        f"• 📢 Публиковать в канал\n\n"
        f"📢 Канал: {', '.join(ALLOWED_CHANNELS)}\n"
        f"🧠 Модель: {OPENROUTER_MODEL}\n"
        f"{status}\n\n"
        f"Просто напиши идею поста или скинь ссылку на TikTok!",
        parse_mode="Markdown",
    )


async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    idea = " ".join(ctx.args) if ctx.args else None
    if not idea:
        await update.message.reply_text(
            "📝 Напиши идею:\n`/post про утренний кофе`",
            parse_mode="Markdown",
        )
        return
    await _create_post(update, user_id, idea)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    drafts.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Черновик удалён.")


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_history.pop(update.effective_user.id, None)
    await update.message.reply_text("🧹 История чата очищена.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = await check_ollama()
    msg = f"✅ OpenRouter работает, модель `{OPENROUTER_MODEL}`." if ok else "❌ OpenRouter недоступен. Проверь API ключ."
    await update.message.reply_text(msg, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════════════════
# Core logic
# ════════════════════════════════════════════════════════════════════════════

async def _create_post(update: Update, user_id: int, idea: str):
    thinking = await update.message.reply_text("⏳ Пишу пост через OpenRouter…")
    try:
        post = await generate_post(idea)
    except httpx.ConnectError:
        await thinking.edit_text("❌ Ollama не запущен!\n\nЗапусти: `ollama serve`", parse_mode="Markdown")
        return
    except Exception as e:
        logger.exception("Generate error")
        await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    drafts[user_id] = {"text": post, "channel": None, "idea": idea, "image": None}
    await thinking.edit_text(
        f"📝 *Черновик поста:*\n\n{post}",
        parse_mode="Markdown",
        reply_markup=post_preview_keyboard(has_image=False),
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ── TikTok ссылка ────────────────────────────────────────────────────────
    tiktok_match = TIKTOK_RE.search(text)
    if tiktok_match:
        await _handle_dub(update, tiktok_match.group(0))
        return

    post_keywords = ["пост", "напиши пост", "создай пост", "сделай пост", "post about", "write a post"]
    if any(kw in text.lower() for kw in post_keywords):
        await _create_post(update, user_id, text)
        return

    thinking = await update.message.reply_text("💭 Думаю…")
    try:
        reply = await chat_reply(user_id, text)
    except httpx.ConnectError:
        await thinking.edit_text("❌ OpenRouter недоступен.", parse_mode="Markdown")
        return
    except Exception as e:
        logger.exception("Chat error")
        await thinking.edit_text(f"❌ Ошибка: {e}")
        return
    await thinking.edit_text(reply)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ Голосовые не поддерживаются. Напиши текстом!")


# ════════════════════════════════════════════════════════════════════════════
# Callback handler
# ════════════════════════════════════════════════════════════════════════════

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "cancel":
        drafts.pop(user_id, None)
        await query.edit_message_text("❌ Отменено.")
        return

    # ── Генерация картинки ──────────────────────────────────────────────────
    if data == "gen_image":
        draft = drafts.get(user_id)
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        await query.edit_message_text("🎨 Генерирую картинку через Pollinations.ai…\n⏳ ~15-30 секунд")
        try:
            img_prompt = await generate_image_prompt(draft["text"])
            logger.info(f"Image prompt: {img_prompt}")
            img_bytes = await generate_image(img_prompt)
            drafts[user_id]["image"] = img_bytes
            drafts[user_id]["img_prompt"] = img_prompt
        except Exception as e:
            logger.exception("Image gen error")
            await query.edit_message_text(
                f"📝 *Черновик поста:*\n\n{draft['text']}\n\n❌ Ошибка картинки: {e}",
                parse_mode="Markdown",
                reply_markup=post_preview_keyboard(has_image=False),
            )
            return

        # Отправляем картинку + текст поста
        await query.message.reply_photo(
            photo=img_bytes,
            caption=f"🎨 Промпт: `{img_prompt}`",
            parse_mode="Markdown",
        )
        await query.edit_message_text(
            f"📝 *Черновик поста:*\n\n{draft['text']}\n\n✅ Картинка готова!",
            parse_mode="Markdown",
            reply_markup=post_preview_keyboard(has_image=True),
        )
        return

    # ── Редактирование поста ────────────────────────────────────────────────
    if data in ("rewrite", "shorter", "longer"):
        draft = drafts.get(user_id)
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        if data == "rewrite":
            prompt, label = draft["idea"], "Переписываю"
        elif data == "shorter":
            prompt, label = f"Сократи до 100-150 слов:\n\n{draft['text']}", "Сокращаю"
        else:
            prompt, label = f"Расширь до 500+ слов:\n\n{draft['text']}", "Расширяю"

        await query.edit_message_text(f"⏳ {label}…")
        try:
            post = await generate_post(prompt)
        except httpx.ConnectError:
            await query.edit_message_text("❌ OpenRouter недоступен.", parse_mode="Markdown")
            return
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
            return

        drafts[user_id]["text"] = post
        drafts[user_id]["image"] = None  # сбрасываем картинку при редактировании
        await query.edit_message_text(
            f"📝 *Новый вариант:*\n\n{post}",
            parse_mode="Markdown",
            reply_markup=post_preview_keyboard(has_image=False),
        )
        return

    # ── Публикация ──────────────────────────────────────────────────────────
    if data == "publish":
        draft = drafts.get(user_id, {})
        has_image = draft.get("image") is not None
        if len(ALLOWED_CHANNELS) == 1:
            drafts.setdefault(user_id, {})["channel"] = ALLOWED_CHANNELS[0]
            await query.edit_message_text(
                f"📢 Публикую в {ALLOWED_CHANNELS[0]}...\nС картинкой или без?" if has_image else f"📢 Публикую в {ALLOWED_CHANNELS[0]}...",
                reply_markup=publish_confirm_keyboard(has_image),
            )
        else:
            await query.edit_message_text("📢 Выбери канал:", reply_markup=channel_keyboard())
        return

    if data == "back_to_draft":
        draft = drafts.get(user_id, {})
        has_image = draft.get("image") is not None
        await query.edit_message_text(
            f"📝 *Черновик поста:*\n\n{draft.get('text', '')}",
            parse_mode="Markdown",
            reply_markup=post_preview_keyboard(has_image=has_image),
        )
        return

    if data.startswith("ch:"):
        channel = data[3:]
        drafts.setdefault(user_id, {})["channel"] = channel
        draft = drafts.get(user_id, {})
        has_image = draft.get("image") is not None
        await query.edit_message_text(
            f"📢 Канал: {channel}",
            reply_markup=publish_confirm_keyboard(has_image),
        )
        return

    if data in ("do_publish", "do_publish_img"):
        await _do_publish(query, user_id, ctx, with_image=(data == "do_publish_img"))


async def _do_publish(query, user_id: int, ctx: ContextTypes.DEFAULT_TYPE, with_image: bool = False):
    draft = drafts.get(user_id)
    if not draft or not draft.get("text"):
        await query.edit_message_text("❌ Черновик не найден.")
        return
    channel = draft.get("channel")
    if not channel:
        await query.edit_message_text("❌ Канал не выбран.")
        return
    try:
        if with_image and draft.get("image"):
            await ctx.bot.send_photo(
                chat_id=channel,
                photo=draft["image"],
                caption=draft["text"],
            )
        else:
            await ctx.bot.send_message(chat_id=channel, text=draft["text"])
        drafts.pop(user_id, None)
        await query.edit_message_text(f"✅ Пост опубликован в {channel}!" + (" 🖼️" if with_image else ""))
    except Exception as e:
        logger.exception("Publish error")
        await query.edit_message_text(
            f"❌ Ошибка публикации:\n`{e}`\n\nУбедись что бот — администратор канала.",
            parse_mode="Markdown",
        )


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("post",        cmd_post))
    app.add_handler(CommandHandler("dub",         cmd_dub))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("cancel",      cmd_cancel))
    app.add_handler(CommandHandler("clear",       cmd_clear))
    app.add_handler(CommandHandler("weather",     cmd_weather))
    app.add_handler(CommandHandler("weather_on",  cmd_weather_on))
    app.add_handler(CommandHandler("weather_off", cmd_weather_off))
    app.add_handler(CommandHandler("rates",       cmd_rates))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(callback_handler))

    async def on_startup(app):
        asyncio.ensure_future(morning_weather_job(app))

    app.post_init = on_startup

    logger.info(f"Bot started | model: {OLLAMA_MODEL} | channels: {ALLOWED_CHANNELS}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
