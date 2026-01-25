import os
import re
import logging
import time
import asyncio
import sys
import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- DB ---
def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def setup_database():
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id VARCHAR(255) NOT NULL,
                reminder_id VARCHAR(255) UNIQUE NOT NULL,
                url TEXT NOT NULL,
                max_price REAL NOT NULL,
                condition VARCHAR(255) NOT NULL,
                min_battery INTEGER NOT NULL,
                frequency_seconds INTEGER NOT NULL,
                last_checked BIGINT NOT NULL,
                device_name TEXT
            );
        """)
        cur.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS device_name TEXT;")
        conn.commit()
    conn.close()

# --- Obtener nombre del producto ---
def get_device_name(url: str):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        h1 = soup.find("h1", class_="my-3")
        if h1:
            return h1.get_text(strip=True)

        a = soup.find("a", href=re.compile(r"^/listings/"))
        if a:
            return a.get_text(strip=True)

        return "Producto Desconocido"
    except Exception as e:
        logger.error(f"Error obteniendo nombre: {e}")
        return "Producto Desconocido"

# --- Scraper ---
def scrape_swappa(url, max_price, desired_condition, min_battery, device_name):
    logger.info(f"Iniciando búsqueda para {device_name}")
    driver = None
    encontrados = []
    vistos = set()

    try:
        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--single-process")

        driver = uc.Chrome(options=options)

        for page in range(1, 4):
            page_url = url if page == 1 else f"{url}&page={page}"
            logger.info(f"Página {page}: {page_url}")
            driver.get(page_url)

            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.listing_card"))
                )
            except Exception:
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            listings = soup.select("div.listing_card")

            for card in listings:
                try:
                    link_tag = card.find("a", href=re.compile(r"^/listing/view/"))
                    if not link_tag:
                        continue
                    link = "https://swappa.com" + link_tag["href"]
                    if link in vistos:
                        continue
                    vistos.add(link)

                    price_tag = card.find("span", class_="price")
                    if not price_tag:
                        continue
                    precio = float(price_tag.text.replace("$", "").strip())

                    seller_tag = card.select_one("div.seller_name")
                    vendedor = seller_tag.get_text(strip=True) if seller_tag else "N/A"

                    estado = "N/A"
                    bateria = None
                    almacenamiento = "N/A"
                    color = "N/A"

                    attrs = card.select("span.attr")
                    for attr in attrs:
                        text = attr.get_text(strip=True)

                        if text.lower() in ["mint", "good", "fair", "used", "new"]:
                            estado = text

                        elif "%" in text:
                            m = re.search(r"(\d+)%", text)
                            if m:
                                bateria = int(m.group(1))

                        elif re.search(r"\d+\s*(GB|TB)", text, re.I):
                            almacenamiento = text

                        elif attr.find("i", {"aria-label": "Color"}):
                            spans = attr.find_all("span")
                            if spans:
                                color = spans[-1].get_text(strip=True)

                    if min_battery > 0 and bateria is not None:
                        cumple_bateria = bateria >= min_battery
                    else:
                        cumple_bateria = True

                    if (
                        estado != "N/A"
                        and precio <= max_price
                        and estado.lower() == desired_condition.lower()
                        and cumple_bateria
                    ):
                        encontrados.append({
                            "precio": precio,
                            "estado": estado,
                            "bateria": bateria,
                            "almacenamiento": almacenamiento,
                            "color": color,
                            "vendedor": vendedor,
                            "link": link
                        })

                except Exception:
                    continue

        if not encontrados:
            return None

        msg = f"<b>🔔 ¡Alerta de Swappa! Se encontraron {len(encontrados)} ofertas de {device_name}:</b>\n\n"
        for d in encontrados:
            msg += f"📱 <b>Precio: ${d['precio']}</b>\n"
            msg += f"   - Estado: {d['estado']}\n"
            if min_battery > 0 and d["bateria"] is not None:
                msg += f"   - Batería: {d['bateria']}%\n"
            msg += f"   - Almacenamiento: {d['almacenamiento']}\n"
            msg += f"   - Color: {d['color']}\n"
            msg += f"   - Vendedor: {d['vendedor']}\n"
            msg += f"   - <a href='{d['link']}'>Ver Anuncio</a>\n\n"

        return msg

    except Exception as e:
        logger.error(f"Error scraping: {e}")
        return None

    finally:
        if driver:
            driver.quit()

# --- Bot commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("🤖 Bot Swappa activo. Usa /help")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "/remind [url] [precio] [condición] [batería] [frecuencia]"
    )

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    if len(context.args) != 5:
        await update.message.reply_text("Formato incorrecto. Usa /help")
        return

    url, max_price, condition, min_battery, freq = context.args
    device_name = await asyncio.to_thread(get_device_name, url)

    max_price = float(max_price)
    min_battery = int(min_battery)

    value = int(re.findall(r"\d+", freq)[0])
    unit = re.findall(r"[a-zA-Z]+", freq)[0].lower()
    frequency_seconds = value * 3600 if unit == "h" else value * 60

    reminder_id = f"reminder_{chat_id}_{int(time.time())}"

    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, int(time.time()), device_name))
        conn.commit()
    conn.close()

    await update.message.reply_text(f"Alerta creada para {device_name}")

    res = await asyncio.to_thread(scrape_swappa, url, max_price, condition, min_battery, device_name)
    if res:
        await update.message.reply_html(res, disable_web_page_preview=True)

async def my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders WHERE chat_id=%s", (chat_id,))
        rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No tienes recordatorios.")
        return

    msg = "<b>Tus recordatorios:</b>\n"
    for r in rows:
        msg += f"{r['device_name']} — ${r['max_price']} — {r['condition']}\n"
    await update.message.reply_html(msg)

async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Proporciona el ID.")
        return
    rid = context.args[0]
    chat_id = str(update.message.chat_id)

    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reminders WHERE reminder_id=%s AND chat_id=%s", (rid, chat_id))
        conn.commit()
    conn.close()

    await update.message.reply_text("Recordatorio eliminado.")

# --- Scheduler ---
async def run_scheduler_check():
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders")
        reminders = cur.fetchall()
    conn.close()

    now = int(time.time())
    bot = Application.builder().token(TELEGRAM_TOKEN).build()

    for r in reminders:
        if now - r["last_checked"] >= r["frequency_seconds"]:
            res = await asyncio.to_thread(
                scrape_swappa,
                r["url"],
                r["max_price"],
                r["condition"],
                r["min_battery"],
                r["device_name"],
            )
            conn = db_connect()
            with conn.cursor() as cur:
                cur.execute("UPDATE reminders SET last_checked=%s WHERE id=%s", (now, r["id"]))
                conn.commit()
            conn.close()

            if res:
                await bot.bot.send_message(
                    chat_id=r["chat_id"],
                    text=res,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

def run_bot_polling():
    setup_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("myreminders", my_reminders))
    app.add_handler(CommandHandler("stopreminder", stop_reminder))
    app.run_polling()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run_scheduler_check":
        asyncio.run(run_scheduler_check())
    else:
        run_bot_polling()
