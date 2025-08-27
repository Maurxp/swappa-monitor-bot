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

# --- Lógica de Scraping Mejorada ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str):
    logger.info(f"Iniciando búsqueda para {device_name} en URL: {url}")
    driver = None
    try:
        options = uc.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        driver = uc.Chrome(options=options)
        
        driver.get(url)
        wait = WebDriverWait(driver, 30)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'tr[itemprop="offers"]')))
        html_content = driver.page_source
        soup = BeautifulSoup(html_content, 'html.parser')
        anuncios = soup.find_all('tr', itemprop='offers')
        if not anuncios: return "No se encontraron anuncios en la página."
        
        dispositivos_encontrados = []
        for anuncio in anuncios:
            try:
                precio_tag = anuncio.find('span', itemprop='price')
                if not precio_tag: continue
                precio = float(precio_tag.text.strip())
                
                condicion_tag = anuncio.find('meta', itemprop='itemCondition')
                if not condicion_tag: continue
                estado = condicion_tag.parent.text.strip()

                vendedor_tag = anuncio.find('span', itemprop='name')
                vendedor = vendedor_tag.text.strip() if vendedor_tag else "N/A"
                
                # --- LÓGICA DE EXTRACCIÓN DEFINITIVA ---
                color = "N/A"
                almacenamiento = "N/A"
                
                title_tag = anuncio.find('a', title=True)
                if title_tag and title_tag.get('title'):
                    title_text = title_tag.get('title')
                    if ' - ' in title_text:
                        specs_part = title_text.split(' - ', 1)[-1]
                        specs_list = [spec.strip() for spec in specs_part.split(',')]
                        
                        # 1. Extraer almacenamiento (el valor más grande en GB o TB)
                        storage_options = []
                        for spec in specs_list:
                            match = re.search(r'(\d+)\s*(GB|TB)', spec, re.IGNORECASE)
                            if match:
                                value = int(match.group(1))
                                unit = match.group(2).upper()
                                # Convertir todo a GB para comparar fácilmente
                                normalized_value = value * 1024 if unit == 'TB' else value
                                storage_options.append((normalized_value, spec))
                        
                        if storage_options:
                            # Encontrar la tupla con el valor normalizado más grande y usar su string original
                            almacenamiento = max(storage_options, key=lambda item: item[0])[1]

                        # 2. Extraer color (generalmente el segundo elemento)
                        if len(specs_list) > 1:
                            # Evitar que el almacenamiento o el carrier se confundan con el color
                            if specs_list[1] != almacenamiento and "unlocked" not in specs_list[1].lower():
                                color = specs_list[1]

                bateria = 0
                cumple_bateria = False
                if min_battery > 0:
                    bateria_tag = anuncio.find('td', class_='col_featured', tabindex='0')
                    if bateria_tag and '%' in bateria_tag.text:
                        bateria_match = re.search(r'(\d+)', bateria_tag.text)
                        if bateria_match: bateria = int(bateria_match.group(1))
                    cumple_bateria = bateria >= min_battery
                else:
                    cumple_bateria = True
                
                link_tag = anuncio.find('a', href=True)
                link = "https://swappa.com" + link_tag['href'] if link_tag else "Enlace no encontrado"

                if precio < max_price and estado.lower() == desired_condition.lower() and cumple_bateria:
                    dispositivos_encontrados.append({
                        "precio": precio, "estado": estado, "bateria": bateria, "link": link,
                        "vendedor": vendedor, "color": color, "almacenamiento": almacenamiento
                    })
            except (ValueError, AttributeError, IndexError): continue
        
        if dispositivos_encontrados:
            mensaje_final = f"<b>🔔 ¡Alerta de Swappa! Se encontraron ofertas de {device_name}:</b>\n\n"
            for dispositivo in dispositivos_encontrados:
                mensaje_final += f"📱 <b>Precio: ${dispositivo['precio']}</b>\n"
                mensaje_final += f"   - Estado: {dispositivo['estado']}\n"
                if min_battery > 0:
                    mensaje_final += f"   - Batería: {dispositivo.get('bateria', 'N/A')}%\n"
                mensaje_final += f"   - Almacenamiento: {dispositivo['almacenamiento']}\n"
                mensaje_final += f"   - Color: {dispositivo['color']}\n"
                mensaje_final += f"   - Vendedor: {dispositivo['vendedor']}\n"
                mensaje_final += f"   - <a href='{dispositivo['link']}'>Ver Anuncio</a>\n\n"
            return mensaje_final
        else:
            return None
    except Exception as e:
        logger.error(f"Error durante el scraping: {e}")
        return f"⚠️ <b>Error en la búsqueda para {device_name}:</b>\n<pre>{e}</pre>"
    finally:
        if driver: driver.quit()

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
            f"<i>Realizando la primera búsqueda ahora...</i> 🔍"
        )
        
        resultado_inicial = await asyncio.to_thread(scrape_swappa, url, max_price_f, condition, min_battery_i, device_name)

        if resultado_inicial and "Error" not in resultado_inicial:
            await update.message.reply_html(resultado_inicial)
        elif "Error" in (resultado_inicial or ""):
            await update.message.reply_html(resultado_inicial)
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
            resultado = scrape_swappa(r["url"], r["max_price"], r["condition"], r["min_battery"], r.get("device_name", "Producto"))
            
            conn_update = db_connect()
            with conn_update.cursor() as cur_update:
                cur_update.execute("UPDATE reminders SET last_checked = %s WHERE id = %s", (current_time, r['id']))
                conn_update.commit()
            conn_update.close()

            if resultado and "Error" not in resultado:
                await bot_app.bot.send_message(chat_id=r["chat_id"], text=resultado, parse_mode='HTML')
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
