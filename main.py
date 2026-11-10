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

        # Tabla de deduplicación persistente de anuncios notificados (M10.5)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notified_listings (
                id SERIAL PRIMARY KEY,
                reminder_id VARCHAR(255) NOT NULL,
                listing_key VARCHAR(255) NOT NULL,
                notified_at BIGINT NOT NULL,
                price REAL,
                UNIQUE (reminder_id, listing_key)
            );
        """)
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notified_listings_lookup ON notified_listings (reminder_id, listing_key);")
        except Exception:
            conn.rollback()

        conn.commit()
    conn.close()

def filter_unnotified_listings(reminder_id: str, items: list[dict]) -> list[dict]:
    """
    Filtra los anuncios ya notificados previamente para este recordatorio.
    Retorna únicamente los anuncios nuevos que no han sido despachados.
    """
    if not items or psycopg2 is None or not DATABASE_URL:
        return items
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            keys = [item.get('listing_id') or item.get('link') for item in items]
            cur.execute("""
                SELECT listing_key FROM notified_listings
                WHERE reminder_id = %s AND listing_key = ANY(%s);
            """, (reminder_id, keys))
            already_notified = {row[0] for row in cur.fetchall()}
        conn.close()
        return [item for item in items if (item.get('listing_id') or item.get('link')) not in already_notified]
    except Exception as e:
        logger.warning(f"No se pudo consultar notified_listings en DB ({e}). Continuando sin filtro.")
        return items

def record_notified_listings(reminder_id: str, items: list[dict]):
    """
    Registra de manera idempotente los anuncios notificados tras confirmación de Telegram.
    """
    if not items or psycopg2 is None or not DATABASE_URL:
        return
    now = int(time.time())
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            for item in items:
                key = item.get('listing_id') or item.get('link')
                price = item.get('precio', 0.0)
                cur.execute("""
                    INSERT INTO notified_listings (reminder_id, listing_key, notified_at, price)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (reminder_id, listing_key) DO NOTHING;
                """, (reminder_id, key, now, price))
            conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"No se pudieron registrar anuncios notificados en DB ({e}).")

def claim_due_reminders(batch_limit: int = 10, tolerance_seconds: int = 60) -> list[dict]:
    """
    Reclama atómicamente recordatorios vencidos desde PostgreSQL utilizando SELECT FOR UPDATE SKIP LOCKED
    y actualizando last_checked en la misma transacción para evitar ejecuciones concurrentes entre dynos.
    """
    if psycopg2 is None or not DATABASE_URL:
        return []
    conn = db_connect()
    claimed = []
    now = int(time.time())
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name
                FROM reminders
                WHERE (%s - last_checked) >= (frequency_seconds - %s)
                ORDER BY last_checked ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED;
            """, (now, tolerance_seconds, batch_limit))
            rows = cur.fetchall()
            if rows:
                row_ids = [r['id'] for r in rows]
                cur.execute(
                    "UPDATE reminders SET last_checked = %s WHERE id = ANY(%s);",
                    (now, row_ids)
                )
                conn.commit()
                claimed = list(rows)
    except Exception as e:
        logger.error(f"Error reclamando recordatorios vencidos en DB: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return claimed

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

# --- Inferencia y Obtención del Nombre del Producto ---
KNOWN_BRANDS_AND_TERMS = {
    "apple": "Apple", "iphone": "iPhone", "ipad": "iPad", "ipod": "iPod",
    "imac": "iMac", "macbook": "MacBook", "airpods": "AirPods", "airpod": "AirPod",
    "samsung": "Samsung", "galaxy": "Galaxy", "google": "Google", "pixel": "Pixel",
    "oneplus": "OnePlus", "motorola": "Motorola", "moto": "Moto", "nintendo": "Nintendo",
    "switch": "Switch", "sony": "Sony", "playstation": "PlayStation", "microsoft": "Microsoft",
    "xbox": "Xbox", "surface": "Surface", "lg": "LG", "asus": "ASUS", "lenovo": "Lenovo",
    "dell": "Dell", "hp": "HP", "pro": "Pro", "max": "Max", "mini": "Mini", "plus": "Plus",
    "ultra": "Ultra", "fe": "FE", "se": "SE", "xr": "XR", "xs": "XS", "xl": "XL",
    "lte": "LTE", "5g": "5G", "4g": "4G", "oled": "OLED", "ps4": "PS4", "ps5": "PS5", "vr": "VR",
}

def extract_device_name_from_url(url: str) -> str | None:
    """
    Infiere el nombre amigable del dispositivo a partir del slug de la URL de Swappa.
    Permite identificar productos (ej. 'Apple iPhone 13') con 0ms de latencia y sin
    exponerse a bloqueos HTTP 403 de Cloudflare.
    """
    try:
        path = urlparse(url).path.strip('/')
        match = re.search(r'(?:listings|buy)/([a-zA-Z0-9\-]+)', path)
        if not match:
            return None
        slug = match.group(1).lower()
        parts = [p for p in slug.split('-') if p]
        if not parts:
            return None
        formatted = []
        for p in parts:
            if p in KNOWN_BRANDS_AND_TERMS:
                formatted.append(KNOWN_BRANDS_AND_TERMS[p])
            elif re.match(r'^(?:[ivx]+)$', p):
                formatted.append(p.upper())
            elif re.match(r'^[a-z]\d+$', p):
                formatted.append(p[0].upper() + p[1:])
            else:
                formatted.append(p.capitalize())
        return ' '.join(formatted)
    except Exception:
        return None

def get_device_name(url: str) -> str:
    """
    Obtiene el nombre del producto combinando inferencia de slug y metadatos de página.
    Si la consulta HTTP falla (por ejemplo por HTTP 403 de Cloudflare o timeout),
    se utiliza prioritariamente el nombre inferido del slug, garantizando que nunca
    se retorne 'Producto Desconocido' para URLs válidas con slug identificable.
    """
    if not is_valid_swappa_url(url):
        logger.warning(f"URL rechazada por validación SSRF: {url}")
        return "Producto Desconocido"

    inferred_name = extract_device_name_from_url(url)

    # Intentar obtener metadatos más específicos de la página web si es posible
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        page = requests.get(url, headers=headers, timeout=5)
        page.raise_for_status()
        if BeautifulSoup is not None:
            soup = BeautifulSoup(page.content, 'html.parser')
            name_link = soup.find('a', href=re.compile(r'^/listings/'), title=re.compile(r'^Buy '))
            if name_link:
                return name_link.get_text(strip=True)
            h1 = soup.find('h1')
            if h1:
                return h1.get_text(strip=True).replace(' on Swappa', '')
    except Exception as e:
        logger.info(f"Consulta HTTP para nombre de dispositivo en {url} no disponible ({e}). Usando inferencia local.")

    if inferred_name:
        return inferred_name

    return "Producto Swappa"

# --- Lógica de Scraping con Contrato ScrapeResult ---
def scrape_swappa(url: str, max_price: float, desired_condition: str, min_battery: int, device_name: str, driver=None) -> ScrapeResult:
    if not is_valid_swappa_url(url):
        logger.warning(f"URL no válida en scrape_swappa: {url}")
        return ScrapeResult(status=ScrapeStatus.ERROR, error_message="URL no válida o no permitida.")

    logger.info(f"Iniciando búsqueda ESTRICTA para {device_name} en URL base: {url}")
    external_driver = driver is not None
    all_found_devices = []
    processed_links = set()

    try:
        if driver is None:
            driver = create_uc_driver()

        card_selector = ".xui_card_listing, [class*='card_listing'], .listing-card, [data-listing]"
        empty_selector = ".no-listings, #listings_none, [data-testid='no-results'], div.empty-state"
        combined_selector = f"{card_selector}, {empty_selector}"

        for page_num in range(1, 4):
            if page_num == 1:
                page_url = url
            else:
                separator = '&' if '?' in url else '?'
                page_url = f"{url}{separator}page={page_num}"

            logger.info(f"Revisando página {page_num}: {page_url}")
            driver.get(page_url)

            # 1. Comprobar pantallas de bloqueo / Cloudflare Turnstile / Bot Challenge
            page_title = driver.title or ""
            if any(marker in page_title.lower() for marker in ["just a moment...", "attention required", "cloudflare", "security check"]):
                logger.warning(f"Desafío anti-bot / Cloudflare detectado en '{page_title}' para {url}")
                return ScrapeResult(
                    status=ScrapeStatus.ERROR,
                    error_message=f"Página bloqueada por verificación Cloudflare / Anti-bot ('{page_title}')."
                )

            # 2. Espera explícita para aparición de tarjetas o estado vacío
            timed_out = False
            if WebDriverWait is not None and EC is not None and By is not None:
                try:
                    wait = WebDriverWait(driver, 15)
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, combined_selector)))
                except Exception:
                    timed_out = True

            if timed_out:
                if page_num == 1:
                    # En página 1, timeout sin elementos es un fallo técnico de carga o red, NUNCA resultado vacío
                    logger.error(f"Timeout de 15s esperando publicaciones en página 1 de {device_name}. Título actual: '{driver.title}'")
                    return ScrapeResult(
                        status=ScrapeStatus.ERROR,
                        error_message=f"Tiempo de espera agotado (timeout) cargando publicaciones de {device_name} en Swappa."
                    )
                else:
                    # En páginas 2 o 3, timeout representa simplemente el fin del catálogo
                    logger.info(f"Fin de resultados alcanzado en página {page_num}.")
                    break

            if BeautifulSoup is None:
                return ScrapeResult(status=ScrapeStatus.ERROR, error_message="BeautifulSoup no está instalado.")

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            anuncios = soup.select(card_selector)

            # Verificar si la página explícitamente reporta estado vacío
            if not anuncios:
                body_text = soup.get_text().lower()
                has_empty_indicator = any(phrase in body_text for phrase in [
                    "no listings found", "no active listings", "there are currently no listings",
                    "0 listings", "no matching listings", "no items found"
                ]) or bool(soup.select(empty_selector))

                if has_empty_indicator and page_num == 1:
                    logger.info(f"Página 1 cargó exitosamente pero Swappa confirma 0 publicaciones para {device_name}.")
                    return ScrapeResult(status=ScrapeStatus.EMPTY, items=[])
                elif page_num == 1:
                    logger.warning(f"No se detectaron tarjetas ni indicador vacío en página 1 para {device_name}.")
                    return ScrapeResult(
                        status=ScrapeStatus.ERROR,
                        error_message=f"Estructura DOM de Swappa no reconocida para {device_name}."
                    )
                else:
                    break

            found_on_page = 0

            for anuncio in anuncios:
                try:
                    # 1. ENLACE Y LISTING ID
                    link_tag = anuncio.find('a', href=re.compile(r'/listing/view/'))
                    if not link_tag:
                        price_div = anuncio.find("div", class_="price")
                        if price_div:
                            link_tag = price_div.find("a", href=True)
                    if not link_tag:
                        link_tag = anuncio.find('a', href=re.compile(r'/listing/'))

                    if not link_tag:
                        continue

                    href = link_tag.get('href', '')
                    link = "https://swappa.com" + href if not href.startswith("http") else href

                    if link in processed_links:
                        continue
                    processed_links.add(link)

                    # Extraer ID estable de Swappa (ej. 'ABCD1234')
                    id_match = re.search(r'/listing/view/([a-zA-Z0-9]+)', href)
                    listing_id = id_match.group(1) if id_match else link

                    # 2. PRECIO
                    precio_tag = anuncio.find('span', itemprop='price')
                    if not precio_tag:
                        precio_tag = anuncio.find(class_=re.compile(r'price'))
                    if not precio_tag:
                        continue
                    precio_text = precio_tag.text.strip().replace(',', '').replace('$', '')
                    precio_match = re.search(r'[\d.]+', precio_text)
                    if not precio_match:
                        continue
                    precio = float(precio_match.group(0))

                    # 3. VENDEDOR
                    vendedor_tag = anuncio.find('div', class_=re.compile(r'seller'))
                    vendedor = vendedor_tag.text.strip() if vendedor_tag else "N/A"

                    # 4. CONDICIÓN Y ATRIBUTOS
                    estado = "N/A"
                    bateria = 0
                    color = "N/A"
                    almacenamiento = "N/A"

                    attrs_div = anuncio.find("div", class_=re.compile(r'attrs|details'))
                    if attrs_div:
                        # A. Condición (Prioridad Meta Tag)
                        cond_meta = attrs_div.find("meta", itemprop="itemCondition")
                        if cond_meta and cond_meta.parent:
                            estado = cond_meta.parent.get_text(strip=True)
                        else:
                            for sp in attrs_div.find_all("span", class_="attr"):
                                txt = sp.get_text(" ", strip=True)
                                if txt in ["Mint", "Good", "Fair", "New", "Open Box"]:
                                    estado = txt
                                    break

                        # B. Batería
                        batt_span = attrs_div.find("span", class_=re.compile(r'battery'))
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

                            if txt == estado or "warranty" in txt_lower or "unlocked" in txt_lower:
                                continue

                            if "gb" in txt_lower or "tb" in txt_lower:
                                almacenamiento = txt
                            elif re.search(r'^[a-z]{1,2}-?[a-z]?\d{3,4}[a-z0-9]*', txt_lower) or re.search(r'^\d+$', txt_lower):
                                continue
                            else:
                                color = txt

                    if estado == "N/A":
                        continue

                    # --- FILTRADO ESTRICTO ---
                    if precio > max_price:
                        continue

                    if desired_condition.lower() not in estado.lower():
                        continue

                    if min_battery > 0:
                        if bateria == 0 or bateria < min_battery:
                            continue

                    all_found_devices.append({
                        "listing_id": listing_id,
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
        if driver and not external_driver:
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
    if not args or len(args) != 5:
        await update.message.reply_html("⚠️ Formato incorrecto. Usa /help para ver las instrucciones.")
        return
    try:
        url, max_price, condition, min_battery, frequency_str = args

        if not is_valid_swappa_url(url):
            await update.message.reply_html("⚠️ La URL debe ser un enlace válido de Swappa comenzando con <code>https://swappa.com/</code>.")
            return

        # Validación estricta de precio
        try:
            max_price_f = float(max_price)
            if max_price_f <= 0:
                await update.message.reply_html("⚠️ El precio máximo debe ser un valor positivo mayor a 0.")
                return
        except ValueError:
            await update.message.reply_html("❌ Precio inválido. Asegúrate de ingresar un número válido (ej. 340).")
            return

        # Validación estricta de batería
        try:
            min_battery_i = int(min_battery)
            if min_battery_i < 0 or min_battery_i > 100:
                await update.message.reply_html("⚠️ La batería mínima debe estar entre 0 y 100 (usa 0 si no deseas filtrar por batería).")
                return
        except ValueError:
            await update.message.reply_html("❌ Batería inválida. Asegúrate de ingresar un número entero entre 0 y 100.")
            return

        # Validación estricta de frecuencia
        freq_match = re.search(r'^(\d+)([hmHM])$', frequency_str.strip())
        if not freq_match:
            await update.message.reply_html("⚠️ Formato de tiempo inválido. Usa por ejemplo <b>15m</b> (15 minutos) o <b>2h</b> (2 horas).")
            return

        num = int(freq_match.group(1))
        unit = freq_match.group(2).lower()
        if num <= 0:
            await update.message.reply_html("⚠️ El tiempo debe ser mayor a 0.")
            return

        seconds = num * 3600 if unit == 'h' else num * 60
        display_freq = f"{num} {'horas' if unit == 'h' else 'minutos'}"

        await update.message.reply_text("🤖 Obteniendo información...")
        device_name = await asyncio.to_thread(get_device_name, url)

        reminder_id = f"reminder_{chat_id}_{int(time.time())}"

        # Persistencia atómica en PostgreSQL previa a la confirmación
        try:
            conn = db_connect()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO reminders (chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (chat_id, reminder_id, url, max_price_f, condition, min_battery_i, seconds, int(time.time()), device_name)
                )
                conn.commit()
            conn.close()
        except Exception as db_err:
            logger.error(f"Error persistiendo recordatorio en DB: {db_err}")
            await update.message.reply_html("❌ No fue posible guardar el recordatorio en la base de datos. Inténtalo más tarde.")
            return

        # Confirmación emitida ÚNICAMENTE tras inserción exitosa en base de datos
        await update.message.reply_html(
            f"✅ <b>Recordatorio configurado para {device_name}.</b>\nFrecuencia: {display_freq}.\n\n<i>Buscando ahora...</i> 🔍"
        )

        # Ejecución controlada con semáforo de concurrencia
        async with SCRAPE_SEMAPHORE:
            res: ScrapeResult = await asyncio.to_thread(scrape_swappa, url, max_price_f, condition, min_battery_i, device_name)

        if res.status == ScrapeStatus.SUCCESS and res.items:
            # Filtrar ofertas para deduplicación
            new_items = await asyncio.to_thread(filter_unnotified_listings, reminder_id, res.items)
            if new_items:
                await update.message.reply_html(res.formatted_message, disable_web_page_preview=True)
                # Registrar como notificadas solo tras confirmación de envío
                await asyncio.to_thread(record_notified_listings, reminder_id, new_items)
            else:
                await update.message.reply_text("ℹ️ Las ofertas encontradas ya fueron notificadas previamente.")
        elif res.status == ScrapeStatus.EMPTY:
            await update.message.reply_text("😥 No se encontraron ofertas que cumplan tus criterios en este momento.")
        else:
            logger.error(f"Fallo en búsqueda inmediata /remind para {device_name}: {res.error_message}")
            await update.message.reply_text(
                "⚠️ No fue posible consultar las ofertas de Swappa en este momento (tiempo de espera agotado o bloqueo temporal). "
                "El recordatorio quedó guardado y se ejecutará automáticamente en el próximo intervalo."
            )

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

    rem_id = context.args[0].strip()
    chat_id = str(update.message.chat_id)
    conn = db_connect()
    with conn.cursor() as cur:
        # Limpiar deduplicación asociada
        try:
            cur.execute("DELETE FROM notified_listings WHERE reminder_id = %s", (rem_id,))
        except Exception:
            pass
        cur.execute("DELETE FROM reminders WHERE reminder_id = %s AND chat_id = %s", (rem_id, chat_id))
        cnt = cur.rowcount
        conn.commit()
    conn.close()

    await update.message.reply_text("✅ Recordatorio eliminado." if cnt > 0 else "❌ Recordatorio no encontrado.")

# --- Ejecución del Scheduler y Despachador Periódico ---
async def run_scheduler_check(bot=None):
    """
    Ejecuta la revisión periódica de recordatorios vencidos.
    Reclama atómicamente los recordatorios con SELECT FOR UPDATE SKIP LOCKED
    y reutiliza una única sesión de Chrome para procesar todo el lote,
    evitando Error R14 de memoria y asegurando ejecución oportuna.
    """
    due_reminders = await asyncio.to_thread(claim_due_reminders, 10, 60)
    if not due_reminders:
        logger.debug("No hay recordatorios pendientes en este ciclo.")
        return

    logger.info(f"Procesando lote de {len(due_reminders)} recordatorios vencidos en scheduler...")

    if bot is None:
        if Application is None or not TELEGRAM_TOKEN:
            logger.error("No se puede inicializar Telegram Bot para scheduler.")
            return
        bot = Application.builder().token(TELEGRAM_TOKEN).build().bot

    driver = None
    try:
        # Abrir UNA SOLA sesión de Chrome para todo el lote
        try:
            driver = await asyncio.to_thread(create_uc_driver)
        except Exception as drv_err:
            logger.error(f"No se pudo inicializar Chrome para el lote de scheduler: {drv_err}")
            return

        for r in due_reminders:
            dev_name = r.get('device_name') or 'Producto'
            rem_id = r['reminder_id']
            try:
                res: ScrapeResult = await asyncio.to_thread(
                    scrape_swappa, r['url'], r['max_price'], r['condition'], r['min_battery'], dev_name, driver
                )
            except Exception as scrape_err:
                logger.error(f"Scheduler falló al procesar {dev_name}: {scrape_err}")
                continue

            if res.status == ScrapeStatus.SUCCESS and res.items:
                # Filtrar anuncios ya notificados previamente
                new_items = await asyncio.to_thread(filter_unnotified_listings, rem_id, res.items)
                if new_items:
                    try:
                        await bot.send_message(
                            chat_id=r['chat_id'],
                            text=res.formatted_message,
                            parse_mode='HTML',
                            disable_web_page_preview=True
                        )
                        # Registrar como notificados tras confirmación exitosa de Telegram
                        await asyncio.to_thread(record_notified_listings, rem_id, new_items)
                        logger.info(f"Notificación enviada a {r['chat_id']} con {len(new_items)} nuevas ofertas de {dev_name}.")
                    except Exception as tg_err:
                        logger.error(f"Error de Telegram enviando notificación de {dev_name}: {tg_err}")
                else:
                    logger.info(f"Ofertas encontradas para {dev_name} ya fueron notificadas previamente. Omitiendo mensaje.")
            elif res.status == ScrapeStatus.ERROR:
                logger.error(f"Scheduler: Scraper retornó ERROR para {dev_name}: {res.error_message}")
            else:
                logger.info(f"Scheduler: Sin ofertas coincidentes para {dev_name} (EMPTY).")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e_quit:
                logger.debug(f"Error cerrando driver de lote: {e_quit}")

async def periodic_scheduler_worker(bot):
    """
    Bucle en segundo plano ejecutado dentro del worker dyno.
    Comprueba recordatorios vencidos cada 60 segundos, permitiendo que recordatorios
    de 15m, 30m, etc. se ejecuten con precisión temporal sin depender del ciclo de 10 min de Heroku.
    """
    logger.info("Iniciando despachador periódico interno en worker...")
    await asyncio.sleep(30)  # Espera inicial tras arranque del dyno
    while True:
        try:
            await run_scheduler_check(bot=bot)
        except asyncio.CancelledError:
            logger.info("Despachador periódico detenido.")
            break
        except Exception as e:
            logger.error(f"Error inesperado en despachador periódico interno: {e}")
        await asyncio.sleep(60)

async def post_init_callback(application: Application):
    """Callback invocado por python-telegram-bot al iniciar el polling."""
    asyncio.create_task(periodic_scheduler_worker(application.bot))

def run_bot_polling():
    if not DATABASE_URL or not TELEGRAM_TOKEN:
        logger.critical("DATABASE_URL o TELEGRAM_TOKEN no están configurados. Abortando inicio del bot.")
        return
    setup_database()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init_callback).build()
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
