import os
import re
import logging
import time
import asyncio
import sys
import psycopg2
import requests # Usaremos requests para obtener el nombre del producto rápidamente
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
        # Asegurarnos de que la columna existe si la tabla ya fue creada
        cur.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS device_name TEXT;")
        conn.commit()
    conn.close()

# --- Nueva Función para Obtener el Nombre del Producto ---
def get_device_name(url: str):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(url, headers=headers)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, 'html.parser')
        # El nombre está en el primer <span> dentro del <h1>
        name_tag = soup.find('h1').find('span')
        if name_tag:
            return name_tag.text.strip()
        return "Producto Desconocido"
    except Exception as e:
        logger.error(f"No se pudo obtener el nombre del dispositivo de {url}: {e}")
        return "Producto Desconocido"

# --- Lógica de Scraping con Paginación y Anti-Duplicados ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Iniciando búsqueda para {device_name} en URL base: {url}")
    driver = None
    all_found_devices = []
    processed_links = set()

    try:
        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        # NO fuerces version_main, deja que uc maneje el driver
        driver = uc.Chrome(options=options)

        # --- BUCLE PARA REVISAR LAS PRIMERAS 3 PÁGINAS ---
        for page_num in range(1, 4):
            if page_num == 1:
                page_url = url
            else:
                sep = "&" if "?" in url else "?"
                page_url = f"{url}{sep}page={page_num}"

            logger.info(f"Revisando página {page_num}: {page_url}")
            driver.get(page_url)

            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.listing_card"))
                )
            except Exception:
                logger.info("No se encontraron más anuncios, terminando paginación.")
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            anuncios = soup.select("div.listing_card")

            for anuncio in anuncios:
                try:
                    # --- LINK ---
                    link_tag = anuncio.find("a", href=re.compile(r"^/listing/view/"))
                    if not link_tag:
                        continue
                    link = "https://swappa.com" + link_tag["href"]

                    if link in processed_links:
                        continue
                    processed_links.add(link)

                    # --- PRECIO ---
                    price_tag = anuncio.find("span", class_="price")
                    if not price_tag:
                        continue
                    precio = float(price_tag.get_text(strip=True).replace("$", ""))

                    # --- VENDEDOR ---
                    seller_tag = anuncio.select_one("div.seller_name")
                    vendedor = seller_tag.get_text(strip=True) if seller_tag else "N/A"

                    # --- ATRIBUTOS ---
                    estado = "N/A"
                    bateria = 0
                    almacenamiento = "N/A"
                    color = "N/A"

                    attrs = anuncio.select("span.attr")
                    for attr in attrs:
                        text = attr.get_text(strip=True)

                        # Condición
                        if text.lower() in ["mint", "good", "fair", "used", "new"]:
                            estado = text

                        # Batería
                        if "%" in text:
                            m = re.search(r"(\d+)%", text)
                            if m:
                                bateria = int(m.group(1))

                        # Almacenamiento
                        if re.search(r"\d+\s*(GB|TB)", text, re.I):
                            almacenamiento = text

                        # Color (ícono + texto)
                        spans = attr.find_all("span")
                        if len(spans) == 2:
                            color = spans[1].get_text(strip=True)

                    cumple_bateria = bateria >= min_battery if min_battery > 0 else True

                    if (
                        precio <= max_price
                        and estado.lower() == desired_condition.lower()
                        and cumple_bateria
                    ):
                        all_found_devices.append({
                            "precio": precio,
                            "estado": estado,
                            "bateria": bateria,
                            "link": link,
                            "vendedor": vendedor,
                            "color": color,
                            "almacenamiento": almacenamiento
                        })

                except Exception:
                    continue

        if not all_found_devices:
            return None

        mensaje_final = (
            f"<b>🔔 ¡Alerta de Swappa! Se encontraron {len(all_found_devices)} "
            f"ofertas de {device_name}:</b>\n\n"
        )

        for d in all_found_devices:
            mensaje_final += f"📱 <b>Precio: ${d['precio']}</b>\n"
            mensaje_final += f"   - Estado: {d['estado']}\n"
            if min_battery > 0:
                mensaje_final += f"   - Batería: {d['bateria']}%\n"
            mensaje_final += f"   - Almacenamiento: {d['almacenamiento']}\n"
            mensaje_final += f"   - Color: {d['color']}\n"
            mensaje_final += f"   - Vendedor: {d['vendedor']}\n"
            mensaje_final += f"   - <a href='{d['link']}'>Ver Anuncio</a>\n\n"

        return mensaje_final

    except Exception as e:
        logger.error(f"Error durante el scraping: {e}")
        return (
            f"⚠️ <b>Error en la búsqueda para {device_name}:</b>\n"
            f"<pre>{e}</pre>"
        )

    finally:
        if driver:
            driver.quit()

# --- Comandos del Bot de Telegram ---
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
        "2. Precio máximo\n"
        "3. Condición (Good, Mint, New, Fair, Used, etc.)\n"
        "4. Batería mínima (<b>Usa 0 si no quieres filtrar por batería</b>)\n"
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
        await update.message.reply_html("⚠️ <b>Formato incorrecto.</b> Necesito 5 parámetros. Usa /help.")
        return
    try:
        url, max_price, condition, min_battery, frequency_str = args
        await update.message.reply_text("🤖 Obteniendo información del producto...")
        device_name = await asyncio.to_thread(get_device_name, url)

        max_price_f = float(max_price)
        min_battery_i = int(min_battery)
        
        time_value = int(re.findall(r'\d+', frequency_str)[0])
        time_unit = re.findall(r'[a-zA-Z]+', frequency_str)[0].lower()

        if time_unit == 'h':
            frequency_seconds = time_value * 3600
            display_freq = f"{time_value} horas"
        elif time_unit == 'm':
            frequency_seconds = time_value * 60
            display_freq = f"{time_value} minutos"
        else:
            await update.message.reply_html("⚠️ <b>Unidad de tiempo inválida.</b> Usa 'h' para horas o 'm' para minutos.")
            return

        reminder_id = f"reminder_{chat_id}_{int(time.time())}"
        
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (chat_id, reminder_id, url, max_price_f, condition, min_battery_i, frequency_seconds, int(time.time()), device_name)
            )
            conn.commit()
        conn.close()
        
        await update.message.reply_html(
            f"✅ <b>Recordatorio configurado para {device_name}.</b> Se buscará cada {display_freq}.\n\n"
            f"<i>Realizando la primera búsqueda en las 3 primeras páginas...</i> 🔍"
        )
        
        resultado_inicial = await asyncio.to_thread(scrape_swappa, url, max_price_f, condition, min_battery_i, device_name)

        if resultado_inicial and "Error" not in resultado_inicial:
            await update.message.reply_html(resultado_inicial, disable_web_page_preview=True)
        elif "Error" in (resultado_inicial or ""):
            await update.message.reply_html(resultado_inicial, disable_web_page_preview=True)
        else:
            await update.message.reply_text("😥 Búsqueda inicial completada. No se encontraron ofertas que cumplan tus criterios.")

    except (ValueError, IndexError):
        await update.message.reply_html("⚠️ <b>Parámetros incorrectos.</b> Revisa el formato y usa /help.")
    except Exception as e:
        logger.error(f"Error en /remind: {e}")
        await update.message.reply_html("❌ Hubo un error al guardar tu recordatorio.")

async def my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders WHERE chat_id = %s", (chat_id,))
        user_reminders = cur.fetchall()
    conn.close()

    if not user_reminders:
        await update.message.reply_text("‼ No tienes ningún recordatorio activo.")
        return
    
    message = "<b>📍 Tus recordatorios activos:</b>\n"
    for r in user_reminders:
        bateria_info = f"{r['min_battery']}%" if r['min_battery'] > 0 else "No Aplica"
        freq_seconds = r.get('frequency_seconds')
        if freq_seconds >= 3600:
            display_freq = f"Cada {freq_seconds // 3600} horas"
        else:
            display_freq = f"Cada {freq_seconds // 60} minutos"

        message += "----------------------------------\n"
        message += f"📱 <b>{r.get('device_name', 'Producto Desconocido')}</b>\n"
        message += f"🆔 <b>ID:</b> <code>{r['reminder_id']}</code>\n"
        message += f"💰 <b>Precio Máx:</b> ${r['max_price']}\n"
        message += f"✨ <b>Condición:</b> {r['condition']}\n"
        message += f"🔋 <b>Batería Mín:</b> {bateria_info}\n"
        message += f"⏰ <b>Frecuencia:</b> {display_freq}\n"
    
    message += "----------------------------------\n\n"
    message += "‼ Para eliminar un recordatorio, usa /stopreminder [ID]"
    await update.message.reply_html(message, disable_web_page_preview=True)

async def stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Por favor, proporciona el ID del recordatorio.")
        return
    
    reminder_id_to_delete = context.args[0]
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM reminders WHERE reminder_id = %s AND chat_id = %s",
            (reminder_id_to_delete, chat_id)
        )
        deleted_count = cur.rowcount
        conn.commit()
    conn.close()

    if deleted_count > 0:
        await update.message.reply_text(f"✅ Recordatorio {reminder_id_to_delete} eliminado.")
    else:
        await update.message.reply_text("❌ No se encontró un recordatorio con ese ID o no te pertenece.")

# --- Funciones de Ejecución ---
async def run_scheduler_check():
    logger.info("Iniciando revisión de todos los recordatorios...")
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders")
        reminders = cur.fetchall()
    conn.close()
    
    current_time = int(time.time())
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()

    for r in reminders:
        freq_seconds = r.get('frequency_seconds', r.get('frequency_hours', 1) * 3600)
        if current_time - r['last_checked'] > freq_seconds:
            logger.info(f"Ejecutando recordatorio: {r['reminder_id']}")
            resultado = await asyncio.to_thread(scrape_swappa, r["url"], r["max_price"], r["condition"], r["min_battery"], r.get("device_name", "Producto"))
            
            conn_update = db_connect()
            with conn_update.cursor() as cur_update:
                cur_update.execute("UPDATE reminders SET last_checked = %s WHERE id = %s", (current_time, r['id']))
                conn_update.commit()
            conn_update.close()

            if resultado and "Error" not in resultado:
                await bot_app.bot.send_message(chat_id=r["chat_id"], text=resultado, parse_mode='HTML', disable_web_page_preview=True)
            elif "Error" in (resultado or ""):
                 await bot_app.bot.send_message(chat_id=r["chat_id"], text=resultado, parse_mode='HTML', disable_web_page_preview=True)

    logger.info("Revisión de recordatorios completada.")

def run_bot_polling():
    if not DATABASE_URL or not TELEGRAM_TOKEN:
        logger.error("Faltan variables de entorno.")
        return
    setup_database()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("remind", remind))
    application.add_handler(CommandHandler("myreminders", my_reminders))
    application.add_handler(CommandHandler("stopreminder", stop_reminder))
    
    logger.info("Iniciando el bot en modo polling...")
    application.run_polling()

if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == 'run_bot_polling':
            run_bot_polling()
        elif sys.argv[1] == 'run_scheduler_check':
            asyncio.run(run_scheduler_check())
    else:
        print("Uso: python main.py [run_bot_polling|run_scheduler_check]")
