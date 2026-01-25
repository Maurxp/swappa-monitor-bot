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

# --- Configuración de Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Base de datos ---
def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

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

# --- Obtener nombre del dispositivo ---
def get_device_name(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        h1 = soup.find("h1", class_="my-3")
        if h1:
            return h1.get_text(strip=True)

        return "Producto Desconocido"
    except Exception:
        return "Producto Desconocido"

# --- SCRAPING (ÚNICO BLOQUE MODIFICADO) ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Iniciando búsqueda para {device_name}")
    driver = None
    encontrados = []
    procesados = set()

    try:
        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = uc.Chrome(options=options)

        for page in range(1, 4):
            page_url = url if page == 1 else f"{url}&page={page}"
            driver.get(page_url)

            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.listing_card"))
                )
            except Exception:
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            anuncios = soup.select("div.listing_card")

            for anuncio in anuncios:
                try:
                    link_tag = anuncio.find("a", href=re.compile(r"^/listing/view/"))
                    if not link_tag:
                        continue

                    link = "https://swappa.com" + link_tag["href"]
                    if link in procesados:
                        co
