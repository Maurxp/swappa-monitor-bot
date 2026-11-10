import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import scrape_swappa, ScrapeStatus, ScrapeResult

# HTML de prueba simulando respuesta real de Swappa con anuncios
SAMPLE_SWAPPA_HTML = """
<!DOCTYPE html>
<html>
<head><title>Apple iPhone 13 - Buy & Sell on Swappa</title></head>
<body>
    <div class="xui_card_listing">
        <div class="price"><a href="/listing/view/ABC101"><span itemprop="price">$320.00</span></a></div>
        <div class="seller_name">TechStoreNY</div>
        <div class="attrs">
            <span class="attr"><meta itemprop="itemCondition" content="https://schema.org/UsedCondition">Good</span>
            <span class="attr color_battery">Battery: 92%</span>
            <span class="attr">128 GB</span>
            <span class="attr">Midnight</span>
        </div>
    </div>
    <div class="xui_card_listing">
        <div class="price"><a href="/listing/view/ABC102"><span itemprop="price">$450.00</span></a></div>
        <div class="seller_name">GadgetPro</div>
        <div class="attrs">
            <span class="attr"><meta itemprop="itemCondition" content="https://schema.org/UsedCondition">Mint</span>
            <span class="attr color_battery">Battery: 98%</span>
            <span class="attr">256 GB</span>
            <span class="attr">Blue</span>
        </div>
    </div>
</body>
</html>
"""

# HTML de prueba simulando estado vacío confirmado
SAMPLE_EMPTY_HTML = """
<!DOCTYPE html>
<html>
<head><title>Apple iPhone 13 - Buy & Sell on Swappa</title></head>
<body>
    <div class="no-listings">
        <h3>No active listings found for this product.</h3>
    </div>
</body>
</html>
"""

class TestM10ScraperResilience(unittest.TestCase):
    """
    Suite de pruebas para M10.2: Clasificación estricta de Scraping y resiliencia de Chrome.
    """

    def test_scraper_success_with_matching_deals(self):
        """Verifica que ofertas que cumplen precio y filtros retornen ScrapeStatus.SUCCESS."""
        mock_driver = MagicMock()
        mock_driver.title = "Apple iPhone 13 - Buy & Sell on Swappa"
        mock_driver.page_source = SAMPLE_SWAPPA_HTML

        with patch("main.create_uc_driver", return_value=mock_driver), \
             patch("main.WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = True

            result = scrape_swappa(
                url="https://swappa.com/listings/apple-iphone-13",
                max_price=340.0,
                desired_condition="Good",
                min_battery=90,
                device_name="Apple iPhone 13"
            )

            self.assertEqual(result.status, ScrapeStatus.SUCCESS)
            self.assertEqual(len(result.items), 1)
            item = result.items[0]
            self.assertEqual(item["listing_id"], "ABC101")
            self.assertEqual(item["precio"], 320.0)
            self.assertEqual(item["estado"], "Good")
            self.assertEqual(item["bateria"], 92)
            self.assertIn("TechStoreNY", item["vendedor"])

    def test_scraper_empty_when_no_deals_match_filters(self):
        """Verifica que si hay publicaciones pero ninguna cumple el precio, se clasifique como ScrapeStatus.EMPTY."""
        mock_driver = MagicMock()
        mock_driver.title = "Apple iPhone 13 - Buy & Sell on Swappa"
        mock_driver.page_source = SAMPLE_SWAPPA_HTML

        with patch("main.create_uc_driver", return_value=mock_driver), \
             patch("main.WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = True

            # Filtro con max_price=300 (ambos anuncios son $320 y $450)
            result = scrape_swappa(
                url="https://swappa.com/listings/apple-iphone-13",
                max_price=300.0,
                desired_condition="Good",
                min_battery=90,
                device_name="Apple iPhone 13"
            )

            self.assertEqual(result.status, ScrapeStatus.EMPTY)
            self.assertEqual(result.items, [])

    def test_scraper_empty_when_page_has_explicit_no_listings(self):
        """Verifica que una página cargada con estado explícito de 'No listings' retorne ScrapeStatus.EMPTY."""
        mock_driver = MagicMock()
        mock_driver.title = "Apple iPhone 13 - Buy & Sell on Swappa"
        mock_driver.page_source = SAMPLE_EMPTY_HTML

        with patch("main.create_uc_driver", return_value=mock_driver), \
             patch("main.WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = True

            result = scrape_swappa(
                url="https://swappa.com/listings/apple-iphone-13",
                max_price=500.0,
                desired_condition="Good",
                min_battery=0,
                device_name="Apple iPhone 13"
            )

            self.assertEqual(result.status, ScrapeStatus.EMPTY)
            self.assertEqual(result.items, [])

    def test_scraper_page_1_timeout_returns_error_never_empty(self):
        """
        Prueba crítica M10-002: Verifica que un TimeoutException en la página 1
        se clasifique rigurosamente como ScrapeStatus.ERROR y NUNCA como ScrapeStatus.EMPTY.
        """
        mock_driver = MagicMock()
        mock_driver.title = "Swappa"
        mock_driver.page_source = "<html><body></body></html>"

        with patch("main.create_uc_driver", return_value=mock_driver), \
             patch("main.WebDriverWait") as mock_wait:
            # Simular timeout de 15s en WebDriverWait
            mock_wait.return_value.until.side_effect = Exception("TimeoutException: Timed out after 15 seconds")

            result = scrape_swappa(
                url="https://swappa.com/listings/apple-iphone-13",
                max_price=340.0,
                desired_condition="Good",
                min_battery=90,
                device_name="Apple iPhone 13"
            )

            self.assertEqual(result.status, ScrapeStatus.ERROR)
            self.assertIn("timeout", result.error_message.lower())

    def test_scraper_cloudflare_challenge_detected_returns_error(self):
        """Verifica que una pantalla de desafío anti-bot de Cloudflare retorne ScrapeStatus.ERROR descriptivo."""
        mock_driver = MagicMock()
        mock_driver.title = "Just a moment..."
        mock_driver.page_source = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"

        with patch("main.create_uc_driver", return_value=mock_driver):
            result = scrape_swappa(
                url="https://swappa.com/listings/apple-iphone-13",
                max_price=340.0,
                desired_condition="Good",
                min_battery=90,
                device_name="Apple iPhone 13"
            )

            self.assertEqual(result.status, ScrapeStatus.ERROR)
            self.assertIn("cloudflare", result.error_message.lower())

    def test_scraper_external_driver_not_closed_in_finally(self):
        """
        Prueba M10.4: Verifica que al pasar un driver externo compartido,
        scrape_swappa NO invoque driver.quit() en su bloque finally, permitiendo reutilizar la sesión.
        """
        mock_driver = MagicMock()
        mock_driver.title = "Apple iPhone 13"
        mock_driver.page_source = SAMPLE_SWAPPA_HTML

        with patch("main.WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = True

            result = scrape_swappa(
                url="https://swappa.com/listings/apple-iphone-13",
                max_price=340.0,
                desired_condition="Good",
                min_battery=90,
                device_name="Apple iPhone 13",
                driver=mock_driver  # Driver externo
            )

            self.assertEqual(result.status, ScrapeStatus.SUCCESS)
            # El driver externo NO debe ser cerrado
            mock_driver.quit.assert_not_called()

if __name__ == "__main__":
    unittest.main()

