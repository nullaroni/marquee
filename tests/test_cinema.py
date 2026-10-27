import json
import os
import tempfile
import unittest
from unittest import mock

import cinema


class DirectorParseTests(unittest.TestCase):
    def test_normal(self):
        text = "Dune is a 2021 film directed by Denis Villeneuve and starring Timothee."
        self.assertEqual(cinema.director_from_extract(text), "Denis Villeneuve")

    def test_unicode(self):
        self.assertEqual(cinema.director_from_extract("directed by Chloé Zhao"), "Chloé Zhao")

    def test_trailing_sentence(self):
        text = "is a 2019 film directed by Jon Favreau. It stars Donald Glover."
        self.assertEqual(cinema.director_from_extract(text), "Jon Favreau")

    def test_hyphen_and_apostrophe(self):
        self.assertEqual(
            cinema.director_from_extract("directed by Jean-Marc Vallée, starring"), "Jean-Marc Vallée"
        )
        self.assertEqual(cinema.director_from_extract("directed by Don O'Brien."), "Don O'Brien")

    def test_initials(self):
        self.assertEqual(
            cinema.director_from_extract("directed by J. J. Abrams. It stars"), "J. J. Abrams"
        )

    def test_and_stops(self):
        self.assertEqual(
            cinema.director_from_extract("directed by Jon Favreau and Dan Fogelman"), "Jon Favreau"
        )

    def test_none(self):
        self.assertEqual(cinema.director_from_extract("A film about a dog."), "")
        self.assertEqual(cinema.director_from_extract(""), "")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "directors.json")
        for patcher in (
            mock.patch.object(cinema, "DIRECTOR_CACHE", self.path),
            mock.patch.object(cinema, "_directors", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_failure_not_cached(self):
        with mock.patch.object(cinema, "fetch", side_effect=OSError("down")):
            self.assertEqual(cinema._director("Dune"), "")
        self.assertFalse(os.path.exists(self.path))
        self.assertNotIn("Dune", cinema._load_directors())

    def test_no_match_not_cached(self):
        body = json.dumps({"extract": "Nothing useful."}).encode()
        with mock.patch.object(cinema, "fetch", return_value=body):
            self.assertEqual(cinema._director("Dune"), "")
        self.assertFalse(os.path.exists(self.path))

    def test_success_cached(self):
        body = json.dumps({"extract": "directed by Chloé Zhao."}).encode()
        with mock.patch.object(cinema, "fetch", return_value=body):
            self.assertEqual(cinema._director("Eternals"), "Chloé Zhao")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"Eternals": "Chloé Zhao"})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["directors.json"])
        with mock.patch.object(cinema, "fetch", side_effect=AssertionError("no network")):
            self.assertEqual(cinema._director("Eternals"), "Chloé Zhao")

    def test_corrupt_cache(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(cinema._load_directors(), {})

    def test_existing_cache_loads(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            json.dump({"Old": ""}, handle)
        with mock.patch.object(cinema, "fetch", side_effect=OSError("down")):
            self.assertEqual(cinema._director("Old"), "")
        body = json.dumps({"extract": "directed by Ann Lee."}).encode()
        with mock.patch.object(cinema, "fetch", return_value=body):
            self.assertEqual(cinema._director("Old"), "Ann Lee")


if __name__ == "__main__":
    unittest.main()
