"""Keep unrelated package changes out of each other's qualification lanes."""
import unittest

from select_checks import select_checks


class SelectChecksTest(unittest.TestCase):
    def test_component_boundaries(self):
        cases = [
            (["sdk/python/openeight/client.py"], (True, False, True)),
            (["sdk/typescript/src/client.ts"], (False, True, False)),
            (["cookbook/README.md"], (False, False, True)),
            (["sdk/contracts/errors.json"], (True, True, True)),
            (["sdk/scripts/check_public_surface.py"], (True, True, True)),
            (["README.md"], (True, True, True)),
            ([".github/workflows/ci.yml"], (True, True, True)),
            (["sdk/typescript/package.json", "cookbook/README.md"], (False, True, True)),
            ([], (False, False, False)),
        ]
        for paths, expected in cases:
            with self.subTest(paths=paths):
                self.assertEqual(tuple(select_checks(paths).values()), expected)


if __name__ == "__main__":
    unittest.main()
