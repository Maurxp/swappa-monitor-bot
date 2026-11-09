import os
import sys
import unittest
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import SensitiveTokenFilter, is_valid_swappa_url

class TestSecurityAndLogging(unittest.TestCase):

    def test_14_token_redaction_in_logging(self):
        """14. Prueba que el SensitiveTokenFilter redacta tokens ficticios de Telegram de mensajes y argumentos."""
        filter_instance = SensitiveTokenFilter()

        # Caso 1: Token ficticio embebido en mensaje
        record1 = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=10,
            msg="Request to https://api.telegram.org/bot123456789:ABC_FakeToken-xyz/sendMessage failed",
            args=(), exc_info=None
        )
        filter_instance.filter(record1)
        self.assertNotIn("bot123456789:ABC_FakeToken-xyz", record1.msg)
        self.assertIn("bot[REDACTED_TELEGRAM_TOKEN]", record1.msg)

        # Caso 2: Token ficticio en argumentos
        record2 = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=15,
            msg="Sending request to: %s",
            args=("https://api.telegram.org/bot987654321:XYZ-AnotherFakeToken_123/getUpdates",),
            exc_info=None
        )
        filter_instance.filter(record2)
        self.assertNotIn("bot987654321:XYZ-AnotherFakeToken_123", str(record2.args))
        self.assertIn("bot[REDACTED_TELEGRAM_TOKEN]", str(record2.args))

    def test_15_ssrf_url_validation(self):
        """15. Prueba validación estricta de dominios permitidos para Swappa y rechazo de IPs privadas."""
        valid_urls = [
            "https://swappa.com/listings/apple-iphone-15",
            "https://www.swappa.com/listings/google-pixel-8?carrier=unlocked",
            "https://swappa.com/buy/phones",
        ]
        invalid_urls = [
            "http://swappa.com/listings/apple-iphone-15",           # Rechazar HTTP inseguro
            "https://evil-swappa.com/listings/fake",                # Dominio malicioso
            "https://swappa.com.attacker.com/bypass",               # Subdominio malicioso
            "https://127.0.0.1/admin",                              # Loopback
            "https://169.254.169.254/latest/meta-data/",            # AWS/Cloud Metadata
            "https://localhost:8080/metrics",                       # Host local
            "ftp://swappa.com/file",                                # Esquema no HTTP
            "not-a-valid-url",
            "",
        ]

        for url in valid_urls:
            self.assertTrue(is_valid_swappa_url(url), f"Debería ser válida: {url}")

        for url in invalid_urls:
            self.assertFalse(is_valid_swappa_url(url), f"Debería ser rechazada: {url}")

if __name__ == "__main__":
    unittest.main()
