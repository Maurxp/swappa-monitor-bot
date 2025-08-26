import os
import re
import logging
import time
import asyncio
import psycopg2 # Librería para conectar con la base de datos Postgres
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
    """Establece conexión con la base de datos."""
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def setup_database():
    """Crea la tabla de recordatorios si no existe."""
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
                frequency_hours INTEGER NOT NULL,
                last_checked BIGINT NOT NULL
            );
        """)
        conn.commit()
    conn.close()

# --- Lógica de Scraping (sin cambios) ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int):
    logger.info(f"Iniciando búsqueda para URL: {url}")
    driver = None
    try:
        options = uc.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Heroku instala Chrome en una ruta específica que UC puede encontrar
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
                    dispositivos_encontrados.append({ "precio": precio, "estado": estado, "bateria": bateria, "link": link })
            except (ValueError, AttributeError): continue
        
        if dispositivos_encontrados:
            mensaje_final = "<b>🔔 ¡Alerta de Swappa! Se encontraron ofertas:</b>\n\n"
            for dispositivo in dispositivos_encontrados:
                mensaje_final += f"📱 <b>Precio: ${dispositivo['precio']}</b>\n"
                mensaje_final += f"   - Estado: {dispositivo['estado']}\n"
                if min_battery > 0:
                    mensaje_final += f"   - Batería: {dispositivo.get('bateria', 'N/A')}%\n"
                mensaje_final += f"   - <a href='{dispositivo['link']}'>Ver Anuncio</a>\n\n"
            return mensaje_final
        else:
            return None
    except Exception as e:
        logger.error(f"Error durante el scraping: {e}")
        return f"⚠️ <b>Error en la búsqueda para {url}:</b>\n<pre>{e}</pre>"
    finally:
        if driver: driver.quit()

# --- Comandos del Bot de Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("¡Hola! Tu bot de monitoreo en Heroku está funcionando.")

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    args = context.args
    if len(args) != 5:
        await update.message.reply_html("⚠️ <b>Formato incorrecto.</b> Necesito 5 parámetros. Usa /help.")
        return
    try:
        url, max_price, condition, min_battery, frequency = args
        reminder_id = f"reminder_{chat_id}_{int(time.time())}"
        
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_hours, last_checked)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (chat_id, reminder_id, url, float(max_price), condition, int(min_battery), int(frequency), int(time.time()))
            )
            conn.commit()
        conn.close()
        
        await update.message.reply_html(f"✅ <b>Recordatorio configurado.</b> Se buscará cada {frequency} horas.")
        # ... (Lógica de búsqueda inmediata)
    except ValueError:
        await update.message.reply_html("⚠️ <b>Parámetros incorrectos.</b>")

# ... (Aquí irían los otros comandos: help, myreminders, stopreminder, adaptados para usar la base de datos Postgres)

# --- Lógica para la Tarea Programada (Heroku Scheduler) ---
async def check_all_reminders():
    logger.info("Iniciando revisión de todos los recordatorios...")
    conn = db_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reminders")
        reminders = cur.fetchall()
    conn.close()
    
    current_time = int(time.time())
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()

    for r in reminders:
        if current_time - r['last_checked'] > r['frequency_hours'] * 3600:
            logger.info(f"Ejecutando recordatorio: {r['reminder_id']}")
            resultado = scrape_swappa(r["url"], r["max_price"], r["condition"], r["min_battery"])
            
            conn = db_connect()
            with conn.cursor() as cur:
                cur.execute("UPDATE reminders SET last_checked = %s WHERE id = %s", (current_time, r['id']))
                conn.commit()
            conn.close()

            if resultado and "Error" not in resultado:
                await bot_app.bot.send_message(chat_id=r["chat_id"], text=resultado, parse_mode='HTML')
    logger.info("Revisión de recordatorios completada.")

def main():
    """Esta función se usará para la tarea programada."""
    if not DATABASE_URL or not TELEGRAM_TOKEN:
        logger.error("Faltan variables de entorno (DATABASE_URL o TELEGRAM_TOKEN).")
        return
    setup_database()
    asyncio.run(check_all_reminders())

def bot_main():
    """Esta función se usará para el bot interactivo."""
    if not DATABASE_URL or not TELEGRAM_TOKEN:
        logger.error("Faltan variables de entorno.")
        return
    setup_database()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remind", remind))
    # ... (Añadir los otros handlers)
    logger.info("Iniciando el bot...")
    application.run_polling()

if __name__ == '__main__':
    # Esto permite ejecutar la revisión manualmente para pruebas
    main()
