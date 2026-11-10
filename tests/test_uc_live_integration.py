import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import resolve_chrome_environment, ChromeDetectionError, create_uc_driver

class TestUCLiveIntegration(unittest.TestCase):

    def test_18_real_uc_session_if_chrome_present(self):
        """
        Prueba de integración real con undetected_chromedriver.
        Verifica el ciclo de vida completo:
        1. Inicialización real de Chrome.
        2. Creación de sesión UC.
        3. Navegación a página de prueba controlada.
        4. Comprobación del título.
        5. Cierre limpio del driver.

        Si el entorno local carece de Chrome compatible, se documenta la limitación
        de manera transparente mediante unittest.SkipTest.
        """
        try:
            bin_path, major_v = resolve_chrome_environment()
        except ChromeDetectionError as e:
            self.skipTest(f"LIMITACIÓN DE ENTORNO LOCAL: Chrome no disponible localmente en Windows ({e}).")

        if not bin_path:
            self.skipTest("LIMITACIÓN DE ENTORNO LOCAL: Binario de Chrome no resuelto en host local.")

        try:
            import undetected_chromedriver as uc
        except ImportError:
            self.skipTest("LIMITACIÓN DE ENTORNO LOCAL: undetected_chromedriver no está instalado en este entorno.")

        driver = None
        try:
            driver = create_uc_driver()
            self.assertIsNotNone(driver)
            driver.get("https://example.com")
            title = driver.title
            self.assertIn("Example", title)
        except Exception as e:
            self.fail(f"Fallo durante prueba de integración real de UC: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

if __name__ == "__main__":
    unittest.main()
