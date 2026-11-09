import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Añadir directorio raíz al sys.path para importar main
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import resolve_chrome_environment, ChromeDetectionError

class TestChromeResolver(unittest.TestCase):

    @patch("main.shutil.which")
    @patch("main.subprocess.check_output")
    @patch("main.os.path.exists", return_value=True)
    @patch("main.os.access", return_value=True)
    def test_01_detect_chrome_in_linux_chrome_for_testing(self, mock_access, mock_exists, mock_subp, mock_which):
        """1. Prueba detección del binario 'chrome' expuesto por heroku-buildpack-chrome-for-testing."""
        mock_which.side_effect = lambda name: "/app/.chrome-for-testing/chrome-linux64/chrome" if name == "chrome" else None
        mock_subp.return_value = b"Google Chrome for Testing 157.0.8103.50\n"

        with patch.dict(os.environ, {}, clear=True):
            path, major = resolve_chrome_environment()

        self.assertEqual(path, "/app/.chrome-for-testing/chrome-linux64/chrome")
        self.assertEqual(major, 157)

    @patch("main.shutil.which")
    @patch("main.subprocess.check_output")
    @patch("main.os.path.exists", return_value=True)
    @patch("main.os.access", return_value=True)
    def test_02_detect_google_chrome(self, mock_access, mock_exists, mock_subp, mock_which):
        """2. Prueba detección convencional del comando 'google-chrome'."""
        mock_which.side_effect = lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None
        mock_subp.return_value = b"Google Chrome 120.0.6099.109\n"

        with patch.dict(os.environ, {}, clear=True):
            path, major = resolve_chrome_environment()

        self.assertEqual(path, "/usr/bin/google-chrome")
        self.assertEqual(major, 120)

    @patch("main.subprocess.check_output")
    @patch("main.os.path.exists", return_value=True)
    @patch("main.os.access", return_value=True)
    def test_03_detect_via_environment_variable(self, mock_access, mock_exists, mock_subp):
        """3. Prueba detección prioritaria por variable de entorno GOOGLE_CHROME_BIN."""
        mock_subp.return_value = b"Google Chrome for Testing 157.0.8103.50\n"

        env = {"GOOGLE_CHROME_BIN": "/custom/bin/chrome"}
        with patch.dict(os.environ, env, clear=True):
            path, major = resolve_chrome_environment()

        self.assertEqual(path, "/custom/bin/chrome")
        self.assertEqual(major, 157)

    @patch("main.shutil.which")
    @patch("main.subprocess.check_output")
    @patch("main.os.path.exists")
    @patch("main.os.access", return_value=True)
    def test_04_non_existent_env_path_falls_back(self, mock_access, mock_exists, mock_subp, mock_which):
        """4. Prueba que una ruta inexistente en variable de entorno se omite y busca en PATH."""
        # La ruta en GOOGLE_CHROME_BIN no existe; la ruta en PATH sí existe
        mock_exists.side_effect = lambda p: p == "/usr/bin/chrome"
        mock_which.side_effect = lambda name: "/usr/bin/chrome" if name == "chrome" else None
        mock_subp.return_value = b"Google Chrome 157.0.8103.50\n"

        env = {"GOOGLE_CHROME_BIN": "/non/existent/path/chrome"}
        with patch.dict(os.environ, env, clear=True):
            path, major = resolve_chrome_environment()

        self.assertEqual(path, "/usr/bin/chrome")
        self.assertEqual(major, 157)

    @patch("main.shutil.which")
    @patch("main.subprocess.check_output")
    @patch("main.os.path.exists", return_value=True)
    @patch("main.os.access", return_value=True)
    def test_05_invalid_version_output_raises_error(self, mock_access, mock_exists, mock_subp, mock_which):
        """5. Prueba que una salida malformada o no numérica no genera un major version corrupto."""
        mock_which.return_value = "/usr/bin/chrome"
        mock_subp.return_value = b"Corrupted browser output without numbers\n"

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ChromeDetectionError):
                resolve_chrome_environment()

    @patch("main.shutil.which", return_value=None)
    @patch("main.os.path.exists", return_value=False)
    def test_06_total_absence_of_chrome_raises_descriptive_error(self, mock_exists, mock_which):
        """6. Prueba que la ausencia total de Chrome produce ChromeDetectionError (sin fallbacks silenciosos)."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ChromeDetectionError) as ctx:
                resolve_chrome_environment()
            self.assertIn("No se pudo detectar un ejecutable de Chrome", str(ctx.exception))

    @patch("main.subprocess.check_output")
    @patch("main.os.path.exists", return_value=True)
    @patch("main.os.access", return_value=True)
    def test_07_correct_major_version_extraction(self, mock_access, mock_exists, mock_subp):
        """7. Prueba extracción precisa de versiones de dos y tres dígitos."""
        test_cases = [
            (b"Google Chrome 157.0.8103.50\n", 157),
            (b"Chromium 99.0.4844.84\n", 99),
            (b"Google Chrome for Testing 160.0.1234.5\n", 160),
        ]
        for output_bytes, expected_major in test_cases:
            mock_subp.return_value = output_bytes
            with patch.dict(os.environ, {"GOOGLE_CHROME_BIN": "/bin/chrome"}, clear=True):
                _, major = resolve_chrome_environment()
                self.assertEqual(major, expected_major)

if __name__ == "__main__":
    unittest.main()
