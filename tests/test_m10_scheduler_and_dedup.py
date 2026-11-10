import os
import sys
import unittest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import (
    claim_due_reminders,
    filter_unnotified_listings,
    record_notified_listings,
    run_scheduler_check,
    ScrapeResult,
    ScrapeStatus
)

class TestM10SchedulerAndDedup(unittest.TestCase):
    """
    Suite de pruebas para M10.3, M10.4 y M10.5: Scheduler, Concurrencia, Memoria y Deduplicación.
    """

    @patch("main.DATABASE_URL", "postgres://mock_db")
    @patch("main.psycopg2", MagicMock())
    @patch("main.db_connect")
    def test_claim_due_reminders_tolerance_window(self, mock_db):
        """
        Prueba M10-003: Verifica que la consulta de reclamo atómico aplique la ventana de tolerancia
        para que recordatorios de 15 minutos (900s) se reclamen puntualmente.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_db.return_value = mock_conn

        # Simular una fila que cumple la condición de vencimiento
        mock_cur.fetchall.return_value = [{
            "id": 10,
            "chat_id": "12345",
            "reminder_id": "rem_10",
            "url": "https://swappa.com/listings/apple-iphone-13",
            "max_price": 340.0,
            "condition": "Good",
            "min_battery": 90,
            "frequency_seconds": 900,
            "last_checked": 1000,
            "device_name": "Apple iPhone 13"
        }]

        claimed = claim_due_reminders(batch_limit=5, tolerance_seconds=60)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["reminder_id"], "rem_10")
        # Verificar que se ejecutó el UPDATE con last_checked actualizado de inmediato
        mock_cur.execute.assert_any_call(
            "UPDATE reminders SET last_checked = %s WHERE id = ANY(%s);",
            (unittest.mock.ANY, [10])
        )
        mock_conn.commit.assert_called_once()

    @patch("main.DATABASE_URL", "postgres://mock_db")
    @patch("main.psycopg2", MagicMock())
    @patch("main.db_connect")
    def test_deduplication_filter_unnotified_listings(self, mock_db):
        """
        Prueba M10-006: Verifica que filter_unnotified_listings excluya ofertas ya despachadas
        y solo conserve ofertas nuevas.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_db.return_value = mock_conn

        # Simular que el anuncio 'ABC101' ya fue notificado en la BD
        mock_cur.fetchall.return_value = [("ABC101",)]

        items = [
            {"listing_id": "ABC101", "precio": 320.0, "link": "https://swappa.com/listing/view/ABC101"},
            {"listing_id": "ABC102", "precio": 330.0, "link": "https://swappa.com/listing/view/ABC102"}
        ]

        unnotified = filter_unnotified_listings("rem_test", items)

        # Solo ABC102 debe pasar el filtro
        self.assertEqual(len(unnotified), 1)
        self.assertEqual(unnotified[0]["listing_id"], "ABC102")

    @patch("main.DATABASE_URL", "postgres://mock_db")
    @patch("main.psycopg2", MagicMock())
    @patch("main.db_connect")
    def test_record_notified_listings_commits_idempotently(self, mock_db):
        """
        Prueba M10.5: Verifica que record_notified_listings inserte registros con ON CONFLICT DO NOTHING.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_db.return_value = mock_conn

        items = [{"listing_id": "ABC102", "precio": 330.0}]

        record_notified_listings("rem_test", items)

        mock_cur.execute.assert_called_once()
        args = mock_cur.execute.call_args[0]
        self.assertIn("ON CONFLICT (reminder_id, listing_key) DO NOTHING", args[0])
        mock_conn.commit.assert_called_once()

    @patch("main.claim_due_reminders")
    @patch("main.create_uc_driver")
    @patch("main.scrape_swappa")
    @patch("main.filter_unnotified_listings")
    @patch("main.record_notified_listings")
    def test_scheduler_batch_browser_reuse_and_telegram_failure_handling(
        self, mock_record, mock_filter, mock_scrape, mock_create_uc, mock_claim
    ):
        """
        Prueba combinada M10.4 y M10.5:
        1. Verifica que para un lote de 2 recordatorios, create_uc_driver se llame EXACTAMENTE UNA VEZ (evitando R14).
        2. Verifica que el driver se cierre exactamente una vez en finally.
        3. Verifica que ante falla en bot.send_message, NO se marque la oferta como notificada.
        """
        mock_claim.return_value = [
            {
                "id": 1,
                "chat_id": "chat_ok",
                "reminder_id": "rem_ok",
                "url": "https://swappa.com/listings/apple-iphone-13",
                "max_price": 340.0,
                "condition": "Good",
                "min_battery": 90,
                "device_name": "Apple iPhone 13"
            },
            {
                "id": 2,
                "chat_id": "chat_fail",
                "reminder_id": "rem_fail",
                "url": "https://swappa.com/listings/google-pixel-8",
                "max_price": 400.0,
                "condition": "Good",
                "min_battery": 0,
                "device_name": "Google Pixel 8"
            }
        ]

        mock_driver = MagicMock()
        mock_create_uc.return_value = mock_driver

        mock_scrape.side_effect = [
            ScrapeResult(
                status=ScrapeStatus.SUCCESS,
                items=[{"listing_id": "A1", "precio": 320}],
                formatted_message="Alerta iPhone"
            ),
            ScrapeResult(
                status=ScrapeStatus.SUCCESS,
                items=[{"listing_id": "B1", "precio": 380}],
                formatted_message="Alerta Pixel"
            )
        ]

        mock_filter.side_effect = lambda rem_id, items: items

        # Simular bot de Telegram donde el segundo envío falla por red
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=[True, Exception("Telegram Network Timeout")])

        asyncio.run(run_scheduler_check(bot=mock_bot))

        # 1. Driver creado UNA sola vez para todo el lote
        mock_create_uc.assert_called_once()
        # 2. Driver cerrado UNA sola vez en finally
        mock_driver.quit.assert_called_once()
        # 3. Solo el primer recordatorio exitoso registró ofertas notificadas
        mock_record.assert_called_once_with("rem_ok", [{"listing_id": "A1", "precio": 320}])

if __name__ == "__main__":
    unittest.main()
