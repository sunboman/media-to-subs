import json
from pathlib import Path
import tempfile
import unittest

from media_to_subs.errors import ValidationError
from media_to_subs.glossary import read_glossary, validate_glossary


class GlossaryTests(unittest.TestCase):
    def test_explicit_json_file_and_snapshot(self):
        source = {"Example": "示例", "猫": "猫咪"}
        self.assertEqual(validate_glossary(source), source)
        self.assertIsNot(validate_glossary(source), source)
        self.assertEqual(validate_glossary(None), {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explicit.json"
            path.write_text('\ufeff' + json.dumps(source), encoding="utf-8")
            self.assertEqual(read_glossary(path), source)

    def test_rejects_invalid_mappings_without_echoing_terms(self):
        for value in [[], "private-marker", {"": "中"}, {"secret": ""},
                      {"secret": 3}, {3: "中"}, {" private-marker": "中"},
                      {"private-marker": "\n中"}, {"private-marker": "a\x00b"}, {"private-marker": "\ud800"}]:
            with self.subTest(value=value), self.assertRaises(ValidationError) as raised:
                validate_glossary(value)
            self.assertNotIn("private-marker", str(raised.exception))

    def test_rejects_duplicate_keys_invalid_utf8_and_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-marker.json"
            for data in [b'{"a":"one","a":"two"}', b'[]', b'\xff', b'{broken']:
                path.write_bytes(data)
                with self.assertRaises(ValidationError) as raised:
                    read_glossary(path)
                self.assertNotIn("private-marker", str(raised.exception))
            path.unlink()
            with self.assertRaises(ValidationError):
                read_glossary(path)


if __name__ == "__main__":
    unittest.main()
