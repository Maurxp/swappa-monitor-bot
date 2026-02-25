import os
import re
import logging
import time
import asyncio
import sys
import psycopg2
import requests # Usaremos requests para obtener el nombre del producto rápidamente
import subprocess # <-- AÑADIDO: Para detectar la versión exacta de Chrome
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
        # Añadimos la columna device_name para identificar los recordatorios
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

# --- NUEVO: Detector Dinámico de Versión de Chrome ---
def get_chrome_version():
    try:
        out = subprocess.check_output(['google-chrome', '--version']).decode('utf-8')
        major_version = int(out.split(' ')[2].split('.')[0])
        logger.info(f"Versión de Chrome detectada en Heroku: {major_version}")
        return major_version
    except Exception as e:
        logger.warning(f"No se pudo autodetectar Chrome, usando 145 por defecto. Error: {e}")
        return 145

# --- Obtener el Nombre del Producto ---
def get_device_name(url: str):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(url, headers=headers)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, 'html.parser')
        
        # Buscar enlace de breadcrumb específico para el modelo
        name_link = soup.find('a', href=re.compile(r'^/listings/'), title=re.compile(r'^Buy '))
        if name_link:
            return name_link.get_text(strip=True)
            
        # Fallback al h1
        h1 = soup.find('h1')
        if h1:
            return h1.get_text(strip=True).replace(' on Swappa', '')
            
        return "Producto Swappa"
    except Exception as e:
        logger.error(f"No se pudo obtener el nombre del dispositivo de {url}: {e}")
        return "Producto Desconocido"

# --- Lógica de Scraping ESTRICTA ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Iniciando búsqueda ESTRICTA para {device_name} en URL base: {url}")
    driver = None
    all_found_devices = []
    processed_links = set()
    
    try:
        options = uc.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        
        # --- MODIFICADO: Aplicar versión detectada ---
        chrome_v = get_chrome_version()
        driver = uc.Chrome(options=options, version_main=chrome_v)
        
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
                logger.info(f"Fin de resultados o error en página {page_num}.")
                break

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            # Buscamos las tarjetas de producto nuevas
            anuncios = soup.find_all("div", class_="xui_card_listing")
            
            found_on_page = 0
            
            for anuncio in anuncios:
                try:
                    # 1. ENLACE
                    link_tag = anuncio.find('a', href=re.compile(r'/listing/view/'))
                    if not link_tag:
                         price_div = anuncio.find("div", class_="price")
                         if price_div: link_tag = price_div.find("a", href=True)
                    
                    if not link_tag: continue
                    
                    href = link_tag['href']
                    link = "https://swappa.com" + href if not href.startswith("http") else href
                    
                    if link in processed_links: continue
                    processed_links.add(link)

                    # 2. PRECIO
                    precio_tag = anuncio.find('span', itemprop='price')
                    if not precio_tag: continue
                    precio = float(precio_tag.text.strip().replace(',', '').replace('$', ''))

                    # 3. VENDEDOR
                    vendedor_tag = anuncio.find('div', class_='seller_name')
                    vendedor = vendedor_tag.text.strip() if vendedor_tag else "N/A"
                    
                    # 4. CONDICIÓN Y ATRIBUTOS
                    estado = "N/A"
                    bateria = 0
                    color = "N/A"
                    almacenamiento = "N/A"
                    
                    attrs_div = anuncio.find("div", class_="attrs")
                    if attrs_div:
                        # A. Condición (Prioridad Meta Tag)
                        cond_meta = attrs_div.find("meta", itemprop="itemCondition")
                        if cond_meta and cond_meta.parent:
                            estado = cond_meta.parent.get_text(strip=True)
                        else:
                            # Fallback texto
                            for sp in attrs_div.find_all("span", class_="attr"):
                                txt = sp.get_text(" ", strip=True)
                                if txt in ["Mint", "Good", "Fair", "New", "Open Box"]:
                                    estado = txt
                                    break
                        
                        # B. Batería
                        batt_span = attrs_div.find("span", class_="color_battery")
                        if batt_span:
                            batt_text = batt_span.get_text(strip=True)
                            match = re.search(r'(\d+)%', batt_text)
                            if match: bateria = int(match.group(1))
                        
                        # C. Almacenamiento y Color
                        for attr in attrs_div.find_all("span", class_="attr"):
                            if "color_battery" in attr.get("class", []): continue
                            txt = attr.get_text(" ", strip=True).strip()
                            txt_lower = txt.lower()
                            
                            # Ignorar lo que ya sabemos
                            if txt == estado: continue
                            if "warranty" in txt_lower: continue
                            
                            if "gb" in txt_lower or "tb" in txt_lower:
                                almacenamiento = txt
                            # Filtro para evitar que el Modelo o Unlocked se marque como Color
                            elif "unlocked" in txt_lower:
                                continue
                            # Regex para detectar modelos (ej: A2482, SM-S908U) y ignorarlos
                            elif re.search(r'^[a-z]{1,2}\d{3,4}', txt_lower) or re.search(r'^\d+$', txt_lower):
                                continue
                            else:
                                # Si sobra algo y no es nada de lo anterior, asumimos que es Color
                                color = txt

                    if estado == "N/A": continue

                    # --- FILTRADO ESTRICTO ---
                    
                    # 1. Filtro Precio
                    if precio > max_price: continue

                    # 2. Filtro Condición
                    if desired_condition.lower() not in estado.lower(): continue
                    
                    # 3. Filtro Batería ESTRICTO
                    # Si el usuario pide batería > 0, SOLO mostramos los que tienen batería >= X
                    if min_battery > 0:
                        if bateria == 0: continue # No tiene info -> FUERA
                        if bateria < min_battery: continue # Tiene info pero es baja -> FUERA
                    
                    all_found_devices.append({
                        "precio": precio, 
                        "estado": estado, 
                        "bateria": bateria, 
                        "link": link,
                        "vendedor": vendedor, 
                        "color": color, 
                        "almacenamiento": almacenamiento
                    })
                    found_on_page += 1

                except (ValueError, AttributeError, IndexError):
                    continue
            
            if found_on_page == 0 and page_num > 1:
                break
        
        # --- GENERACIÓN DE MENSAJE (Formato Original) ---
        if all_found_devices:
            all_found_devices.sort(key=lambda x: x['precio'])
            
            # Limitamos a 15 para evitar errores de Telegram por mensaje largo
            items_to_show = all_found_devices[:15]
            total_real = len(all_found_devices)
            
            mensaje_final = f"<b>🔔 ¡Alerta de Swappa! Se encontraron {total_real} ofertas de {device_name}:</b>\n\n"
            
            for dispositivo in items_to_show:
                bat_val = f"{dispositivo['bateria']}%" if dispositivo['bateria'] > 0 else "N/A"
                
                mensaje_final += f"📱 <b>Precio: ${dispositivo['precio']}</b>\n"
                mensaje_final += f"   - Condición: {dispositivo['estado']}\n"
                if min_battery > 0:
                     mensaje_final += f"   - Batería: {bat_val}\n"
                mensaje_final += f"   - Almacenamiento: {dispositivo['almacenamiento']}\n"
                mensaje_final += f"   - Color: {dispositivo['color']}\n"
                mensaje_final += f"   - Vendedor: {dispositivo['vendedor']}\n"
                mensaje_final += f"   - <a href='{dispositivo['link']}'>Ver Anuncio</a>\n\n"
            
            if total_real > 15:
                mensaje_final += f"<i>⚠️ ... y otros {total_real - 15} resultados más. Revisa la web para ver todos.</i>"
                
            return mensaje_final
        else:
            return None
            
    except Exception as e:
        logger.error(f"Error scraping: {e}")
        return f"⚠️ Error buscando {device_name}: {str(e)[:100]}"
    finally:
        if driver: driver.quit()

# --- Comandos del Bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "¡Hola! Soy tu bot de monitoreo de precios para Swappa💚.\n\n"
        "<b>Comandos disponibles:</b>\n"
        "/remind - Configura una nueva alerta y busca de inmediato.\n"
        "/myreminders - Muestra tus alertas activas.\n"
        "/stopreminder - Elimina una alerta.\n"
        "/help - Muestra las instrucciones detalladas.\n\n"
        "<i>Hecho con mucho ❤ por @devmauro</i>"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>✨ Instrucciones para /remind:</b>\n\n"
        "Debes proporcionar 5 parámetros:\n"
        "1. URL de Swappa\n"
        "2. Precio Máximo\n"
        "3. Condición (Good, Mint, New, Fair, Open Box, etc.)\n"
        "4. Batería Mínima (<b>Usa 0 si no quieres filtrar por batería</b>)\n"
        "5. Frecuencia (ej. <b>30m</b> para 30 minutos, <b>2h</b> para 2 horas)\n\n"
        "<b>Ejemplo (cada 2 horas):</b>\n"
        "/remind https://swappa.com/listings/apple-iphone-15 700 Good 90 2h\n\n"
        "<b>Ejemplo (cada 45 minutos):</b>\n"
        "/remind https://swappa.com/listings/google-pixel-8 400 Good 0 45m\n\n"
        "<b>Recuerda el formato:</b>\n"
        "/remind [url_swappa] [precio_max] [condicion] [bateria] [tiempo]"
    )

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    args = context.args
    if len(args) != 5:
        await update.message.reply_html("⚠️ Formato incorrecto. Usa /help.")
        return
    try:
        url, max_price, condition, min_battery, frequency_str = args
        await update.message.reply_text("🤖 Obteniendo información...")
        device_name = await asyncio.to_thread(get_device_name, url)

        max_price_f = float(max_price)
        min_battery_i = int(min_battery)
        
        num = int(re.search(r'\d+', frequency_str)[0])
        unit = re.search(r'[a-zA-Z]+', frequency_str)[0].lower()
        seconds = num * 3600 if 'h' in unit else num * 60
        display_freq = f"{num} {'horas' if 'h' in unit else 'minutos'}"

        reminder_id = f"reminder_{chat_id}_{int(time.time())}"
        
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (chat_id, reminder_id, url, max_price_f, condition, min_battery_i, seconds, int(time.time()), device_name)
            )
            conn.commit()
        conn.close()
        
        await update.message.reply_html(
            f"✅ <b>Recordatorio configurado para {device_name}.</b>\nFrecuencia: {display_freq}.\n\n<i>Buscando ahora...</i> 🔍"
        )
        
        res = await asyncio.to_thread(scrape_swappa, url, max_price_f, condition, min_battery_i, device_name)

        if res:
            await update.message.reply_html(res, disable_web_page_preview=True)
        else:
            await update.message.reply_text("😥 No se encontraron ofertas que cumplan tus criterios.")

    except Exception as e:
        logger.error(f"Error en remind: {e}")
        await update.message.reply_html("❌ Error en los datos. Verifica el formato.")

async def my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders WHERE chat_id = %s", (chat_id,))
        rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("‼ No tienes alertas activas.")
        return
    
    msg = "<b>📍 Tus recordatorios activos:</b>\n"
    for r in rows:
        bat = f"{r['min_battery']}%" if r['min_battery'] > 0 else "No Aplica"
        freq_min = r['frequency_seconds'] // 60
        freq_str = f"{freq_min // 60} horas" if freq_min >= 60 else f"{freq_min} minutos"

        msg += "----------------------------------\n"
        msg += f"📱 <b>{r.get('device_name', 'Producto')}</b>\n"
        msg += f"🆔 <b>ID:</b> <code>{r['reminder_id']}</code>\n"
        msg += f"💰 <b>Max:</b> ${r['max_price']}\n"
        msg += f"✨ <b>Condición:</b> {r['condition']}\n"
        msg += f"🔋 <b>Bat Mín:</b> {bat}\n"
        msg += f"⏰ <b>Cada:</b> {freq_str}\n"
    
    msg += "----------------------------------\n\n‼ Usa /stopreminder [ID] para borrar."
    await update.message.reply_html(msg, disable_web_page_preview=True)

async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("🗣 Recuerda agregar el ID al ejecutar el comando.")
        return
    
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reminders WHERE reminder_id = %s AND chat_id = %s", (context.args[0], str(update.message.chat_id)))
        cnt = cur.rowcount
        conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ Recordatorio eliminado." if cnt > 0 else "❌ Recordatorio no encontrado.")

# --- Ejecución ---
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
                try:
                     await bot.send_message(chat_id=r['chat_id'], text=res, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Error telegram: {e}")

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
