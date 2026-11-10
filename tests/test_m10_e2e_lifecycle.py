import os
import sys
import unittest
import asyncio
import time
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import (
    remind,
    claim_due_reminders,
    run_scheduler_check,
    filter_unnotified_listings,
    record_notified_listings,
    ScrapeResult,
    ScrapeStatus
)

class TestM10E2ELifecycle(unittest.TestCase):
    """
    Subfase M10.7: Prueba de Validación Funcional de Extremo a Extremo en Entorno Controlado.
    Modela el ciclo de vida completo de un recordatorio:
    Creación -> Parser -> Slug Inferred -> DB Store -> Telegram Confirmation ->
    Immediate Search -> Scheduler Claim -> Browser Batch -> Listing Deduplication -> Telegram Alert.
    """

    @patch("main.DATABASE_URL", "postgres://mock_production_db")
    @patch("main.psycopg2", MagicMock())
    @patch("main.db_connect")
    @patch("main.scrape_swappa")
    def test_end_to_end_lifecycle_pipeline(self, mock_scrape, mock_db):
        # 1. Base de datos simulada en memoria
        db_reminders = {}
        db_notified = set()
        next_id = 1

        mock_conn = MagicMock()
        mock_cur = MagicMock()

        def mock_execute(sql, params=None):
            nonlocal next_id
            sql_clean = " ".join(sql.split())
            if "INSERT INTO reminders" in sql_clean:
                # chat_id, reminder_id, url, max_price, condition, min_battery, frequency_seconds, last_checked, device_name
                r_id = next_id
                next_id += 1
                db_reminders[params[1]] = {
                    "id": r_id,
                    "chat_id": params[0],
                    "reminder_id": params[1],
                    "url": params[2],
                    "max_price": params[3],
                    "condition": params[4],
                    "min_battery": params[5],
                    "frequency_seconds": params[6],
                    "last_checked": params[7],
                    "device_name": params[8]
                }
            elif "SELECT id, chat_id, reminder_id" in sql_clean:
                # Simular SELECT FOR UPDATE SKIP LOCKED
                now_val = params[0]
                tol_val = params[1]
                due = []
                for rem in db_reminders.values():
                    if (now_val - rem["last_checked"]) >= (rem["frequency_seconds"] - tol_val):
                        due.append(rem)
                mock_cur.fetchall.return_value = due
            elif "UPDATE reminders SET last_checked" in sql_clean:
                now_val = params[0]
                row_ids = params[1]
                for rem in db_reminders.values():
                    if rem["id"] in row_ids:
                        rem["last_checked"] = now_val
            elif "SELECT listing_key FROM notified_listings" in sql_clean:
                rem_id = params[0]
                keys = params[1]
                found = [(k,) for k in keys if (rem_id, k) in db_notified]
                mock_cur.fetchall.return_value = found
            elif "INSERT INTO notified_listings" in sql_clean:
                rem_id = params[0]
                l_key = params[1]
                db_notified.add((rem_id, l_key))

        mock_cur.execute.side_effect = mock_execute
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_db.return_value = mock_conn

        # ---------------------------------------------------------
        # ETAPA 1: RECEPCIÓN DEL COMANDO /remind EN TELEGRAM
        # ---------------------------------------------------------
        mock_update = MagicMock()
        mock_update.message.chat_id = 987654321
        mock_update.message.reply_html = AsyncMock()
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.args = [
            "https://swappa.com/listings/apple-iphone-13?carrier=unlocked&condition=good&sort=price_low",
            "340",
            "Good",
            "91",
            "15m"
        ]

        # Simular que en la primera búsqueda inmediata Swappa no tiene ofertas que cumplan criterios
        mock_scrape.return_value = ScrapeResult(status=ScrapeStatus.EMPTY, items=[])

        # Ejecutar /remind
        asyncio.run(remind(mock_update, mock_context))

        # Verificación Etapa 1: Inferencia slug-first correcta sin depender de red
        self.assertEqual(len(db_reminders), 1)
        stored_reminder = list(db_reminders.values())[0]
        self.assertEqual(stored_reminder["device_name"], "Apple iPhone 13")
        self.assertEqual(stored_reminder["max_price"], 340.0)
        self.assertEqual(stored_reminder["condition"], "Good")
        self.assertEqual(stored_reminder["min_battery"], 91)
        self.assertEqual(stored_reminder["frequency_seconds"], 900)  # 15 minutos exactos

        # Verificación Confirmación Telegram
        mock_update.message.reply_html.assert_any_call(
            "✅ <b>Recordatorio configurado para Apple iPhone 13.</b>\nFrecuencia: 15 minutos.\n\n<i>Buscando ahora...</i> 🔍"
        )
        mock_update.message.reply_text.assert_any_call(
            "😥 No se encontraron ofertas que cumplan tus criterios en este momento."
        )

        # ---------------------------------------------------------
        # ETAPA 2: EVALUACIÓN TEMPORAL PREMATURA (4 minutos transcurridos)
        # ---------------------------------------------------------
        initial_checked = stored_reminder["last_checked"]
        current_clock = initial_checked + 240  # 4 minutos después

        with patch("time.time", return_value=current_clock):
            claimed_early = claim_due_reminders(batch_limit=10, tolerance_seconds=60)
            # No debe reclamarse porque 240 < (900 - 60)
            self.assertEqual(len(claimed_early), 0)

        # ---------------------------------------------------------
        # ETAPA 3: EVALUACIÓN EN TIEMPO VENCIDO (15 minutos transcurridos)
        # ---------------------------------------------------------
        current_clock = initial_checked + 900  # Exactamente 15 minutos después

        # Simular que ahora Swappa tiene una nueva oferta
        mock_scrape.return_value = ScrapeResult(
            status=ScrapeStatus.SUCCESS,
            items=[{"listing_id": "DEAL999", "precio": 329.0, "link": "https://swappa.com/listing/view/DEAL999"}],
            formatted_message="🔔 ¡Alerta de Swappa! Oferta iPhone 13 a $329"
        )

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=True)

        with patch("time.time", return_value=current_clock), \
             patch("main.create_uc_driver") as mock_uc:
            mock_driver = MagicMock()
            mock_uc.return_value = mock_driver

            # Ejecución del scheduler
            asyncio.run(run_scheduler_check(bot=mock_bot))

            # Verificar que el driver fue utilizado y cerrado
            mock_uc.assert_called_once()
            mock_driver.quit.assert_called_once()

            # Verificar envío de Telegram
            mock_bot.send_message.assert_called_once_with(
                chat_id="987654321",
                text="🔔 ¡Alerta de Swappa! Oferta iPhone 13 a $329",
                parse_mode="HTML",
                disable_web_page_preview=True
            )

            # Verificar que DEAL999 quedó registrado en deduplicación
            rem_id = stored_reminder["reminder_id"]
            self.assertIn((rem_id, "DEAL999"), db_notified)

            # Verificar que last_checked se actualizó a la hora actual
            self.assertEqual(stored_reminder["last_checked"], current_clock)

        # ---------------------------------------------------------
        # ETAPA 4: SIGUIENTE CICLO (30 minutos transcurridos) CON LA MISMA OFERTA
        # ---------------------------------------------------------
        current_clock += 900  # 15 minutos más tarde
        mock_bot.reset_mock()

        with patch("time.time", return_value=current_clock), \
             patch("main.create_uc_driver") as mock_uc:
            mock_driver = MagicMock()
            mock_uc.return_value = mock_driver

            # Ejecución del scheduler
            asyncio.run(run_scheduler_check(bot=mock_bot))

            # La oferta DEAL999 ya fue notificada previamente:
            # NO debe reenviar el mensaje a Telegram
            mock_bot.send_message.assert_not_called()

if __name__ == "__main__":
    unittest.main()
