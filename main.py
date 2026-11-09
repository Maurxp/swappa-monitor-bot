import os
import re
import logging
import time
import asyncio
import sys
import shutil
import subprocess
from enum import Enum
from dataclasses import dataclass
from urllib.parse import urlparse
import requests

# Importaciones condicionales seguras para portabilidad de pruebas y ejecución en producción
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    Update = None
    Application = None
    CommandHandler = None
    ContextTypes = None

try:
    import undetected_chromedriver as uc
except ImportError:
    uc = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
except ImportError:
    WebDriverWait = None
    EC = None
    By = None

# --- Filtro de Redacción de Tokens para Logging ---
class SensitiveTokenFilter(logging.Filter):
    """
    Filtro de seguridad que intercepta y redacta tokens de Telegram
    (formato bot<id>:<token>) de cualquier mensaje de log antes de su emisión.
    """
    TOKEN_PATTERN = re.compile(r'bot\d+:[A-Za-z0-9_\-]+')

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.TOKEN_PATTERN.sub("bot[REDACTED_TELEGRAM_TOKEN]", record.msg)
        if record.args:
            clean_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    clean_args.append(self.TOKEN_PATTERN.sub("bot[REDACTED_TELEGRAM_TOKEN]", arg))
                else:
                    clean_args.append(arg)
            record.args = tuple(clean_args)
        return True

# --- Configuración de Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Aplicar filtro de redacción a todos los handlers del logger raíz
token_filter = SensitiveTokenFilter()
for handler in logging.root.handlers:
    handler.addFilter(token_filter)

# Silenciar logs verbosos a nivel INFO de httpx y httpcore que exponen URLs salientes con tokens
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

# --- Tipos y Contratos Explícitos del Scraper ---
class ScrapeStatus(Enum):
    SUCCESS = "SUCCESS"  # Scraping exitoso con ofertas encontradas
    EMPTY = "EMPTY"      # Scraping exitoso pero sin ofertas que cumplan criterios
    ERROR = "ERROR"      # Error técnico durante inicialización o scraping

@dataclass
class ScrapeResult:
    status: ScrapeStatus
    items: list = None
    formatted_message: str = None
    error_message: str = None

class ChromeDetectionError(Exception):
    """Excepción descriptiva cuando no se puede detectar un ejecutable de Chrome válido."""
    pass

# --- Semáforo de Concurrencia para Procesos Chrome ---
# Limita la apertura de instancias Chrome simultáneas por proceso para proteger la RAM (dyno 512MB)
SCRAPE_SEMAPHORE = asyncio.Semaphore(1)

# --- Obtener credenciales de las variables de entorno ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Validación de URLs (Protección SSRF) ---
def is_valid_swappa_url(url: str) -> bool:
    """Valida que la URL pertenezca estrictamente al dominio de Swappa y utilice HTTPS."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https':
            return False
        hostname = (parsed.hostname or '').lower()
        return hostname in ['swappa.com', 'www.swappa.com']
    except Exception:
        return False

# --- Funciones para manejar la Base de Datos Postgres ---
def db_connect():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 no está instalado en este entorno.")
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

# --- Detector Dinámico y Centralizado de Chrome ---
def resolve_chrome_environment() -> tuple[str | None, int | None]:
    """
    Resuelve la ruta del binario de Chrome/Chromium y detecta su versión mayor real.
    Prioriza variables de entorno de Heroku, comandos en PATH (ej. 'chrome' en chrome-for-testing),
    y ubicaciones estándar por sistema operativo.
    Retorna: (binary_path: str | None, major_version: int | None)
    """
    candidates = []

    # 1. Variables de entorno explícitas / Heroku
    for env_var in ["GOOGLE_CHROME_BIN", "CHROME_BIN", "CHROME_PATH", "GOOGLE_CHROME_SHIM"]:
        val = os.environ.get(env_var)
        if val and val not in candidates:
            candidates.append(val)

    # 2. Comandos en PATH (en heroku-buildpack-chrome-for-testing el ejecutable es 'chrome')
    for name in ["chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)

    # 3. Ruta directa del buildpack heroku-buildpack-chrome-for-testing
    cft_path = "/app/.chrome-for-testing/chrome-linux64/chrome"
    if os.path.exists(cft_path) and cft_path not in candidates:
        candidates.append(cft_path)

    # 4. Ubicaciones estándar en Windows (únicamente en Windows)
    if sys.platform == "win32":
        for win_path in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]:
            if os.path.exists(win_path) and win_path not in candidates:
                candidates.append(win_path)

    # Probar cada candidato ejecutable
    for bin_path in candidates:
        if not bin_path:
            continue
        # Si es una ruta con separadores y el archivo no existe físicamente, omitir
        if ("/" in bin_path or "\\" in bin_path) and not os.path.exists(bin_path):
            continue
        if sys.platform != "win32" and ("/" in bin_path) and not os.access(bin_path, os.X_OK):
            continue
        try:
            out = subprocess.check_output([bin_path, '--version'], stderr=subprocess.STDOUT, timeout=5).decode('utf-8')
            match = re.search(r'(\d+)\.\d+\.\d+', out)
            if match:
                major = int(match.group(1))
                logger.info(f"Chrome detectado en '{bin_path}' con versión mayor: {major}")
                return bin_path, major
        except Exception as e:
            logger.debug(f"Candidato Chrome '{bin_path}' no disponible: {e}")
            continue

    # 5. Soporte para override manual explícito vía variable de entorno
    env_override = os.environ.get("CHROME_VERSION_MAIN")
    if env_override and env_override.strip().isdigit():
        major = int(env_override.strip())
        if major > 0:
            logger.info(f"Usando CHROME_VERSION_MAIN configurado: {major}")
            return None, major

    raise ChromeDetectionError(
        "No se pudo detectar un ejecutable de Chrome compatible ni determinar su versión mayor. "
        "Asegúrese de tener Chrome/Chromium instalado o defina GOOGLE_CHROME_BIN / CHROME_PATH."
    )

def get_chrome_version() -> int | None:
    """Wrapper de compatibilidad retroactiva."""
    try:
        _, major_v = resolve_chrome_environment()
        return major_v
    except ChromeDetectionError as e:
        logger.error(f"Error detectando versión de Chrome: {e}")
        return None

# --- Factory de Undetected Chromedriver ---
def create_uc_driver():
    """
    Factory centralizado para instanciar undetected_chromedriver.
    Garantiza sincronización de version_main y browser_executable_path,
    opciones headless optimizadas y protección de recursos.
    """
    if uc is None:
        raise RuntimeError("undetected_chromedriver no está instalado en este entorno.")

    bin_path, major_v = resolve_chrome_environment()

    options = uc.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    kwargs = {'options': options}
    if major_v:
        kwargs['version_main'] = major_v
    if bin_path:
        kwargs['browser_executable_path'] = bin_path

    logger.info(f"Iniciando undetected_chromedriver con version_main={major_v}, browser_executable_path={bin_path}")
    return uc.Chrome(**kwargs)

# --- Obtener el Nombre del Producto ---
def get_device_name(url: str) -> str:
    if not is_valid_swappa_url(url):
        logger.warning(f"URL rechazada por validación SSRF: {url}")
        return "Producto Desconocido"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        page = requests.get(url, headers=headers, timeout=15)
        page.raise_for_status()
        if BeautifulSoup is None:
            return "Producto Swappa"
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

# --- Lógica de Scraping con Contrato ScrapeResult ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str) -> ScrapeResult:
    if not is_valid_swappa_url(url):
        logger.warning(f"URL no válida en scrape_swappa: {url}")
        return ScrapeResult(status=ScrapeStatus.ERROR, error_message="URL no válida o no permitida.")

    logger.info(f"Iniciando búsqueda ESTRICTA para {device_name} en URL base: {url}")
    driver = None
    all_found_devices = []
    processed_links = set()

    try:
        driver = create_uc_driver()

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
            anuncios = soup.find_all("div", class_="xui_card_listing")

            found_on_page = 0

            for anuncio in anuncios:
                try:
                    # 1. ENLACE
                    link_tag = anuncio.find('a', href=re.compile(r'/listing/view/'))
                    if not link_tag:
                        price_div = anuncio.find("div", class_="price")
                        if price_div:
                            link_tag = price_div.find("a", href=True)

                    if not link_tag:
                        continue

                    href = link_tag['href']
                    link = "https://swappa.com" + href if not href.startswith("http") else href

                    if link in processed_links:
                        continue
                    processed_links.add(link)

                    # 2. PRECIO
                    precio_tag = anuncio.find('span', itemprop='price')
                    if not precio_tag:
                        continue
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
                            if match:
                                bateria = int(match.group(1))

                        # C. Almacenamiento y Color
                        for attr in attrs_div.find_all("span", class_="attr"):
                            if "color_battery" in attr.get("class", []):
                                continue
                            txt = attr.get_text(" ", strip=True).strip()
                            txt_lower = txt.lower()

                            # Ignorar estado conocido o garantía
                            if txt == estado:
                                continue
                            if "warranty" in txt_lower:
                                continue

                            if "gb" in txt_lower or "tb" in txt_lower:
                                almacenamiento = txt
                            # Filtro para evitar que Modelo o Unlocked se marque como Color
                            elif "unlocked" in txt_lower:
                                continue
                            # Regex mejorada para ignorar modelos con guión (ej: A2482, SM-S908U, SM-S918B)
                            elif re.search(r'^[a-z]{1,2}-?[a-z]?\d{3,4}[a-z0-9]*', txt_lower) or re.search(r'^\d+$', txt_lower):
                                continue
                            else:
                                color = txt

                    if estado == "N/A":
                        continue

                    # --- FILTRADO ESTRICTO ---
                    # 1. Filtro Precio
                    if precio > max_price:
                        continue

                    # 2. Filtro Condición
                    if desired_condition.lower() not in estado.lower():
                        continue

                    # 3. Filtro Batería ESTRICTO
                    if min_battery > 0:
                        if bateria == 0 or bateria < min_battery:
                            continue

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

        # --- GENERACIÓN DE MENSAJE Y RESULTADO ---
        if all_found_devices:
            all_found_devices.sort(key=lambda x: x['precio'])
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

            return ScrapeResult(status=ScrapeStatus.SUCCESS, items=items_to_show, formatted_message=mensaje_final)
        else:
            return ScrapeResult(status=ScrapeStatus.EMPTY, items=[])

    except Exception as e:
        logger.error(f"Error durante scraping de {device_name}: {e}")
        return ScrapeResult(status=ScrapeStatus.ERROR, error_message=str(e))
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e_quit:
                logger.debug(f"Error cerrando driver: {e_quit}")

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
        "1. URL de Swappa (ej. https://swappa.com/listings/...)\n"
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
        await update.message.reply_html("⚠️ Formato incorrecto. Usa /help para ver las instrucciones.")
        return
    try:
        url, max_price, condition, min_battery, frequency_str = args

        if not is_valid_swappa_url(url):
            await update.message.reply_html("⚠️ La URL debe ser un enlace válido de Swappa comenzando con <code>https://swappa.com/</code>.")
            return

        # Validación estricta de frecuencia
        num_match = re.search(r'^\d+', frequency_str)
        unit_match = re.search(r'[a-zA-Z]+$', frequency_str)
        if not num_match or not unit_match or unit_match.group(0).lower() not in ['h', 'm']:
            await update.message.reply_html("⚠️ Formato de tiempo inválido. Usa por ejemplo <b>30m</b> (30 minutos) o <b>2h</b> (2 horas).")
            return

        num = int(num_match.group(0))
        unit = unit_match.group(0).lower()
        if num <= 0:
            await update.message.reply_html("⚠️ El tiempo debe ser mayor a 0.")
            return

        seconds = num * 3600 if unit == 'h' else num * 60
        display_freq = f"{num} {'horas' if unit == 'h' else 'minutos'}"

        await update.message.reply_text("🤖 Obteniendo información...")
        device_name = await asyncio.to_thread(get_device_name, url)

        max_price_f = float(max_price)
        min_battery_i = int(min_battery)

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

        # Ejecución controlada con semáforo de concurrencia
        async with SCRAPE_SEMAPHORE:
            res: ScrapeResult = await asyncio.to_thread(scrape_swappa, url, max_price_f, condition, min_battery_i, device_name)

        if res.status == ScrapeStatus.SUCCESS and res.formatted_message:
            await update.message.reply_html(res.formatted_message, disable_web_page_preview=True)
        elif res.status == ScrapeStatus.EMPTY:
            await update.message.reply_text("😥 No se encontraron ofertas que cumplan tus criterios en este momento.")
        else:
            logger.error(f"Fallo en búsqueda inmediata /remind para {device_name}: {res.error_message}")
            await update.message.reply_text("⚠️ No fue posible consultar las ofertas de Swappa en este momento. El recordatorio quedó programado y se ejecutará automáticamente.")

    except ValueError:
        await update.message.reply_html("❌ Precio o batería inválidos. Asegúrate de ingresar números válidos.")
    except Exception as e:
        logger.error(f"Error en remind: {e}")
        await update.message.reply_html("❌ Error procesando los datos. Verifica el formato con /help.")

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

    await update.message.reply_text("✅ Recordatorio eliminado." if cnt > 0 else "❌ Recordatorio no encontrado.")

# --- Ejecución del Scheduler ---
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
            # Ejecución protegida con semáforo
            async with SCRAPE_SEMAPHORE:
                res: ScrapeResult = await asyncio.to_thread(
                    scrape_swappa, r['url'], r['max_price'], r['condition'], r['min_battery'], r['device_name']
                )

            c2 = db_connect()
            with c2.cursor() as cur2:
                cur2.execute("UPDATE reminders SET last_checked = %s WHERE id = %s", (now, r['id']))
                c2.commit()
            c2.close()

            # Solo ScrapeStatus.SUCCESS puede despachar alertas
            if res.status == ScrapeStatus.SUCCESS and res.formatted_message:
                try:
                    await bot.send_message(chat_id=r['chat_id'], text=res.formatted_message, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Error telegram enviando notificación: {e}")
            elif res.status == ScrapeStatus.ERROR:
                logger.error(f"Scheduler falló al procesar {r.get('device_name')}: {res.error_message}")
            # ScrapeStatus.EMPTY no genera ninguna alerta

def run_bot_polling():
    if not DATABASE_URL or not TELEGRAM_TOKEN:
        logger.critical("DATABASE_URL o TELEGRAM_TOKEN no están configurados. Abortando inicio del bot.")
        return
    setup_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("myreminders", my_reminders))
    app.add_handler(CommandHandler("stopreminder", stop_reminder))
    logger.info("Iniciando long-polling de Telegram Bot...")
    app.run_polling()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'run_scheduler_check':
        asyncio.run(run_scheduler_check())
    else:
        run_bot_polling()
