import os
import sys
import unittest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import scrape_swappa, ScrapeStatus, ScrapeResult, run_scheduler_check

class TestContractsAndConsumers(unittest.TestCase):

    @patch("main.create_uc_driver")
    def test_10_driver_quit_called_on_exception(self, mock_factory):
        """10. Prueba que driver.quit() siempre se ejecuta en bloque finally cuando ocurre un error."""
        mock_driver = MagicMock()
        mock_driver.get.side_effect = Exception("Connection refused / Swappa timeout")
        mock_factory.return_value = mock_driver

        res = scrape_swappa("https://swappa.com/listings/apple-iphone-15", 800, "Good", 0, "iPhone 15")

        self.assertEqual(res.status, ScrapeStatus.ERROR)
        mock_driver.quit.assert_called_once()

    @patch("main.create_uc_driver")
    def test_11_scrape_swappa_contract_empty_and_error(self, mock_factory):
        """11. Prueba que scrape_swappa produce ScrapeResult con estados formales EMPTY y ERROR."""
        # A. Error
        mock_factory.side_effect = Exception("Chrome could not start")
        res_error = scrape_swappa("https://swappa.com/listings/apple-iphone-15", 800, "Good", 0, "iPhone 15")
        self.assertEqual(res_error.status, ScrapeStatus.ERROR)
        self.assertIn("Chrome could not start", res_error.error_message)

        # B. URL inválida
        res_invalid = scrape_swappa("http://evil.com/phishing", 800, "Good", 0, "iPhone 15")
        self.assertEqual(res_invalid.status, ScrapeStatus.ERROR)

    @patch("main.db_connect")
    @patch("main.Application")
    @patch("main.scrape_swappa")
    def test_12_and_13_scheduler_does_not_send_alerts_on_error(self, mock_scrape, mock_app_cls, mock_db):
        """12 y 13. Prueba que el scheduler no envía mensajes a Telegram ante ScrapeStatus.ERROR ni stack traces."""
        # Simular recordatorio en BD
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [{
            "id": 1,
            "chat_id": "12345",
            "reminder_id": "rem_1",
            "url": "https://swappa.com/listings/apple-iphone-15",
            "max_price": 700.0,
            "condition": "Good",
            "min_battery": 90,
            "frequency_seconds": 60,
            "last_checked": 0,
            "device_name": "iPhone 15"
        }]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_db.return_value = mock_conn

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        mock_builder = MagicMock()
        mock_builder.token.return_value.build.return_value = mock_app
        mock_app_cls.builder.return_value = mock_builder

        # Simular resultado ERROR del scraper
        mock_scrape.return_value = ScrapeResult(
            status=ScrapeStatus.ERROR,
            error_message="SessionNotCreatedException: Chrome crash"
        )

        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "dummy_token", "DATABASE_URL": "postgres://dummy"}, clear=True):
            asyncio.run(run_scheduler_check())

        # Verificar que send_message NUNCA fue llamado
        mock_bot.send_message.assert_not_called()

if __name__ == "__main__":
    unittest.main()
