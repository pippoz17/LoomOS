PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, type TEXT NOT NULL,
  segment TEXT NOT NULL DEFAULT 'Standard', city TEXT, state TEXT, contact_name TEXT, email TEXT, phone TEXT,
  credit_limit REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customer_portal_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL UNIQUE,
  email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, contact_name TEXT, email TEXT, source TEXT,
  stage TEXT NOT NULL DEFAULT 'New Lead', value REAL NOT NULL DEFAULT 0, salesperson TEXT, followup_date TEXT,
  notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS enquiries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, enquiry_no TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL,
  product_category TEXT, fabric_type TEXT, garment_type TEXT, quantity INTEGER NOT NULL DEFAULT 0,
  sizes TEXT, colour TEXT, gsm TEXT, target_price REAL DEFAULT 0, delivery_date TEXT, status TEXT DEFAULT 'Open', notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS quotations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, quote_no TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL,
  enquiry_id INTEGER, product_name TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL,
  discount_pct REAL NOT NULL DEFAULT 0, tax_pct REAL NOT NULL DEFAULT 5, valid_until TEXT, status TEXT NOT NULL DEFAULT 'Draft',
  payment_terms TEXT DEFAULT '30 days', delivery_terms TEXT DEFAULT 'FOB Chennai', converted_order_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(enquiry_id) REFERENCES enquiries(id)
);
CREATE TABLE IF NOT EXISTS sales_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT NOT NULL UNIQUE, customer_id INTEGER NOT NULL,
  quotation_id INTEGER, product_name TEXT NOT NULL, sku TEXT, colour TEXT, size_breakdown TEXT,
  quantity INTEGER NOT NULL, unit_price REAL NOT NULL, tax_pct REAL NOT NULL DEFAULT 5, total_value REAL NOT NULL,
  order_date TEXT NOT NULL, promised_date TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'Order Confirmed', priority TEXT NOT NULL DEFAULT 'Normal',
  notes TEXT, tracking_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(customer_id) REFERENCES customers(id), FOREIGN KEY(quotation_id) REFERENCES quotations(id)
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category TEXT,
  city TEXT, lead_time_days INTEGER NOT NULL DEFAULT 7, rating REAL NOT NULL DEFAULT 4.0, email TEXT, phone TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inventory_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category TEXT NOT NULL,
  supplier_id INTEGER, unit TEXT NOT NULL DEFAULT 'pcs', on_hand REAL NOT NULL DEFAULT 0, reserved REAL NOT NULL DEFAULT 0,
  reorder_level REAL NOT NULL DEFAULT 0, safety_stock REAL NOT NULL DEFAULT 0, avg_cost REAL NOT NULL DEFAULT 0,
  warehouse TEXT DEFAULT 'Main Warehouse', location TEXT, batch_no TEXT, colour TEXT, gsm TEXT, last_movement TEXT,
  FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
);
CREATE TABLE IF NOT EXISTS inventory_movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_item_id INTEGER NOT NULL, date TEXT NOT NULL,
  movement_type TEXT NOT NULL, qty_in REAL NOT NULL DEFAULT 0, qty_out REAL NOT NULL DEFAULT 0,
  reference TEXT, source TEXT, destination TEXT, user_id INTEGER, notes TEXT,
  FOREIGN KEY(inventory_item_id) REFERENCES inventory_items(id), FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS production_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_no TEXT NOT NULL UNIQUE, sales_order_id INTEGER NOT NULL,
  product_name TEXT NOT NULL, planned_qty INTEGER NOT NULL, completed_qty INTEGER NOT NULL DEFAULT 0,
  rejected_qty INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'Material Preparation', start_date TEXT,
  target_date TEXT, line_name TEXT, supervisor TEXT, priority TEXT DEFAULT 'Normal', downtime_reason TEXT, finished_qty_posted INTEGER NOT NULL DEFAULT 0, notes TEXT,
  FOREIGN KEY(sales_order_id) REFERENCES sales_orders(id)
);
CREATE TABLE IF NOT EXISTS production_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, date TEXT NOT NULL, stage TEXT NOT NULL,
  completed_delta INTEGER NOT NULL DEFAULT 0, rejected_delta INTEGER NOT NULL DEFAULT 0, downtime_minutes INTEGER DEFAULT 0,
  defect_reason TEXT, notes TEXT, user_id INTEGER, FOREIGN KEY(batch_id) REFERENCES production_batches(id),
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS quality_inspections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, inspection_no TEXT NOT NULL UNIQUE, batch_id INTEGER, order_id INTEGER,
  stage TEXT NOT NULL, sample_size INTEGER NOT NULL DEFAULT 0, units_inspected INTEGER NOT NULL DEFAULT 0,
  passed_units INTEGER NOT NULL DEFAULT 0, rejected_units INTEGER NOT NULL DEFAULT 0, defect_category TEXT,
  severity TEXT DEFAULT 'Low', status TEXT DEFAULT 'Pending', notes TEXT, corrective_action TEXT, inspected_at TEXT,
  FOREIGN KEY(batch_id) REFERENCES production_batches(id), FOREIGN KEY(order_id) REFERENCES sales_orders(id)
);
CREATE TABLE IF NOT EXISTS purchase_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, po_no TEXT NOT NULL UNIQUE, supplier_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL, quantity REAL NOT NULL, received_qty REAL NOT NULL DEFAULT 0, unit_cost REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'Pending Approval', expected_date TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(supplier_id) REFERENCES suppliers(id), FOREIGN KEY(item_id) REFERENCES inventory_items(id)
);
CREATE TABLE IF NOT EXISTS shipments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_no TEXT NOT NULL UNIQUE, order_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0, carrier TEXT, tracking_no TEXT, carton_count INTEGER DEFAULT 0, dispatch_date TEXT, expected_arrival TEXT,
  status TEXT NOT NULL DEFAULT 'Ready to Dispatch', delivered_at TEXT, delivery_address TEXT, proof_ref TEXT,
  FOREIGN KEY(order_id) REFERENCES sales_orders(id)
);
CREATE TABLE IF NOT EXISTS shipment_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL, event_date TEXT NOT NULL,
  status TEXT NOT NULL, location TEXT, note TEXT, FOREIGN KEY(shipment_id) REFERENCES shipments(id)
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT NOT NULL UNIQUE, order_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
  issue_date TEXT NOT NULL, due_date TEXT NOT NULL, total REAL NOT NULL, paid REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Issued',
  FOREIGN KEY(order_id) REFERENCES sales_orders(id), FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL,
  method TEXT DEFAULT 'Bank Transfer', reference TEXT, FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT, due_date TEXT, priority TEXT DEFAULT 'Normal',
  status TEXT DEFAULT 'Open', assigned_to TEXT, category TEXT DEFAULT 'Operations', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customer_update_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL, request_type TEXT NOT NULL, message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT, handled_by INTEGER,
  FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(order_id) REFERENCES sales_orders(id),
  FOREIGN KEY(handled_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_requests_status ON customer_update_requests(status,created_at);
CREATE INDEX IF NOT EXISTS idx_customer_requests_customer ON customer_update_requests(customer_id,created_at);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, entity TEXT NOT NULL,
  entity_id INTEGER, details TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_passports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, passport_code TEXT NOT NULL UNIQUE, order_id INTEGER, batch_id INTEGER,
  style_code TEXT, composition TEXT, colour TEXT, size_range TEXT, care_instructions TEXT, certification TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(order_id) REFERENCES sales_orders(id), FOREIGN KEY(batch_id) REFERENCES production_batches(id)
);
CREATE INDEX IF NOT EXISTS idx_orders_stage ON sales_orders(stage);
CREATE INDEX IF NOT EXISTS idx_orders_due ON sales_orders(promised_date);
CREATE INDEX IF NOT EXISTS idx_inventory_code ON inventory_items(item_code);
CREATE INDEX IF NOT EXISTS idx_movements_item_date ON inventory_movements(inventory_item_id, date);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
CREATE TABLE IF NOT EXISTS sustainability_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entry_date TEXT NOT NULL, resource_type TEXT NOT NULL,
  quantity REAL NOT NULL, unit TEXT NOT NULL, category TEXT, batch_id INTEGER, reusable_qty REAL NOT NULL DEFAULT 0,
  notes TEXT, entered_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(batch_id) REFERENCES production_batches(id), FOREIGN KEY(entered_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_sustainability_date ON sustainability_entries(entry_date,resource_type);
