import unittest

from manager.redaction import redact_text


class RedactionTests(unittest.TestCase):
    def test_sensitive_spans_are_removed_without_erasing_public_context(self) -> None:
        value = (
            "Public status. Password: demo-secret. "
            "See https://example.org/home/public. "
            "Local C:\\Users\\lilli\\private.txt. token=another-secret."
        )
        result = redact_text(value)
        self.assertIn("Public status.", result)
        self.assertIn("https://example.org/home/public", result)
        self.assertIn("password: [redacted]", result.lower())
        self.assertIn("token: [redacted]", result.lower())
        self.assertNotIn("demo-secret", result)
        self.assertNotIn("another-secret", result)
        self.assertNotIn("C:\\Users\\lilli", result)
        self.assertNotEqual(result, "[redacted]")


if __name__ == "__main__":
    unittest.main()
