import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import extract_device_name_from_url, get_device_name, is_valid_swappa_url

class TestM10ProductAndParser(unittest.TestCase):
    """
    Suite de pruebas para M10.1: Inferencia de nombre slug-first y validaciones.
    """

    def test_extract_device_name_from_standard_slugs(self):
        """Verifica que diversos slugs canónicos de Swappa se formateen con capitalización semántica adecuada."""
        cases = [
            ("https://swappa.com/listings/apple-iphone-13", "Apple iPhone 13"),
            ("https://swappa.com/listings/apple-iphone-15-pro-max", "Apple iPhone 15 Pro Max"),
            ("https://swappa.com/listings/apple-iphone-se-2022", "Apple iPhone SE 2022"),
            ("https://swappa.com/buy/google-pixel-8-pro", "Google Pixel 8 Pro"),
            ("https://swappa.com/buy/samsung-galaxy-s23-ultra", "Samsung Galaxy S23 Ultra"),
            ("https://swappa.com/listings/nintendo-switch-oled", "Nintendo Switch OLED"),
            ("https://swappa.com/listings/sony-playstation-5", "Sony PlayStation 5"),
            ("https://swappa.com/listings/oneplus-11-5g", "OnePlus 11 5G"),
            ("https://swappa.com/listings/apple-ipad-air-5th-gen", "Apple iPad Air 5th Gen"),
            ("https://swappa.com/listings/microsoft-surface-pro-9", "Microsoft Surface Pro 9"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                result = extract_device_name_from_url(url)
                self.assertEqual(result, expected)

    def test_extract_device_name_with_complex_query_params(self):
        """Verifica que los query strings y el orden de parámetros no alteren la extracción del slug."""
        url = "https://swappa.com/listings/apple-iphone-13?carrier=unlocked&condition=good&sort=price_low"
        self.assertEqual(extract_device_name_from_url(url), "Apple iPhone 13")

        url_reversed = "https://swappa.com/listings/apple-iphone-13?sort=price_low&condition=good&carrier=unlocked"
        self.assertEqual(extract_device_name_from_url(url_reversed), "Apple iPhone 13")

    def test_get_device_name_falls_back_to_slug_on_http_403(self):
        """
        Prueba crítica M10-001: Verifica que ante una respuesta HTTP 403 Forbidden de Cloudflare,
        get_device_name NO retorne 'Producto Desconocido', sino el nombre inferido localmente.
        """
        url = "https://swappa.com/listings/apple-iphone-13?carrier=unlocked&condition=good&sort=price_low"
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("403 Client Error: Forbidden for url")

        with patch("requests.get", return_value=mock_response):
            name = get_device_name(url)
            self.assertEqual(name, "Apple iPhone 13")

    def test_get_device_name_timeout_falls_back_to_slug(self):
        """Verifica que un timeout de red en requests.get tampoco impida la resolución vía slug."""
        url = "https://swappa.com/buy/google-pixel-8-pro"
        with patch("requests.get", side_effect=Exception("ReadTimeout / Connection timed out")):
            name = get_device_name(url)
            self.assertEqual(name, "Google Pixel 8 Pro")

    def test_get_device_name_ssrf_rejection(self):
        """Verifica que URLs ajenas a Swappa o maliciosas sean rechazadas por SSRF y retornen 'Producto Desconocido'."""
        invalid_urls = [
            "http://swappa.com/listings/apple-iphone-13",  # No HTTPS
            "https://evil.com/listings/apple-iphone-13",
            "https://swappa.com.attacker.com/listings/apple-iphone-13",
            "javascript:alert(1)",
        ]
        for bad_url in invalid_urls:
            with self.subTest(url=bad_url):
                self.assertEqual(get_device_name(bad_url), "Producto Desconocido")

    def test_get_device_name_non_slug_url_fallback(self):
        """Verifica que una URL de Swappa sin slug reconocible retorne 'Producto Swappa' en vez de error fatal."""
        url = "https://swappa.com/account/login"
        with patch("requests.get", side_effect=Exception("403 Forbidden")):
            name = get_device_name(url)
            self.assertEqual(name, "Producto Swappa")

if __name__ == "__main__":
    unittest.main()

