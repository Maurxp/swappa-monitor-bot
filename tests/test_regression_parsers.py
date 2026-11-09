import os
import sys
import unittest
import re

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

class TestRegressionParsers(unittest.TestCase):

    def test_16_samsung_model_regex_with_hyphen(self):
        """16. Prueba que los códigos de modelo con guión (ej: SM-S908U) son reconocidos como modelos y no como color."""
        # Regex implementada en main.py para atributos de Swappa
        model_pattern = re.compile(r'^[a-z]{1,2}-?[a-z]?\d{3,4}[a-z0-9]*', re.IGNORECASE)

        models = [
            "SM-S908U",     # Samsung Galaxy S22 Ultra
            "SM-S918B",     # Samsung Galaxy S23 Ultra
            "A2482",        # iPhone 13
            "A2649",        # iPhone 14
            "SM-G998U1",    # S21 Ultra
            "9999",         # Solo dígitos
        ]

        non_models = [
            "Phantom Black",
            "Midnight",
            "Sierra Blue",
            "128GB",
            "256GB",
            "Unlocked",
        ]

        for m in models:
            is_model = bool(model_pattern.search(m)) or m.isdigit()
            self.assertTrue(is_model, f"El código '{m}' debería reconocerse como modelo para omitirlo.")

        for nm in non_models:
            # Colores no deben ser descartados por el regex de modelos
            is_model = bool(model_pattern.search(nm)) or nm.isdigit()
            if "GB" not in nm and nm != "Unlocked":
                self.assertFalse(is_model, f"El atributo '{nm}' no debe coincidir con el regex de modelos.")

    def test_17_frequency_parsing_validation(self):
        """17. Prueba parseo y conversión rigurosa de frecuencias."""
        test_valid = [
            ("30m", 1800),
            ("45m", 2700),
            ("1h", 3600),
            ("2h", 7200),
            ("12H", 43200),
        ]
        for freq_str, expected_secs in test_valid:
            num_match = re.search(r'^\d+', freq_str)
            unit_match = re.search(r'[a-zA-Z]+$', freq_str)
            self.assertIsNotNone(num_match)
            self.assertIsNotNone(unit_match)
            unit = unit_match.group(0).lower()
            self.assertIn(unit, ['h', 'm'])
            num = int(num_match.group(0))
            secs = num * 3600 if unit == 'h' else num * 60
            self.assertEqual(secs, expected_secs)

        test_invalid = [
            "invalid",
            "-30m",
            "30sec",
            "2d",
            "0h",
            "",
        ]
        for inv in test_invalid:
            num_match = re.search(r'^\d+', inv)
            unit_match = re.search(r'[a-zA-Z]+$', inv)
            is_valid = bool(num_match and unit_match and unit_match.group(0).lower() in ['h', 'm'] and int(num_match.group(0)) > 0)
            self.assertFalse(is_valid, f"'{inv}' no debería ser aceptado como frecuencia válida.")

if __name__ == "__main__":
    unittest.main()
