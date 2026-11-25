"""The shipped source CAS must admit the complete last deployed release."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PreviousSourceUpgrade(unittest.TestCase):
    def test_all_v35_source_bytes_are_either_unchanged_or_explicitly_admitted(self):
        previous = json.loads((ROOT/'tests/fixtures/native-v35-source-pins.json').read_text())
        self.assertEqual(previous['commit'], 'afc7029392486b14751fbd52a60a8030f2e58ff7')
        current = json.loads((ROOT/'nativebundle/manifest.json').read_text())
        rows = {r['path']: r for r in current['files']}
        self.assertEqual(len(previous['files']), 80)
        for name, old in previous['files'].items():
            with self.subTest(path=name):
                row = rows[name]
                self.assertEqual(hashlib.sha256((ROOT/'nativebundle/source'/name).read_bytes()).hexdigest(),
                                 row['sha256'])
                self.assertTrue(old == row['sha256'] or old in row['before_sha256'],
                                'Previously installed source would be rejected by native bundle CAS')


if __name__ == '__main__':
    unittest.main()
