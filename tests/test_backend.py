from __future__ import annotations

import unittest
from pathlib import Path

from backend.server import _version_sort_key


class BackendTests(unittest.TestCase):
    def test_version_sort_key_orders_natural_versions(self) -> None:
        versions = [Path("v9-parsed-layout"), Path("v18-full-bodies"), Path("v10-llm-outline")]

        ordered = sorted(versions, key=_version_sort_key)

        self.assertEqual([item.name for item in ordered], ["v9-parsed-layout", "v10-llm-outline", "v18-full-bodies"])


if __name__ == "__main__":
    unittest.main()
