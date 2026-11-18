import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app


class SellerVerificationTests(unittest.TestCase):
    def test_marketplace_retailers_require_exact_first_party_seller(self):
        self.assertEqual(app.verify_retailer_seller("Target", '{"seller":{"name":"Target"}}')[0], True)
        self.assertEqual(app.verify_retailer_seller("Walmart", '{"seller":{"name":"Walmart.com"}}')[0], True)
        self.assertEqual(app.verify_retailer_seller("Target", '{"seller":{"name":"Target Plus"}}')[0], False)
        self.assertEqual(app.verify_retailer_seller("Walmart", '{"seller":{"name":"Marketplace Outlet"}}')[0], False)
        self.assertEqual(app.verify_retailer_seller("Best Buy", "No seller information present")[0], False)

    def test_retailer_owned_non_marketplace_domain_can_be_verified(self):
        self.assertEqual(app.verify_retailer_seller("Costco", "Pokémon trading card product")[0], True)


class MonitoringTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "monitor-test.db")
        app.DB_READY = False
        self.db = app.conn()
        stamp = app.now()
        self.db.execute(
            """INSERT INTO products
               (retailer,title,url,availability,source,first_seen,last_seen,last_checked,
                seller_verified,seller_name,popularity_score,monitor_enabled,
                check_interval_seconds,next_check_at,last_confirmed_availability)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("Target", "Pokémon 30th Celebration ETB",
             "https://www.target.com/p/test/A-12345678", "out_of_stock",
             "retailer product page", stamp, stamp, stamp, 1, "Target",
             35, 1, 300, stamp, "out_of_stock")
        )
        self.db.commit()
        self.product_id = self.db.execute("SELECT id FROM products").fetchone()[0]
        self.db.close()

    def tearDown(self):
        self.tmp.cleanup()
        app.DB_READY = False

    def test_unknown_check_does_not_erase_last_confirmed_state_or_fake_restock(self):
        blocked = SimpleNamespace(ok=False, status_code=403, text="Access denied")
        with patch.object(app, "retailer_get", return_value=blocked):
            result = app.monitor_due_products()
        self.assertEqual(result["unknown"], 1)
        c = app.conn()
        row = c.execute(
            "SELECT availability,last_confirmed_availability FROM products WHERE id=?",
            (self.product_id,)
        ).fetchone()
        self.assertEqual(row["availability"], "unknown")
        self.assertEqual(row["last_confirmed_availability"], "out_of_stock")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM restock_events").fetchone()[0], 0)
        c.execute("UPDATE products SET next_check_at=? WHERE id=?", (app.now(), self.product_id))
        c.commit()
        c.close()

        available = SimpleNamespace(
            ok=True, status_code=200,
            text='<html><script type="application/ld+json">{"seller":{"name":"Target"},"availability":"https://schema.org/InStock"}</script><button>Add to cart</button></html>'
        )
        with patch.object(app, "retailer_get", return_value=available):
            result = app.monitor_due_products()
        self.assertEqual(result["restocks"], 1)
        c = app.conn()
        row = c.execute(
            "SELECT availability,last_confirmed_availability FROM products WHERE id=?",
            (self.product_id,)
        ).fetchone()
        self.assertEqual(row["availability"], "in_stock")
        self.assertEqual(row["last_confirmed_availability"], "in_stock")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM restock_events").fetchone()[0], 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
