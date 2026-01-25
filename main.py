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

# --- Obtener credenciales de las variables de entorno de Heroku ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Funciones para manejar la Base de Datos Postgres ---
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
        try:
            cur.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS device_name TEXT;")
        except Exception:
            conn.rollback()
        conn.commit()
    conn.close()

# --- Obtener el Nombre del Producto (Headers Mejorados) ---
def get_device_name(url: str):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        page = requests.get(url, headers=headers, timeout=10)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, 'html.parser')
        
        title_selectors = ['h1 span', 'h1', '.product_title', 'title']
        for selector in title_selectors:
            name_tag = soup.select_one(selector)
            if name_tag:
                # Limpieza básica del título
                return name_tag.text.strip().replace(' on Swappa', '')
        
        return "Producto Swappa"
    except Exception as e:
        logger.error(f"No se pudo obtener el nombre del dispositivo de {url}: {e}")
        return "Producto Swappa"

# --- Lógica de Scraping EXACTA ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Iniciando búsqueda para {device_name}...")
    driver = None
    all_found_devices = []
    processed_links = set() 
    
    try:
        options = uc.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        
        driver = uc.Chrome(options=options)
        
        # --- BUCLE PÁGINAS ---
        for page_num in range(1, 4): 
            if page_num == 1:
                page_url = url
            else:
                separator = '&' if '?' in url else '?'
                page_url = f"{url}{separator}page={page_num}"
            
            logger.info(f"Revisando página {page_num}: {page_url}")
            driver.get(page_url)
            
            try:
                wait = WebDriverWait(driver, 15)
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "xui_card_listing")))
            except Exception:
                logger.warning(f"No se detectaron tarjetas 'xui_card_listing' en pág {page_num} o timeout.")
                if page_num > 1: break

            html_content = driver.page_source
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # --- PARSING QUIRÚRGICO ---
            listings = soup.find_all("div", class_="xui_card_listing")
            
            found_on_page = 0
            
            for card in listings:
                try:
                    # 1. PRECIO
                    price_span = card.find("span", itemprop="price")
                    if not price_span: continue
                    
                    price_text = price_span.get_text(strip=True).replace(',', '')
                    if not price_text: continue
                    precio = float(price_text)

                    # 2. LINK
                    link_tag = None
                    price_div = card.find("div", class_="price")
                    if price_div:
                        link_tag = price_div.find("a", href=True)
                    
                    if not link_tag:
                         link_tag = card.find("a", href=re.compile(r'/listing/view/'))

                    if not link_tag: continue
                    
                    href = link_tag['href']
                    link = f"https://swappa.com{href}" if not href.startswith('http') else href
                    if link in processed_links: continue

                    # 3. VENDEDOR
                    seller_div = card.find("div", class_="seller_name")
                    vendedor = seller_div.get_text(strip=True) if seller_div else "Vendedor"

                    # 4. ATRIBUTOS
                    condicion_actual = "N/A"
                    bateria_actual = 0
                    info_extra = []
                    
                    attrs_div = card.find("div", class_="attrs")
                    if attrs_div:
                        # A. Batería
                        batt_span = attrs_div.find("span", class_="color_battery")
                        if batt_span:
                            batt_text = batt_span.get_text(strip=True)
                            batt_match = re.search(r'(\d+)%', batt_text)
                            if batt_match:
                                bateria_actual = int(batt_match.group(1))
                        
                        # B. Resto de atributos
                        all_attrs = attrs_div.find_all("span", class_="attr")
                        known_conditions = ["mint", "good", "fair", "new", "open box"]
                        
                        for attr in all_attrs:
                            if "color_battery" in attr.get("class", []): continue
                            
                            txt = attr.get_text(" ", strip=True).lower()
                            
                            is_condition = False
                            for cond in known_conditions:
                                if cond == txt:
                                    condicion_actual = txt.title()
                                    is_condition = True
                                    break
                            
                            if not is_condition and "warranty" not in txt:
                                clean_txt = txt.strip()
                                if clean_txt: info_extra.append(clean_txt.title())

                    if condicion_actual == "N/A":
                        full_text = card.get_text(" ", strip=True).lower()
                        if "mint" in full_text: condicion_actual = "Mint"
                        elif "good" in full_text: condicion_actual = "Good"
                        elif "fair" in full_text: condicion_actual = "Fair"

                    # --- FILTROS ---
                    if precio > max_price: continue

                    if desired_condition.lower() not in condicion_actual.lower():
                        continue

                    if min_battery > 0:
                        if bateria_actual > 0 and bateria_actual < min_battery:
                            continue

                    processed_links.add(link)
                    
                    all_found_devices.append({
                        "precio": precio,
                        "estado": condicion_actual,
                        "bateria": bateria_actual,
                        "link": link,
                        "vendedor": vendedor,
                        "info": ", ".join(info_extra)
                    })
                    found_on_page += 1
                        
                except Exception as e:
                    logger.error(f"Error parseando tarjeta: {e}")
                    continue
            
            if found_on_page == 0 and page_num > 1:
                break
        
        # --- GENERAR RESPUESTA ---
        if all_found_devices:
            all_found_devices.sort(key=lambda x: x['precio'])
            msg = f"<b>🔔 ¡Swappa Bot! {len(all_found_devices)} ofertas para {device_name}:</b>\n\n"
            
            for d in all_found_devices[:10]:
                batt_icon = "🔋" if d['bateria'] > 0 else "❓"
                batt_str = f"{d['bateria']}%" if d['bateria'] > 0 else "N/A"
                
                msg += f"📱 <b>${d['precio']}</b> | {d['estado']}\n"
                msg += f"   {batt_icon} Bat: {batt_str} | {d['info']}\n"
                msg += f"   👤 {d['vendedor']}\n"
                msg += f"   🔗 <a href='{d['link']}'>Ver Oferta</a>\n\n"
            
            return msg
        return None

    except Exception as e:
        logger.error(f"Error scraping general: {e}")
        return None
    finally:
        if driver: 
            try: driver.quit()
            except: pass

# --- Comandos del Bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>Swappa Bot Reparado</b>\n\n"
        "Comando:\n<code>/remind [URL] [PRECIO] [CONDICION] [BAT] [TIEMPO]</code>\n\n"
        "Ejemplo:\n<code>/remind https://swappa.com/listings/iphone-13 350 Mint 90 30m</code>"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Ayuda:</b>\n"
        "1. Ve a Swappa y copia el link del producto.\n"
        "2. Usa /remind con tus filtros.\n"
        "3. Usa /myreminders para ver qué estás buscando.\n"
        "4. Usa /stopreminder [ID] para borrar."
    )

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    args = context.args
    if len(args) != 5:
        await update.message.reply_text("❌ Faltan datos. Ejemplo: /remind [link] 300 Good 85 1h")
        return

    url, price, cond, batt, freq = args
    
    try:
        num = int(re.search(r'\d+', freq).group())
        unit = re.search(r'[a-zA-Z]', freq).group().lower()
        seconds = num * 3600 if 'h' in unit else num * 60
        
        device_name = await asyncio.to_thread(get_device_name, url)
        rid = f"rem_{chat_id}_{int(time.time())}"
        
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (chat_id, rid, url, float(price), cond, int(batt), seconds, int(time.time()), device_name)
            )
            conn.commit()
        conn.close()
        
        await update.message.reply_html(f"✅ Alerta creada para <b>{device_name}</b>.\nBuscando...")
        
        res = await asyncio.to_thread(scrape_swappa, url, float(price), cond, int(batt), device_name)
        if res:
            await update.message.reply_html(res, disable_web_page_preview=True)
        else:
            await update.message.reply_text("🔎 No encontré nada ahora, pero seguiré buscando automáticamente.")
            
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Error. Verifica el formato (ej. precio sin signos de dolar).")

async def my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders WHERE chat_id = %s", (str(update.message.chat_id),))
        rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📭 Sin alertas.")
        return

    msg = "<b>Tus Alertas:</b>\n\n"
    for r in rows:
        # CORREGIDO: Eliminados caracteres < y > que rompen el HTML de Telegram
        # También escapamos el nombre por seguridad
        safe_name = str(r['device_name']).replace('<', '').replace('>', '')
        
        msg += f"🔹 <b>{safe_name}</b>\n"
        msg += f"   Max: ${r['max_price']} | {r['condition']} | Bat: {r['min_battery']}%\n"
        msg += f"   ID: <code>{r['reminder_id']}</code>\n\n"
    
    # Enviamos el mensaje corregido
    await update.message.reply_html(msg)

async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Dime el ID (usa /myreminders).")
        return
    
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reminders WHERE reminder_id = %s AND chat_id = %s", (context.args[0], str(update.message.chat_id)))
        cnt = cur.rowcount
        conn.commit()
    conn.close()
    
    await update.message.reply_text("✅ Borrado." if cnt > 0 else "❌ ID incorrecto.")

# --- Scheduler ---
async def run_scheduler_check():
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders")
        rows = cur.fetchall()
    conn.close()
    
    bot = Application.builder().token(TELEGRAM_TOKEN).build().bot
    now = int(time.time())
    
    for r in rows:
        if now - r['last_checked'] > r['frequency_seconds']:
            logger.info(f"Check auto: {r['device_name']}")
            res = await asyncio.to_thread(scrape_swappa, r['url'], r['max_price'], r['condition'], r['min_battery'], r['device_name'])
            
            c2 = db_connect()
            with c2.cursor() as cur2:
                cur2.execute("UPDATE reminders SET last_checked = %s WHERE id = %s", (now, r['id']))
                c2.commit()
            c2.close()
            
            if res:
                try:
                    await bot.send_message(chat_id=r['chat_id'], text=res, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Error enviando a Telegram: {e}")

def run_bot_polling():
    setup_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("myreminders", my_reminders))
    app.add_handler(CommandHandler("stopreminder", stop_reminder))
    app.run_polling()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'run_scheduler_check':
        asyncio.run(run_scheduler_check())
    else:
        run_bot_polling()
