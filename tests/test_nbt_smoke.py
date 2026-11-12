from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcstructure_merger_v2_features_bundle as app


class NbtSmokeTests(unittest.TestCase):
    def test_empty_lists_keep_valid_element_hints(self):
        for element_type in range(13):
            with self.subTest(element_type=element_type):
                tag = app.Tag(9, (element_type, []))
                self.assertEqual(app.tag_list(tag, expected_element_type=10), [])

    def test_non_empty_lists_keep_strict_type_validation(self):
        tag = app.Tag(9, (1, [app.Tag(1, 7)]))
        self.assertEqual(app.tag_list(tag, expected_element_type=1)[0].value, 7)
        with self.assertRaises(app.NBTError):
            app.tag_list(tag, expected_element_type=10)

    def test_round_trip_all_empty_list_hints(self):
        root = app.Tag(10, {
            f"empty_{element_type}": app.Tag(9, (element_type, []))
            for element_type in range(13)
        })
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty_lists.nbt"
            app.write_nbt(str(path), root, "test")
            name, loaded = app.read_nbt(str(path))
        self.assertEqual(name, "test")
        for element_type in range(13):
            tag = loaded.value[f"empty_{element_type}"]
            self.assertEqual(tag.tag_id, 9)
            self.assertEqual(tag.value, (element_type, []))

    def test_reader_rejects_invalid_list_type_id(self):
        # Root compound: one empty ListTag with an invalid element type 13.
        data = (
            b"\x0a\x00\x00"       # root type + empty root name
            + b"\x09"             # ListTag
            + b"\x07\x00bad"     # name "bad"
            + b"\x0d"             # invalid element type 13
            + b"\x00\x00\x00\x00"  # list length 0
            + b"\x00"             # end compound
        )
        with self.assertRaises(app.NBTError):
            app.read_nbt_bytes(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
