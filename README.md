# Swappa Monitor Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.11.8-blue.svg)](runtime.txt)
[![Platform](https://img.shields.io/badge/Platform-Heroku%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)]()
[![Code Style: Python](https://img.shields.io/badge/Architecture-AsyncIO%20%2B%20Selenium-brightgreen.svg)]()

An automated marketplace monitoring and alert bot for [Swappa](https://swappa.com), built with Python, `undetected-chromedriver`, `python-telegram-bot`, and PostgreSQL. It delivers real-time Telegram notifications when qualifying listings match user-defined price, cosmetic condition, and battery health criteria.

---

## Overview

Swappa is a curated peer-to-peer marketplace for consumer electronics. High-demand items—such as iPhones, Google Pixel smartphones, and MacBooks—often sell within minutes when listed at competitive prices or with high battery health (>90%).

Manually monitoring Swappa requires continuous page refreshing and tedious inspection of individual device cards. **Swappa Monitor Bot** automates this surveillance 24/7. Users configure custom search alerts directly through Telegram. The bot continuously inspects marketplace listings, applies strict multi-parameter filters (price, condition, battery health), and sends instantaneous alerts with direct purchase links.

---

## Features

* **Anti-Bot Resilient Scraping:** Leverages `undetected-chromedriver` in headless mode to bypass Cloudflare perimeter challenges and render dynamic JavaScript content.
* **Slug-First Product Inference:** Extracts canonical device titles (e.g. `apple-iphone-13` → `Apple iPhone 13`) with 0ms latency, preventing Cloudflare HTTP 403 blocks from yielding generic titles.
* **Dynamic Chrome Versioning:** Inspects the host environment's Google Chrome binary at runtime to synchronize Chromedriver versions dynamically.
* **Strict Multi-Criteria Filtering:**
  * **Price Threshold:** Discards any listing exceeding the user's upper limit.
  * **Condition Matching:** Filters by cosmetic grade (`Mint`, `Good`, `Fair`, `New`, `Open Box`).
  * **Strict Battery Health:** Filters devices by reported battery percentage (e.g., `>= 90%`). Listings omitting battery percentages are strictly excluded when battery monitoring is enabled.
* **Persistent Listing Deduplication:** Employs a dedicated `notified_listings` PostgreSQL table to prevent repetitive alerts for the same offer across check cycles.
* **Memory & Concurrency Control:** Reuses a single Chrome instance per scheduler batch and uses `SELECT ... FOR UPDATE SKIP LOCKED` to prevent duplicate dyno execution and eliminate Heroku `Error R14` memory spikes.
* **Multi-Page Pagination:** Scrapes across up to 3 pages per target URL with anti-duplicate link tracking.
* **Interactive Telegram Interface:** Full command suite for registering, listing, and canceling monitors.
* **Dual Execution & Hybrid Dispatcher:** Continuous polling with an internal 60s asynchronous dispatcher in the worker dyno, plus support for Heroku Scheduler batch execution as high-availability watchdog.
* **Persistent Storage:** Retains alert configurations and execution timestamps in PostgreSQL with SSL encryption.

---

## Architecture

The application is structured around a monolithic async controller (`main.py`) that coordinates user interactions from Telegram, manages state in PostgreSQL, and offloads browser automation to dedicated worker threads.

### High-Level Architectural Diagram

```mermaid
graph TD
    subgraph Telegram_Interface["Telegram Interface"]
        User["Telegram User"]
        BotAPI["Telegram Bot API"]
    end

    subgraph Core_Engine["Swappa Monitor Bot Core (main.py)"]
        subgraph Process_Worker["Worker Mode: run_bot_polling()"]
            TGApp["python-telegram-bot Application"]
            Handlers["Command Handlers<br/>(/start, /help, /remind, /myreminders, /stopreminder)"]
        end

        subgraph Process_Scheduler["Scheduler Mode: run_scheduler_check()"]
            BatchJob["Interval Check Evaluator"]
        end

        subgraph Pipeline["Data Extraction & Processing"]
            Resolver["get_device_name()<br/>(requests + bs4)"]
            VersionDetector["get_chrome_version()<br/>(subprocess check_output)"]
            ScraperEngine["scrape_swappa()<br/>(undetected-chromedriver)"]
            FilterRules["Strict Filter Pipeline<br/>(Price, Condition, Battery %)"]
        end

        subgraph Data_Access["Data Access Layer"]
            DB["db_connect() & setup_database()<br/>(psycopg2 raw SQL)"]
        end
    end

    subgraph External["External Systems"]
        Postgres[("PostgreSQL Database<br/>(reminders table)")]
        Swappa["Swappa Marketplace (swappa.com)"]
    end

    User <-->|Commands & Alerts| BotAPI
    BotAPI <-->|Long Polling / Push| TGApp
    TGApp --> Handlers
    Handlers --> DB
    Handlers --> Resolver
    Handlers --> ScraperEngine
    BatchJob --> DB
    BatchJob --> ScraperEngine
    BatchJob --> BotAPI
    ScraperEngine --> VersionDetector
    Resolver --> Swappa
    ScraperEngine --> Swappa
    ScraperEngine --> FilterRules
    DB <-->|SSL Required| Postgres
```

---

## Technology Stack

| Category | Technology | Details |
| :--- | :--- | :--- |
| **Language** | Python 3.11.8 | Pinned in `runtime.txt` for Heroku runtime compatibility |
| **Messaging Framework** | `python-telegram-bot` (v20+) | Async coroutine handlers, long-polling Telegram event loop |
| **Browser Automation** | `undetected-chromedriver` & `selenium` | Headless Chrome automation bypassing anti-bot challenges |
| **HTML Parsing** | `beautifulsoup4` | Parsing DOM nodes, metadata cards, attributes, and breadcrumbs |
| **HTTP Client** | `requests` | Fast synchronous metadata queries for product titles |
| **Database** | PostgreSQL (`psycopg2-binary`) | Relational persistence with raw SQL and `RealDictCursor` |
| **Deployment / Infra** | Heroku | Worker dyno configuration via `Procfile`, compatible with Heroku Scheduler |

---

## How It Works

```mermaid
sequenceDiagram
    autonumber
    actor User as Telegram User
    participant Bot as Telegram Bot
    participant Worker as Threadpool Worker
    participant DB as PostgreSQL
    participant Swappa as Swappa.com

    User->>Bot: /remind [url] [max_price] [condition] [battery] [frequency]
    Bot->>Worker: Resolve product title (requests)
    Worker->>Swappa: GET /listings/...
    Swappa-->>Worker: HTML Breadcrumb
    Worker-->>Bot: Product Name (e.g., Apple iPhone 15)
    Bot->>DB: INSERT INTO reminders (chat_id, reminder_id, url, ...)
    Bot-->>User: "Recordatorio configurado. Buscando ahora..."
    Bot->>Worker: Run scrape_swappa() via asyncio.to_thread
    Worker->>Swappa: Load headless Chrome (Pages 1-3)
    Swappa-->>Worker: Listing cards (div.xui_card_listing)
    Worker->>Worker: Apply price, condition & battery filter
    Worker-->>Bot: List of matching offers
    Bot-->>User: Formatted HTML message with top 15 deals
```

### Detailed Execution Pipeline

1. **Product Name Resolution (`get_device_name`):**
   * Issues a synchronous HTTP GET request with a desktop `User-Agent`.
   * Parses the breadcrumb link (`<a href="^/listings/" title="^Buy ">`) or `<h1>` header.
2. **Dynamic Scraping Engine (`scrape_swappa`):**
   * Discovers the major Chrome version on the system via `get_chrome_version()` (running `google-chrome --version`).
   * Initializes `uc.Chrome` in headless mode (`--no-sandbox`, `--disable-dev-shm-usage`, `--window-size=1920,1080`).
   * Iterates through pages 1 to 3, waiting up to 15 seconds per page for `.xui_card_listing` DOM nodes.
3. **Data Extraction & Heuristics:**
   * **Listing URL:** Extracted from `<a href="/listing/view/...">` and normalized with `https://swappa.com`.
   * **Price:** Parsed from `<span itemprop="price">`.
   * **Condition:** Parsed from `<meta itemprop="itemCondition">` or badge spans (`Mint`, `Good`, `Fair`, `New`, `Open Box`).
   * **Battery Health:** Extracted via regex `r'(\d+)%'` from `<span class="color_battery">`.
   * **Storage & Color:** Parsed by inspecting attribute spans, filtering out carrier ("unlocked") and hardware model strings.
4. **Strict Filtering:**
   * Price must be `<= max_price`.
   * Condition string must match `desired_condition`.
   * If `min_battery > 0`, listings with no reported battery percentage or battery `< min_battery` are rejected.
5. **Notification Delivery:**
   * Sorts qualifying offers ascending by price.
   * Limits output to the top 15 results to prevent exceeding Telegram message size limits (4096 characters).
   * Sends formatted HTML with clickable links directly to the chat.

---

## Project Structure

```text
swappa-monitor-bot/
├── tests/                             # Automated test suite (34 unit, regression & e2e tests)
│   ├── test_chrome_resolver.py        # Chrome version & path resolution tests
│   ├── test_contracts_and_consumers.py# ScrapeResult contracts & error isolation tests
│   ├── test_regression_parsers.py     # Regex model & frequency validation tests
│   ├── test_security_and_logging.py   # Token redaction & SSRF validation tests
│   ├── test_uc_factory.py             # undetected-chromedriver factory tests
│   ├── test_uc_live_integration.py    # UC live integration test stub
│   ├── test_m10_product_and_parser.py # Slug-first inference & parameter validation tests
│   ├── test_m10_scraper_resilience.py # DOM selector robustness & timeout tests
│   ├── test_m10_scheduler_and_dedup.py# Concurrency & listing deduplication tests
│   └── test_m10_e2e_lifecycle.py      # End-to-end reminder lifecycle pipeline test
├── LICENSE                            # MIT License (Mauricio Perdomo, 2025)
├── Procfile                           # Heroku process declaration (worker dyno)
├── requirements.txt                   # Declared Python dependencies
├── runtime.txt                        # Target Python runtime specification (python-3.11.8)
├── main.py                            # Monolithic bot entry point, scraper, and database controller
└── README.md                          # Project documentation
```

---

## Requirements

* **Python:** `3.11.8` (or Python 3.10+)
* **Google Chrome:** Headless Chrome binary installed on the host system.
* **External Services:**
  * **Telegram Bot Token:** Obtained from [@BotFather](https://t.me/BotFather).
  * **PostgreSQL Database:** Remote PostgreSQL instance supporting SSL connections.

---

## Installation

### 1. Clone the Repository
```bash
git clone https://github.com/Maurxp/swappa-monitor-bot.git
cd swappa-monitor-bot
```

### 2. Create and Activate a Virtual Environment
```bash
# On Linux / macOS:
python3 -m venv .venv
source .venv/bin/activate

# On Windows (PowerShell):
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Configuration

The application reads configuration parameters strictly from environment variables.

### Environment Variables

| Variable | Required | Description | Example |
| :--- | :--- | :--- | :--- |
| `TELEGRAM_TOKEN` | **Yes** | Telegram Bot API token issued by BotFather | `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ` |
| `DATABASE_URL` | **Yes** | PostgreSQL connection URI (`sslmode=require` enforced) | `postgresql://user:pass@host:5432/dbname` |

### Setting Environment Variables

#### Linux / macOS:
```bash
export TELEGRAM_TOKEN="your_telegram_bot_token"
export DATABASE_URL="postgresql://user:password@host:5432/dbname"
```

#### Windows (PowerShell):
```powershell
$env:TELEGRAM_TOKEN="your_telegram_bot_token"
$env:DATABASE_URL="postgresql://user:password@host:5432/dbname"
```

---

## Database Setup

The database schema is initialized automatically when the application starts via `setup_database()`.

### Table Schemas

#### 1. `reminders` (Active Alert Configurations)
```sql
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
```

#### 2. `notified_listings` (Persistent Listing Deduplication)
```sql
CREATE TABLE IF NOT EXISTS notified_listings (
    id SERIAL PRIMARY KEY,
    reminder_id VARCHAR(255) NOT NULL,
    listing_key VARCHAR(255) NOT NULL,
    notified_at BIGINT NOT NULL,
    price REAL,
    UNIQUE (reminder_id, listing_key)
);
CREATE INDEX IF NOT EXISTS idx_notified_listings_lookup ON notified_listings (reminder_id, listing_key);
```

* SSL connections are required (`sslmode='require'`).
* Auto-migration ensures the `device_name` column exists on startup.

---

## Usage

### 1. Starting the Telegram Bot (Interactive Worker)
Runs the Telegram bot using long-polling:
```bash
python main.py
```
*(Or explicitly: `python main.py run_bot_polling`)*

### 2. Running Scheduled Surveillance Check (Batch CLI)
Executes a single evaluation pass over all active reminders whose frequency interval has expired:
```bash
python main.py run_scheduler_check
```

### 3. Telegram Bot Commands

| Command | Syntax | Example | Description |
| :--- | :--- | :--- | :--- |
| `/start` | `/start` | `/start` | Displays welcome message and command overview. |
| `/help` | `/help` | `/help` | Detailed syntax and parameter documentation. |
| `/remind` | `/remind <url> <price> <cond> <bat> <time>` | `/remind https://swappa.com/listings/apple-iphone-15 700 Good 90 2h` | Creates a monitor and performs an immediate search. |
| `/myreminders` | `/myreminders` | `/myreminders` | Lists all active alerts registered to your chat ID. |
| `/stopreminder` | `/stopreminder <reminder_id>` | `/stopreminder reminder_123456_1710000000` | Deletes the specified reminder by its ID. |

#### Parameter Guide for `/remind`:
* `<url>`: Full Swappa listing URL (e.g., `https://swappa.com/listings/apple-iphone-15`).
* `<price>`: Maximum acceptable price in USD (e.g., `650` or `700.50`).
* `<cond>`: Target cosmetic condition (`Good`, `Mint`, `Fair`, `New`, `Open Box`).
* `<bat>`: Minimum battery health percentage (`0` disables battery filtering; `85` requires `>= 85%`).
* `<time>`: Check frequency: e.g. `30m` (30 minutes) or `2h` (2 hours).

---

## Deployment (Heroku)

The repository includes standard Heroku configuration files:
* `runtime.txt`: Pins `python-3.11.8`.
* `Procfile`: Declares the worker dyno process:
  ```text
  worker: python3 main.py run_bot_polling
  ```

### Heroku Setup Steps

1. **Create Heroku App & Provision Postgres:**
   ```bash
   heroku create swappa-monitor-bot
   heroku addons:create heroku-postgresql:essential-0
   ```

2. **Add Required Buildpacks:**
   The application utilizes Chrome for Testing and Chromedriver via the modern buildpack:
   ```bash
   heroku buildpacks:add heroku/python
   heroku buildpacks:add https://github.com/heroku/heroku-buildpack-chrome-for-testing
   heroku buildpacks:add https://github.com/heroku/heroku-buildpack-chromedriver
   ```

3. **Configure Environment Variables (Optional Chrome Overrides):**
   ```bash
   heroku config:set TELEGRAM_TOKEN="your_bot_token"
   # Optional: Explicit Chrome binary or version override if ever needed:
   # heroku config:set GOOGLE_CHROME_BIN="/app/.chrome-for-testing/chrome-linux64/chrome"
   # heroku config:set CHROME_VERSION_MAIN=157
   ```
   *(Note: `DATABASE_URL` is configured automatically by Heroku Postgres).*

4. **Deploy Application:**
   ```bash
   git push heroku main
   heroku ps:scale worker=1
   ```

5. **Configure Periodic Monitoring (Heroku Scheduler):**
   To execute the periodic reminder evaluation:
   ```bash
   heroku addons:create scheduler:standard
   heroku addons:open scheduler
   ```
   In the Scheduler dashboard, add a recurring task:
   * **Command:** `python3 main.py run_scheduler_check`
   * **Frequency:** Every 10 minutes (or as desired).

---

## Automated Testing

The repository contains an automated test suite verifying version resolution, factory initialization, typed contracts, logging security, and regression parsers:

```bash
# Run all unit and regression tests:
python -m unittest discover tests -v
```

---

## Troubleshooting

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| Bot terminates immediately on startup | Missing `TELEGRAM_TOKEN` or `DATABASE_URL` | Ensure environment variables are populated before running `main.py`. |
| ChromeDetectionError on startup | Chrome executable not found in PATH or standard locations | Install Google Chrome or export `GOOGLE_CHROME_BIN` / `CHROME_PATH` pointing to the binary. |
| `psycopg2.OperationalError: SSL error` | Postgres server does not support or require SSL | Verify connection string; `sslmode='require'` is hardcoded in `db_connect()`. |
| Zero listings returned for valid searches | Swappa updated card CSS selectors | Check if class `xui_card_listing` is still in use on Swappa's desktop site. |
| Scraper errors in logs | Temporary network timeout or Swappa anti-bot challenge | The scraper catches exceptions and sets `ScrapeStatus.ERROR`; alerts are suppressed so no crash logs are sent to users. |

---

## Security & Operational Best Practices

1. **Zero Secrets in Code:** Never commit `.env` files, production logs (`HEROKU_LOGS/`), API keys, or database connection strings.
2. **Exclusion via `.gitignore`:** The repository enforces exclusion of logs, bytecode, and environment secrets via `.gitignore`.
3. **Sensitive Token Redaction:** The logging subsystem applies `SensitiveTokenFilter` to automatically redact Telegram bot tokens from logs before output.
4. **SSRF Protection:** All Swappa URLs submitted by users are strictly validated against `https://swappa.com/` before initiating requests or browser sessions.
5. **SSL Database Enforcement:** All PostgreSQL traffic is forced over encrypted connections via `sslmode='require'`.

---

## Known Limitations

* **Memory Footprint:** Running headless Chrome consumes 150MB–300MB of RAM per scrape session. High concurrency on Heroku Eco/Basic dynos (512MB RAM) may cause memory pressure.
* **No Price History:** The database stores alert criteria only. It does not track historical prices or record which specific listings were previously notified.
* **DOM Fragility:** Scraping depends directly on Swappa's current HTML structure (`div.xui_card_listing`, `span.color_battery`). Layout redesigns require selector updates.
* **Model Regex Edge Case:** Samsung model codes containing hyphens (e.g. `SM-S908U`) may bypass the model filter and be parsed as device color.

---

## Roadmap

- [ ] **Architecture Modularization:** Split `main.py` into decoupled packages (`bot/`, `services/`, `scraper/`, `database/`).
- [ ] **Connection Pooling:** Implement `psycopg2.pool.ThreadedConnectionPool` to reuse database connections.
- [ ] **Concurrency Semaphore:** Implement an `asyncio.Semaphore(1)` to limit concurrent Chrome instances and prevent dyno memory exhaustion.
- [ ] **Historical Price Tracking:** Introduce a `listings_history` table to alert only on newly created listings or price drops.
- [ ] **Unit & Integration Test Suite:** Add `pytest` test suites with mocked HTML snapshots.

---

## Contributing

Contributions, bug reports, and suggestions are welcome!

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature/my-feature`.
3. Verify syntax and logic: `python -m py_compile main.py`.
4. Commit your changes: `git commit -m "feat: add support for custom user-agent"`.
5. Push to the branch: `git push origin feature/my-feature`.
6. Open a Pull Request.

---

## License

This project is licensed under the [MIT License](LICENSE) - see the `LICENSE` file for details.
Copyright (c) 2025 **Mauricio Perdomo**.

---

## Author

* **Mauricio Perdomo** ([@devmauro](https://t.me/devmauro))
* **GitHub:** [@Maurxp](https://github.com/Maurxp)
