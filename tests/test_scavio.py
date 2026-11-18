import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as radar


class ScavioMockedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        radar.DB_PATH = os.path.join(self.tmp.name, "test.db")
        radar.DB_READY = False
        radar.SCAVIO_ORIGIN = (39.1857, -78.1636)
        os.environ["SCAVIO_API_KEY"] = "unit-test-token"
        self.db = radar.conn()

    def tearDown(self):
        self.db.close()
        radar.DB_READY = False
        radar.SCAVIO_ORIGIN = None
        os.environ.pop("SCAVIO_API_KEY", None)
        self.tmp.cleanup()

    def test_inventory_status_maps_boolean_quantity_and_unknown(self):
        self.assertEqual(radar.scavio_inventory_status(True), "in_stock")
        self.assertEqual(radar.scavio_inventory_status(False), "out_of_stock")
        self.assertEqual(radar.scavio_inventory_status(None, 4), "in_stock")
        self.assertEqual(radar.scavio_inventory_status(None, 0), "out_of_stock")
        self.assertEqual(radar.scavio_inventory_status("available"), "in_stock")
        self.assertEqual(radar.scavio_inventory_status("mystery"), "unknown")

    def test_store_radius_accepts_nearby_and_rejects_outside(self):
        self.assertIsNotNone(radar.scavio_store_distance(39.20, -78.16))
        self.assertIsNone(radar.scavio_store_distance(41.0, -78.16))

    def test_store_and_inventory_upsert(self):
        sid = radar.scavio_upsert_store(self.db, "Costco", {
            "warehouse_id": "123", "warehouse_name": "Winchester Costco",
            "address": "123 Test Rd", "city": "Winchester", "state": "VA",
            "zip": "22601", "latitude": 39.20, "longitude": -78.16
        })
        self.assertIsNotNone(sid)
        product = {"url": "https://www.costco.com/pokemon-tcg.html",
                   "title": "Pokemon TCG Elite Trainer Box"}
        radar.scavio_save_inventory(self.db, sid, product, "in_stock", None,
                                    "Unit count not provided", "Mocked response")
        self.db.commit()
        row = self.db.execute(
            "SELECT status,quantity,quantity_label FROM store_inventory WHERE store_id=?",
            (sid,)).fetchone()
        self.assertEqual(row["status"], "in_stock")
        self.assertIsNone(row["quantity"])
        self.assertEqual(row["quantity_label"], "Unit count not provided")

    @patch.object(radar.requests, "post")
    def test_scavio_post_uses_bearer_token_and_parses_response(self, post):
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"data": {"products": [{"title": "Pokemon TCG"}]},
                        "credits_used": 1}
        post.return_value = Response()
        result = radar.scavio_post("/api/v1/target/search",
                                   {"keyword": "Pokemon trading cards"})
        self.assertEqual(result["data"]["products"][0]["title"], "Pokemon TCG")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         "Bearer unit-test-token")

    @patch.object(radar.requests, "post")
    def test_target_search_excludes_marketplace_and_accepts_first_party(self, post):
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"data": {"products": [
                    {"title": "Pokemon TCG Elite Trainer Box",
                     "url": "https://www.target.com/p/pokemon-etb/-/A-111",
                     "is_marketplace": True, "seller_name": "Third Party"},
                    {"title": "Pokemon TCG Booster Bundle",
                     "url": "https://www.target.com/p/pokemon-bundle/-/A-222",
                     "is_marketplace": False, "seller_name": "Target",
                     "in_stock": True, "item_number": "222"}
                ]}, "credits_used": 1}
        post.return_value = Response()
        results = radar.scavio_products_for_retailer("Target")
        self.assertEqual(len(results), 1)
        self.assertIn("Booster Bundle", results[0]["title"])
        rows = self.db.execute(
            "SELECT title,seller_verified FROM products ORDER BY title").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seller_verified"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
