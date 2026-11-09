import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import create_uc_driver

class TestUCFactory(unittest.TestCase):

    @patch("main.resolve_chrome_environment")
    @patch("main.uc")
    def test_08_uc_initialization_receives_correct_path_and_version(self, mock_uc, mock_resolver):
        """8. Prueba que create_uc_driver transfiere version_main y browser_executable_path a uc.Chrome."""
        mock_resolver.return_value = ("/app/.chrome-for-testing/chrome-linux64/chrome", 157)
        mock_driver_instance = MagicMock()
        mock_uc.Chrome.return_value = mock_driver_instance

        driver = create_uc_driver()

        self.assertEqual(driver, mock_driver_instance)
        mock_uc.Chrome.assert_called_once()
        _, kwargs = mock_uc.Chrome.call_args
        self.assertEqual(kwargs.get("version_main"), 157)
        self.assertEqual(kwargs.get("browser_executable_path"), "/app/.chrome-for-testing/chrome-linux64/chrome")

    @patch("main.resolve_chrome_environment")
    @patch("main.uc")
    def test_09_uc_initialization_error_propagated(self, mock_uc, mock_resolver):
        """9. Prueba que una falla en uc.Chrome eleva una excepción capturable."""
        mock_resolver.return_value = ("/usr/bin/chrome", 157)
        mock_uc.Chrome.side_effect = Exception("SessionNotCreatedException: driver mismatch")

        with self.assertRaises(Exception) as ctx:
            create_uc_driver()
        self.assertIn("SessionNotCreatedException", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
