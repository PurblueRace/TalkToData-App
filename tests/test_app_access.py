import unittest

from app_access import PUBLIC_WORKSPACE_USERNAME, parse_flag


class AccessControlTests(unittest.TestCase):
    def test_parse_flag_accepts_common_true_and_false_values(self):
        for value in (True, "true", "TRUE", "1", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(parse_flag(value))

        for value in (False, "false", "FALSE", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(parse_flag(value, default=True))

    def test_parse_flag_uses_explicit_default_for_missing_or_invalid_values(self):
        self.assertFalse(parse_flag(None))
        self.assertTrue(parse_flag(None, default=True))
        self.assertTrue(parse_flag("unexpected", default=True))

    def test_public_workspace_uses_a_reserved_non_user_name(self):
        self.assertTrue(PUBLIC_WORKSPACE_USERNAME.startswith("__"))


if __name__ == "__main__":
    unittest.main()
