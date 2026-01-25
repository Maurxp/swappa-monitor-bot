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
    level=logging.INFO
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

# --- Device name ---
def get_device_name(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        page = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(page.text, "html.parser")
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
    except Exception as e:
        logger.warning(f"No se pudo obtener device name: {e}")
    return "Producto"

# ---------- SCRAPING HELPERS ----------

def extract_listing_links(driver):
    links = set()
    anchors = driver.find_elements(By.XPATH, "//a[contains(@href, '/listing/view/')]")
    for a in anchors:
        href = a.get_attribute("href")
        if href:
            links.add(href.split("?")[0])
    return links

def extract_price(soup):
    text = soup.get_text(" ", strip=True)
    m = re.search(r"\$(\d{2,4})", text)
    return float(m.group(1)) if m else None

def extract_seller(soup):
    seller = soup.find("div", class_="seller_name")
    return seller.get_text(strip=True) if seller else "N/A"

def parse_attrs(soup):
    data = {
        "estado": "N/A",
        "bateria": 0,
        "almacenamiento": "N/A",
        "color": "N/A"
    }

    container = soup.find("div", class_="attrs")
    if not container:
        return data

    for attr in container.find_all("span", class_="attr"):
        text = attr.get_text(" ", strip=True)

        if text.lower() in ["mint", "good", "fair", "new"]:
            data["estado"] = text.title()

        elif attr.find("i", attrs={"aria-label": "Battery icon"}):
            m = re.search(r"(\d{2,3})%", text)
            if m:
                data["bateria"] = int(m.group(1))

        elif attr.find("i", attrs={"aria-label": "Color"}):
            spans = attr.find_all("span")
            if spans:
                data["color"] = spans[-1].get_text(strip=True)

        elif re.search(r"\b(GB|TB)\b", text):
            data["almacenamiento"] = text

    return data

# ---------- SCRAPE MAIN ----------

def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Buscando {device_name}")
    driver = None
    found = []
    seen = set()

    try:
        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = uc.Chrome(options=options)

        for page in range(1, 4):
            page_url = url if page == 1 else f"{url}&page={page}"
            driver.get(page_url)

            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//a[contains(@href, '/listing/view/')]"))
            )

            links = extract_listing_links(driver)
            if not links:
                break

            for link in links:
                if link in seen:
                    continue
                seen.add(link)

                try:
                    driver.get(link)
                    time.sleep(1)
                    soup = BeautifulSoup(driver.page_source, "html.parser")

                    precio = extract_price(soup)
                    if precio is None or precio > max_price:
                        continue

                    attrs = parse_attrs(soup)
                    if attrs["estado"].lower() != desired_condition.lower():
                        continue
                    if min_battery > 0 and attrs["bateria"] < min_battery:
                        continue

                    vendedor = extract_seller(soup)

                    found.append({
                        "precio": precio,
                        "estado": attrs["estado"],
                        "bateria": attrs["bateria"],
                        "almacenamiento": attrs["almacenamiento"],
                        "color": attrs["color"],
                        "vendedor": vendedor,
                        "link": link
                    })

                except Exception as e:
                    logger.warning(f"Error en listing {link}: {e}")

        if not found:
            return None

        msg = f"<b>🔔 ¡Alerta de Swappa! Se encontraron {len(found)} ofertas de {device_name}:</b>\n\n"
        for d in found:
            msg += f"📱 <b>Precio: ${d['precio']}</b>\n"
            msg += f"   - Estado: {d['estado']}\n"
            if min_battery > 0:
                msg += f"   - Batería: {d['bateria']}%\n"
            msg += f"   - Almacenamiento: {d['almacenamiento']}\n"
            msg += f"   - Color: {d['color']}\n"
            msg += f"   - Vendedor: {d['vendedor']}\n"
            msg += f"   - <a href='{d['link']}'>Ver Anuncio</a>\n\n"

        return msg

    except Exception as e:
        logger.error(f"Scraping error: {e}")
        return None

    finally:
        if driver:
            driver.quit()

# ---------- BOT ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🤖 Bot Swappa activo.\n\n"
        "/remind - Crear alerta\n"
        "/myreminders - Ver alertas\n"
        "/stopreminder - Eliminar alerta"
    )

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    url, max_price, condition, min_battery, freq = context.args

    device_name = await asyncio.to_thread(get_device_name, url)

    max_price = float(max_price)
    min_battery = int(min_battery)

    seconds = int(re.findall(r"\d+", freq)[0])
    if "h" in freq:
        seconds *= 3600
    else:
        seconds *= 60

    reminder_id = f"{chat_id}_{int(time.time())}"

    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (chat_id, reminder_id, url, max_price, condition, min_battery, seconds, int(time.time()), device_name))
        conn.commit()
    conn.close()

    await update.message.reply_text("🔍 Buscando ofertas...")
    result = await asyncio.to_thread(scrape_swappa, url, max_price, condition, min_battery, device_name)
    if result:
        await update.message.reply_html(result, disable_web_page_preview=True)
    else:
        await update.message.reply_text("No se encontraron ofertas.")

# ---------- RUN ----------

def run_bot_polling():
    setup_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("remind", remind))
    app.run_polling()

if __name__ == "__main__":
    run_bot_polling()
