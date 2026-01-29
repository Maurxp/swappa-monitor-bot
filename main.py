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

# --- Obtener credenciales ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Base de Datos ---
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

# --- Obtener Nombre del Producto ---
def get_device_name(url: str):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        page = requests.get(url, headers=headers)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, 'html.parser')
        
        name_link = soup.find('a', href=re.compile(r'^/listings/'), title=re.compile(r'^Buy '))
        if name_link:
            return name_link.get_text(strip=True)
            
        h1 = soup.find('h1')
        if h1:
            return h1.get_text(strip=True).replace(' on Swappa', '')
            
        return "Producto Swappa"
    except Exception as e:
        logger.error(f"Error nombre dispositivo: {e}")
        return "Producto Swappa"

# --- Scraping ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Buscando {device_name} en: {url}")
    driver = None
    all_found_devices = []
    processed_links = set()
    
    try:
        # --- CORRECCIÓN CRÍTICA DE CHROME ---
        options = uc.ChromeOptions()
        options.add_argument('--headless=new') # Modo headless moderno y estable
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu") # Vital para evitar crash gráfico
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--remote-debugging-port=9222") # Ayuda a estabilizar la conexión del driver
        
        # Eliminamos version_main=139 para permitir auto-detección
        driver = uc.Chrome(options=options)
        
        for page_num in range(1, 4):
            separator = '&' if '?' in url else '?'
            page_url = f"{url}{separator}page={page_num}" if page_num > 1 else url
            
            logger.info(f"Pagina {page_num}: {page_url}")
            driver.get(page_url)
            
            try:
                wait = WebDriverWait(driver, 15)
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "xui_card_listing")))
            except Exception:
                break

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            anuncios = soup.find_all("div", class_="xui_card_listing")
            
            found_on_page = 0
            
            for anuncio in anuncios:
                try:
                    # 1. LINK
                    link_tag = anuncio.find('a', href=re.compile(r'/listing/view/'))
                    if not link_tag:
                         price_div = anuncio.find("div", class_="price")
                         if price_div: link_tag = price_div.find("a", href=True)
                    
                    if not link_tag: continue
                    link = "https://swappa.com" + link_tag['href'] if not link_tag['href'].startswith("http") else link_tag['href']
                    
                    if link in processed_links: continue
                    processed_links.add(link)

                    # 2. PRECIO
                    precio_tag = anuncio.find('span', itemprop='price')
                    if not precio_tag: continue
                    precio = float(precio_tag.text.strip().replace(',', '').replace('$', ''))

                    # 3. VENDEDOR
                    vendedor_tag = anuncio.find('div', class_='seller_name')
                    vendedor = vendedor_tag.text.strip() if vendedor_tag else "N/A"
                    
                    # 4. ATRIBUTOS
                    estado = "N/A"
                    bateria = 0
                    color = "N/A"
                    almacenamiento = "N/A"
                    
                    attrs_div = anuncio.find("div", class_="attrs")
                    if attrs_div:
                        # Condición
                        cond_meta = attrs_div.find("meta", itemprop="itemCondition")
                        if cond_meta and cond_meta.parent:
                            estado = cond_meta.parent.get_text(strip=True)
                        else:
                            for sp in attrs_div.find_all("span", class_="attr"):
                                txt = sp.get_text(" ", strip=True)
                                if txt in ["Mint", "Good", "Fair", "New", "Open Box"]:
                                    estado = txt; break
                        
                        # Batería
                        batt_span = attrs_div.find("span", class_="color_battery")
                        if batt_span:
                            match = re.search(r'(\d+)%', batt_span.get_text(strip=True))
                            if match: bateria = int(match.group(1))
                        
                        # Otros
                        for attr in attrs_div.find_all("span", class_="attr"):
                            txt = attr.get_text(" ", strip=True).strip()
                            if "color_battery" in attr.get("class", []): continue
                            if txt == estado: continue
                            if "warranty" in txt.lower(): continue

                            txt_lower = txt.lower()
                            if "gb" in txt_lower or "tb" in txt_lower:
                                almacenamiento = txt
                            elif "unlocked" in txt_lower: continue
                            elif re.search(r'^[a-z]{1,2}\d{3,4}', txt_lower) or re.search(r'^\d+$', txt_lower):
                                continue
                            else:
                                color = txt

                    if estado == "N/A": continue

                    # --- FILTROS ---
                    if precio > max_price: continue
                    if desired_condition.lower() not in estado.lower(): continue
                    
                    # Filtro Batería: Solo si min_battery > 0
                    if min_battery > 0:
                        if bateria == 0 or bateria < min_battery: continue
                    
                    all_found_devices.append({
                        "precio": precio, "estado": estado, "bateria": bateria, 
                        "link": link, "vendedor": vendedor, "color": color, 
                        "almacenamiento": almacenamiento
                    })
                    found_on_page += 1

                except Exception: continue
            
            if found_on_page == 0 and page_num > 1: break
        
        # --- MENSAJE DINÁMICO (Formato Original) ---
        if all_found_devices:
            all_found_devices.sort(key=lambda x: x['precio'])
            items_to_show = all_found_devices[:15]
            
            msg = f"<b>🔔 ¡Alerta de Swappa! Se encontraron {len(all_found_devices)} ofertas de {device_name}:</b>\n\n"
            
            for d in items_to_show:
                msg += f"📱 <b>Precio: ${d['precio']}</b>\n"
                msg += f"   - Estado: {d['estado']}\n"
                
                # Solo mostrar Batería si existe (>0)
                if d['bateria'] > 0:
                     msg += f"   - Batería: {d['bateria']}%\n"
                
                # Solo mostrar Almacenamiento si no es N/A
                if d['almacenamiento'] != "N/A":
                    msg += f"   - Almacenamiento: {d['almacenamiento']}\n"
                
                # Solo mostrar Color si no es N/A
                if d['color'] != "N/A":
                    msg += f"   - Color: {d['color']}\n"
                
                msg += f"   - Vendedor: {d['vendedor']}\n"
                msg += f"   - <a href='{d['link']}'>Ver Anuncio</a>\n\n"
            
            if len(all_found_devices) > 15:
                msg += f"<i>⚠️ ... y otros {len(all_found_devices) - 15} más.</i>"
                
            return msg
        return None
            
    except Exception as e:
        logger.error(f"Error: {e}")
        # Mensaje simplificado para evitar errores 400 en Telegram con stacktraces largos
        return f"⚠️ Problema temporal conectando con Swappa. Reintentando..."
    finally:
        if driver: 
            try: driver.quit()
            except: pass

# --- Comandos Bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("Bot Swappa Activo 🤖\nUsa /help para instrucciones.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Instrucciones /remind:</b>\n"
        "/remind [URL] [PRECIO] [CONDICION] [BAT] [TIEMPO]\n\n"
        "👉 <b>Importante:</b> Usa <b>0</b> en batería si buscas productos sin batería (AirPods, Laptops, etc)."
    )

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    args = context.args
    if len(args) != 5:
        await update.message.reply_html("⚠️ Faltan datos. Usa /help.")
        return
    try:
        url, max_price, condition, min_battery, frequency_str = args
        await update.message.reply_text("🤖 Configurando...")
        device_name = await asyncio.to_thread(get_device_name, url)

        max_price_f = float(max_price)
        min_battery_i = int(min_battery)
        num = int(re.search(r'\d+', frequency_str)[0])
        unit = re.search(r'[a-zA-Z]+', frequency_str)[0].lower()
        seconds = num * 3600 if 'h' in unit else num * 60
        
        rid = f"reminder_{chat_id}_{int(time.time())}"
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (chat_id, rid, url, max_price_f, condition, min_battery_i, seconds, int(time.time()), device_name)
            )
            conn.commit()
        conn.close()
        
        await update.message.reply_html(f"✅ Alerta para <b>{device_name}</b> creada.\nBuscando...")
        res = await asyncio.to_thread(scrape_swappa, url, max_price_f, condition, min_battery_i, device_name)
        
        if res: await update.message.reply_html(res, disable_web_page_preview=True)
        else: await update.message.reply_text("🔎 Sin resultados inmediatos.")

    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Error. Verifica formato.")

async def my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders WHERE chat_id = %s", (str(update.message.chat_id),))
        rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📭 Sin alertas.")
        return
    
    msg = "<b>📍 Tus Alertas:</b>\n"
    for r in rows:
        bat = f"{r['min_battery']}%" if r['min_battery'] > 0 else "No Aplica"
        freq = f"{r['frequency_seconds']//3600}h" if r['frequency_seconds']>=3600 else f"{r['frequency_seconds']//60}m"
        msg += f"-----------------\n📱 <b>{r.get('device_name')}</b>\n🆔 <code>{r['reminder_id']}</code>\n💰 Max: ${r['max_price']} | ✨ {r['condition']}\n🔋 Bat: {bat} | ⏰ {freq}\n"
    
    await update.message.reply_html(msg, disable_web_page_preview=True)

async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reminders WHERE reminder_id = %s AND chat_id = %s", (context.args[0], str(update.message.chat_id)))
        cnt = cur.rowcount
        conn.commit()
    conn.close()
    await update.message.reply_text("✅ Borrado." if cnt > 0 else "❌ No encontrado.")

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
            res = await asyncio.to_thread(scrape_swappa, r['url'], r['max_price'], r['condition'], r['min_battery'], r['device_name'])
            c2 = db_connect()
            with c2.cursor() as cur2:
                cur2.execute("UPDATE reminders SET last_checked = %s WHERE id = %s", (now, r['id']))
                c2.commit()
            c2.close()
            if res:
                try: await bot.send_message(chat_id=r['chat_id'], text=res, parse_mode='HTML', disable_web_page_preview=True)
                except: pass

def run_bot_polling():
    if not DATABASE_URL or not TELEGRAM_TOKEN: return
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
