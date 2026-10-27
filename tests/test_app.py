"""HTTP and business-rule smoke tests for LoomOS. Install requirements before running."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TEST_HOME = Path(tempfile.mkdtemp(prefix="loomos-tests-"))
os.environ["LOOMOS_DATABASE"] = str(TEST_HOME / "loomos-test.db")
os.environ["LOOMOS_SECRET_KEY"] = "unit-test-secret-not-for-production"
os.environ["LOOMOS_ENV"] = "development"
os.environ["LOOMOS_SEED_SAMPLE_DATA"] = "1"
sys.path.insert(0, str(PROJECT))
import app as loom  # noqa: E402  (must set the temporary database path first)

loom.app.config.update(TESTING=True, SECRET_KEY="unit-test-secret")
BASELINE_DB = TEST_HOME / "baseline.db"
shutil.copy2(loom.DB_PATH, BASELINE_DB)


class LoomOSAppTests(unittest.TestCase):
    def setUp(self):
        shutil.copy2(BASELINE_DB, loom.DB_PATH)
        self.client = loom.app.test_client()

    def login(self, email="admin@loomos.local", password="LoomOS@2026"):
        return self.client.post("/login", data={"email": email, "password": password}, follow_redirects=True)

    def fetch_one(self, sql, params=()):
        with loom.app.app_context():
            return loom.get_db().execute(sql, params).fetchone()

    def test_admin_login_and_dashboard(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Command Center", response.data)

    def test_protected_page_redirects_to_login(self):
        response = self.client.get("/inventory")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_sales_role_is_blocked_from_inventory(self):
        self.login("sales@loomos.local", "Sales@2026")
        response = self.client.get("/inventory")
        self.assertEqual(response.status_code, 403)

    def test_stock_receipt_updates_balance_and_ledger(self):
        self.login()
        item = self.fetch_one("SELECT id,on_hand FROM inventory_items ORDER BY id LIMIT 1")
        before_ledger = self.fetch_one("SELECT COUNT(*) FROM inventory_movements WHERE inventory_item_id=?", (item["id"],))[0]
        response = self.client.post(f"/actions/inventory/{item['id']}/movement", data={"movement_type":"Receipt", "quantity":"5", "reference":"TEST-RECEIPT"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        after = self.fetch_one("SELECT on_hand FROM inventory_items WHERE id=?", (item["id"],))[0]
        after_ledger = self.fetch_one("SELECT COUNT(*) FROM inventory_movements WHERE inventory_item_id=?", (item["id"],))[0]
        self.assertAlmostEqual(after, item["on_hand"] + 5)
        self.assertEqual(after_ledger, before_ledger + 1)

    def test_issue_cannot_make_available_stock_negative(self):
        self.login()
        item = self.fetch_one("SELECT id,on_hand,reserved FROM inventory_items ORDER BY id LIMIT 1")
        before = item["on_hand"]
        response = self.client.post(f"/actions/inventory/{item['id']}/movement", data={"movement_type":"Issue", "quantity":str(item["on_hand"] + 1000)}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        after = self.fetch_one("SELECT on_hand FROM inventory_items WHERE id=?", (item["id"],))[0]
        self.assertAlmostEqual(after, before)

    def test_completed_production_posts_finished_goods_to_inventory_ledger(self):
        self.login("production@loomos.local", "Factory@2026")
        batch = self.fetch_one("SELECT id,batch_no,planned_qty,completed_qty,rejected_qty FROM production_batches WHERE finished_qty_posted=0 ORDER BY id LIMIT 1")
        self.assertIsNotNone(batch)
        self.assertLess(batch["completed_qty"] + batch["rejected_qty"], batch["planned_qty"])
        response = self.client.post(f"/actions/production/{batch['id']}/update", data={
            "completed_delta": "1", "rejected_delta": "0", "stage": "Completed", "notes": "Production completion test"
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        movement = self.fetch_one("SELECT inventory_item_id,qty_in FROM inventory_movements WHERE movement_type='Production Output' AND reference=?", (batch["batch_no"],))
        self.assertIsNotNone(movement)
        self.assertGreater(movement["qty_in"], 0)
        posted = self.fetch_one("SELECT finished_qty_posted FROM production_batches WHERE id=?", (batch["id"],))[0]
        self.assertGreater(posted, 0)

    def test_partial_dispatch_deducts_stock_and_finishes_order_after_all_pieces(self):
        self.login()
        order = self.fetch_one("SELECT id,order_no,quantity FROM sales_orders WHERE order_no='ORD-26046'")
        with loom.app.app_context():
            db = loom.get_db()
            old_shipments = db.execute("SELECT id FROM shipments WHERE order_id=?", (order["id"],)).fetchall()
            for old in old_shipments:
                db.execute("DELETE FROM shipment_events WHERE shipment_id=?", (old["id"],))
                db.execute("DELETE FROM shipments WHERE id=?", (old["id"],))
            db.execute("UPDATE sales_orders SET stage='Ready for Dispatch' WHERE id=?", (order["id"],))
            db.execute("INSERT INTO inventory_items(item_code,name,category,unit,on_hand,reserved,reorder_level,safety_stock,avg_cost,warehouse,last_movement) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (f"FG-ORD-{order['id']}", "Test finished garments", "Finished garments", "pcs", order["quantity"], 0, 0, 0, 0, "Finished Goods", loom.today()))
            db.commit()
        first = self.client.post("/actions/shipment", data={"order_id": str(order["id"]), "quantity": "100", "carrier": "Customer pickup"}, follow_redirects=True)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.fetch_one("SELECT on_hand FROM inventory_items WHERE item_code=?", (f"FG-ORD-{order['id']}",))[0], order["quantity"] - 100)
        self.assertEqual(self.fetch_one("SELECT stage FROM sales_orders WHERE id=?", (order["id"],))[0], "Ready for Dispatch")
        second_qty = order["quantity"] - 100
        second = self.client.post("/actions/shipment", data={"order_id": str(order["id"]), "quantity": str(second_qty), "carrier": "Customer pickup"}, follow_redirects=True)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.fetch_one("SELECT stage FROM sales_orders WHERE id=?", (order["id"],))[0], "Dispatched")
        self.assertEqual(self.fetch_one("SELECT COALESCE(SUM(quantity),0) FROM shipments WHERE order_id=?", (order["id"],))[0], order["quantity"])
        self.assertEqual(self.fetch_one("SELECT on_hand FROM inventory_items WHERE item_code=?", (f"FG-ORD-{order['id']}",))[0], 0)

    def test_quality_action_can_be_resolved_with_documented_notes(self):
        self.login("production@loomos.local", "Factory@2026")
        inspection = self.fetch_one("SELECT id,inspection_no FROM quality_inspections WHERE status='Action Required' LIMIT 1")
        self.assertIsNotNone(inspection)
        response = self.client.post(f"/actions/quality/{inspection['id']}/resolve", data={"resolution":"Reworked rejected units and verified measurements."}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fetch_one("SELECT status FROM quality_inspections WHERE id=?", (inspection["id"],))[0], "Resolved")

    def test_public_tracking_requires_a_valid_token(self):
        response = self.client.get("/track/not-a-real-token")
        self.assertEqual(response.status_code, 404)

    def test_accepted_quotation_cannot_be_converted_twice(self):
        self.login()
        quote = self.fetch_one("SELECT id FROM quotations WHERE converted_order_id IS NOT NULL LIMIT 1")
        before = self.fetch_one("SELECT COUNT(*) FROM sales_orders")[0]
        response = self.client.post(f"/actions/quotation/{quote['id']}/convert", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        after = self.fetch_one("SELECT COUNT(*) FROM sales_orders")[0]
        self.assertEqual(after, before)

    def test_invoice_overpayment_is_rejected(self):
        self.login()
        invoice = self.fetch_one("SELECT id,total,paid FROM invoices WHERE total>paid LIMIT 1")
        before_paid = invoice["paid"]
        amount = invoice["total"] - invoice["paid"] + 100
        response = self.client.post(f"/actions/invoice/{invoice['id']}/payment", data={"amount":str(amount)}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        after_paid = self.fetch_one("SELECT paid FROM invoices WHERE id=?", (invoice["id"],))[0]
        self.assertAlmostEqual(after_paid, before_paid)


    def customer_login(self, email="rhea@northstar.example", password="Customer@2026"):
        return self.client.post("/customer/login", data={"email": email, "password": password}, follow_redirects=True)

    def test_customer_portal_is_separate_and_shows_only_own_orders(self):
        response = self.customer_login()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"My orders", response.data)
        self.assertIn(b"ORD-26041", response.data)
        self.assertNotIn(b"ORD-26042", response.data)

    def test_customer_cannot_open_another_customers_order(self):
        self.customer_login()
        with loom.app.app_context():
            other_order = loom.get_db().execute("SELECT id FROM sales_orders WHERE order_no='ORD-26042'").fetchone()[0]
        response = self.client.get(f"/portal/orders/{other_order}")
        self.assertEqual(response.status_code, 404)

    def test_customer_update_request_is_visible_to_company(self):
        self.customer_login()
        with loom.app.app_context():
            own_order = loom.get_db().execute("SELECT id FROM sales_orders WHERE order_no='ORD-26041'").fetchone()[0]
        post = self.client.post(f"/portal/orders/{own_order}/request-update", data={
            "request_type": "Delivery date", "message": "Please confirm our expected delivery date."
        }, follow_redirects=True)
        self.assertEqual(post.status_code, 200)
        with loom.app.app_context():
            req = loom.get_db().execute("SELECT id FROM customer_update_requests WHERE message=?", ("Please confirm our expected delivery date.",)).fetchone()
            self.assertIsNotNone(req)
        self.client.post("/customer/logout", follow_redirects=True)
        self.login()
        queue = self.client.get("/customer-requests")
        self.assertEqual(queue.status_code, 200)
        self.assertIn(b"Please confirm our expected delivery date.", queue.data)

    def test_health_endpoint_reports_workspace_configuration(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"environment":"development"', response.data)


if __name__ == "__main__":
    unittest.main()
