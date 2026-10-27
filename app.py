from __future__ import annotations
import csv, io, json, os, secrets, sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from io import BytesIO
from markupsafe import Markup, escape
import qrcode
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, session, url_for, flash, abort, send_file
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
_ON_VERCEL = bool(os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV'))
_DEFAULT_DB_PATH = Path('/tmp/loomos.db') if _ON_VERCEL else BASE_DIR / 'instance' / 'loomos.db'
DB_PATH = Path(os.environ.get('LOOMOS_DATABASE', _DEFAULT_DB_PATH))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# Vercel serves files in /public through its CDN. Keep Flask's static endpoint
# aligned with that folder so templates continue to generate /static/... URLs.
app = Flask(__name__, static_folder='public/static', static_url_path='/static')
APP_ENV = os.environ.get('LOOMOS_ENV', 'development').strip().lower()
_configured_secret = os.environ.get('LOOMOS_SECRET_KEY', '').strip()
if APP_ENV == 'production' and len(_configured_secret) < 32:
    raise RuntimeError('Set LOOMOS_SECRET_KEY to a unique random value of at least 32 characters before production startup.')
app.secret_key = _configured_secret or secrets.token_hex(32)
app.config.update(
    TEMPLATES_AUTO_RELOAD=(APP_ENV != 'production'),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('LOOMOS_COOKIE_SECURE', '0') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

ORDER_STAGES = ['Enquiry Received', 'Quotation Approved', 'Order Confirmed', 'Materials Allocated', 'Cutting', 'Stitching', 'Finishing', 'Quality Inspection', 'Packing', 'Ready for Dispatch', 'Dispatched', 'Delivered']
CRM_STAGES = ['New Lead', 'Contacted', 'Qualified', 'Sample Requested', 'Quotation Sent', 'Negotiation', 'Won', 'Lost']
PRODUCTION_STAGES = ['Material Preparation', 'Cutting', 'Stitching', 'Finishing', 'Quality Control', 'Packing', 'Completed']
NAV_GROUPS = [
    ('OVERVIEW', [('Dashboard','/','◫'),('Executive Analytics','/analytics','◧')]),
    ('CUSTOMER & SALES', [('CRM Pipeline','/crm','◎'),('Customers','/customers','♙'),('Enquiries & Quotations','/quotations','▤'),('Sales Orders','/orders','⇄'),('Customer Requests','/customer-requests','✉')]),
    ('OPERATIONS', [('Production Planning','/production','▦'),('Production Floor','/production-floor','⌁'),('Inventory & Warehouse','/inventory','▧'),('Quality Control','/quality','✓'),('Dispatch & Logistics','/dispatch','↗')]),
    ('BUSINESS', [('Procurement & Suppliers','/procurement','⇣'),('Invoices & Payments','/finance','₹'),('Reports & Exports','/reports','▥')]),
    ('INTELLIGENCE', [('Textile Intelligence','/intelligence','✳'),('What-if Simulator','/simulator','∿'),('Sustainability','/sustainability','◇'),('Product Passports','/passports','⌗')]),
    ('SYSTEM', [('Notifications & Tasks','/tasks','♧'),('User Management','/users','♙'),('Settings','/settings','⚙')]),
]

ROLE_RULES = {
    'Administrator': {'*'},
    'Sales Manager': {'dashboard','analytics','crm','customers','quotations','orders','customer-requests','reports','tasks','intelligence','passports'},
    'Sales Executive': {'dashboard','crm','customers','quotations','orders','customer-requests','tasks','passports'},
    'Production Manager': {'dashboard','analytics','orders','production','production-floor','inventory','quality','dispatch','reports','tasks','intelligence','simulator','sustainability','passports'},
    'Production Operator': {'dashboard','production-floor','tasks'},
    'Inventory Manager': {'dashboard','inventory','procurement','production','reports','tasks','intelligence','simulator'},
    'Quality Inspector': {'dashboard','quality','production','orders','tasks','reports'},
    'Procurement Manager': {'dashboard','inventory','procurement','reports','tasks','intelligence'},
    'Finance Executive': {'dashboard','finance','customers','orders','reports','tasks'},
    'Dispatch Manager': {'dashboard','orders','dispatch','passports','tasks','reports'},
    'Read-only Executive': {'dashboard','analytics','customers','orders','inventory','production','quality','dispatch','finance','reports','intelligence','sustainability','passports','tasks'},
}

# ---------- database ----------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db

@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop('db', None)
    if db is not None: db.close()

def rows(sql, params=()): return [dict(r) for r in get_db().execute(sql, params).fetchall()]
def row(sql, params=()):
    r = get_db().execute(sql, params).fetchone()
    return dict(r) if r else None

def write(sql, params=()):
    db = get_db(); cur = db.execute(sql, params); db.commit(); return cur.lastrowid

def today(): return date.today().isoformat()
def dt_days(n=0): return (date.today() + timedelta(days=n)).isoformat()
def money(value): return f"₹{float(value or 0):,.0f}"
app.jinja_env.filters['money'] = money
app.jinja_env.filters['date_short'] = lambda v: (str(v)[:10] if v else '—')

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.executescript((BASE_DIR / 'schema.sql').read_text(encoding='utf-8'))
        # Backward-compatible migrations for workspaces created by an earlier LoomOS build.
        batch_columns = {r[1] for r in conn.execute('PRAGMA table_info(production_batches)')}
        if 'finished_qty_posted' not in batch_columns:
            conn.execute('ALTER TABLE production_batches ADD COLUMN finished_qty_posted INTEGER NOT NULL DEFAULT 0')
        shipment_columns = {r[1] for r in conn.execute('PRAGMA table_info(shipments)')}
        shipment_quantity_migrated = 'quantity' not in shipment_columns
        if shipment_quantity_migrated:
            conn.execute('ALTER TABLE shipments ADD COLUMN quantity INTEGER NOT NULL DEFAULT 0')
            # Legacy shipments were all-or-nothing records; preserve them as full-order dispatches.
            conn.execute('UPDATE shipments SET quantity=(SELECT quantity FROM sales_orders WHERE sales_orders.id=shipments.order_id) WHERE quantity=0')
        defaults = [
            ('company_name', 'LoomOS Textiles'), ('company_email', ''), ('company_phone', ''),
            ('currency', 'INR — ₹'), ('timezone', 'Asia/Kolkata'), ('theme', 'Forest')
        ]
        conn.executemany('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', defaults)
    # A real workspace starts empty. Synthetic fixture records can only be loaded explicitly.
    if os.environ.get('LOOMOS_SEED_SAMPLE_DATA', '0').strip() == '1':
        seed_db()
        seed_customer_portal_accounts()

def seed_customer_portal_accounts():
    """Create portal accounts only for explicitly seeded development fixtures."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    customers = db.execute("SELECT id,email FROM customers WHERE email IS NOT NULL AND trim(email) <> ''").fetchall()
    for customer in customers:
        email = customer['email'].strip().lower()
        exists = db.execute('SELECT id FROM customer_portal_users WHERE customer_id=? OR lower(email)=?',
                            (customer['id'], email)).fetchone()
        if not exists:
            db.execute('INSERT INTO customer_portal_users(customer_id,email,password_hash) VALUES(?,?,?)',
                       (customer['id'], email, generate_password_hash('Customer@2026')))
    db.commit()
    db.close()

def seed_db():
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
        db.close(); return
    def ex(sql, p=()): return db.execute(sql, p).lastrowid
    # Optional local fixture identities. This routine runs only when fixture seeding is explicitly enabled.
    users = [
      ('Afeef Aleem','admin@loomos.local','LoomOS@2026','Administrator'),
      ('Meera Krishnan','sales@loomos.local','Sales@2026','Sales Manager'),
      ('Arun Prakash','production@loomos.local','Factory@2026','Production Manager'),
      ('Nisha Raman','inventory@loomos.local','Stock@2026','Inventory Manager'),
      ('Kavin Raj','finance@loomos.local','Finance@2026','Finance Executive'),
      ('Priya Nair','quality@loomos.local','Quality@2026','Quality Inspector'),
    ]
    for name,email,pwd,role in users:
        ex('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',(name,email,generate_password_hash(pwd),role))
    customer_seed = [
      ('CUS-001','Northstar Apparel','Brand','Strategic','Bengaluru','Karnataka','Rhea Menon','rhea@northstar.example','+91 98710 11001',1200000),
      ('CUS-002','Cobalt Retail Group','Retailer','Strategic','Chennai','Tamil Nadu','Vikram Rao','vikram@cobalt.example','+91 98710 11002',850000),
      ('CUS-003','Meridian Exports','Exporter','Premium','Tiruppur','Tamil Nadu','Sanjay Kumar','sanjay@meridian.example','+91 98710 11003',1800000),
      ('CUS-004','Little Loom Kids','Brand','Growth','Coimbatore','Tamil Nadu','Ananya Iyer','ananya@littleloom.example','+91 98710 11004',500000),
      ('CUS-005','Urban Thread Co.','Wholesaler','Premium','Mumbai','Maharashtra','Rohan Shah','rohan@urbanthread.example','+91 98710 11005',950000),
      ('CUS-006','Cedar Uniforms','Manufacturer','Growth','Hyderabad','Telangana','Farah Khan','farah@cedar.example','+91 98710 11006',650000),
      ('CUS-007','Blue Finch Studio','Brand','Standard','Pune','Maharashtra','Dev Kulkarni','dev@bluefinch.example','+91 98710 11007',350000),
      ('CUS-008','Atlas Workwear','Wholesaler','Premium','Delhi','Delhi','Simran Gill','simran@atlas.example','+91 98710 11008',800000),
      ('CUS-009','Palm & Pine','Brand','Growth','Kochi','Kerala','Naveen Thomas','naveen@palmpine.example','+91 98710 11009',420000),
      ('CUS-010','Redline Sportswear','Retailer','Strategic','Noida','Uttar Pradesh','Aarav Sethi','aarav@redline.example','+91 98710 11010',1100000),
      ('CUS-011','Eden School Supplies','Wholesaler','Standard','Chennai','Tamil Nadu','Madhavi S','madhavi@eden.example','+91 98710 11011',300000),
      ('CUS-012','Threadline International','Exporter','Strategic','Mumbai','Maharashtra','Kabir Das','kabir@threadline.example','+91 98710 11012',2200000),
    ]
    for c in customer_seed: ex('INSERT INTO customers(code,name,type,segment,city,state,contact_name,email,phone,credit_limit) VALUES(?,?,?,?,?,?,?,?,?,?)',c)
    supplier_seed = [
      ('SUP-001','Kaveri Cotton Mills','Cotton Fabric','Coimbatore',9,4.7,'sales@kavericotton.example','+91 422 400 1001'),
      ('SUP-002','Prime Trims & Accessories','Trims','Chennai',5,4.4,'orders@primetrims.example','+91 44 4000 1002'),
      ('SUP-003','Southern Dye Works','Dyeing','Tiruppur',12,4.2,'team@southerndye.example','+91 421 400 1003'),
      ('SUP-004','WeaveRight Textiles','Greige Fabric','Erode',7,4.5,'sales@weaveright.example','+91 424 400 1004'),
      ('SUP-005','Packform Industrial','Packaging','Bengaluru',6,4.1,'hello@packform.example','+91 80 4000 1005'),
    ]
    for s in supplier_seed: ex('INSERT INTO suppliers(code,name,category,city,lead_time_days,rating,email,phone) VALUES(?,?,?,?,?,?,?,?)',s)
    item_seed = [
      ('YRN-001','Combed Cotton Yarn 30s','Yarn','kg',620,180,160,245,1,'A-01','Y30-COT-24','Natural','—'),
      ('FAB-001','Greige Cotton Fabric 180 GSM','Greige fabric','m',4200,800,700,96,4,'F-02','GF180-07','Ecru','180'),
      ('FAB-002','Dyed Jersey — Bottle Green','Dyed fabric','m',1850,300,350,134,3,'F-04','DJ-BG-12','Bottle Green','180'),
      ('FAB-003','Printed Cotton — Micro Floral','Printed fabric','m',940,160,180,168,3,'F-07','PC-MF-05','Multi','140'),
      ('FAB-004','Finished Poplin 120 GSM','Finished fabric','m',2800,500,480,118,4,'F-03','FP120-14','White','120'),
      ('THR-001','Poly Core Sewing Thread','Thread','cone',780,140,120,42,2,'T-01','TH-PC-08','Black','—'),
      ('TRM-001','Resin Buttons 18L','Buttons','pcs',12500,2000,1800,1.8,2,'T-04','BT-18-11','White','—'),
      ('TRM-002','Nylon Zipper 18 cm','Zippers','pcs',5400,800,900,6.5,2,'T-05','ZP-18-06','Black','—'),
      ('TRM-003','Woven Brand Labels','Labels','pcs',6800,1200,1000,2.4,2,'T-06','LBL-WV-15','Custom','—'),
      ('PKG-001','Corrugated Carton 5-ply','Packaging','pcs',1150,300,250,38,5,'P-02','CT-5P-22','Brown','—'),
      ('PKG-002','Recycled Poly Mailer','Packaging','pcs',3200,500,600,7.5,5,'P-03','PM-RC-09','Grey','—'),
      ('GAR-001','Cotton Crew T-shirt — Navy','Finished garments','pcs',720,100,130,215,1,'G-01','TS-NV-24','Navy','180'),
      ('GAR-002','Pique Polo — White','Finished garments','pcs',460,80,100,295,1,'G-02','PL-WH-18','White','220'),
      ('GAR-003','School Shirt — White','Finished garments','pcs',1100,220,200,185,1,'G-03','SS-WH-31','White','120'),
      ('GAR-004','Performance Hoodie — Charcoal','Finished garments','pcs',260,60,75,480,1,'G-04','HD-CH-10','Charcoal','320'),
      ('FAB-005','Organic Cotton Jersey','Finished fabric','m',970,250,220,154,1,'F-05','OCJ-11','Undyed','200'),
      ('TRM-004','Elastic Waistband 30 mm','Accessories','m',610,100,140,12,2,'T-07','EL-30-04','Black','—'),
      ('TRM-005','Embroidery Backing Roll','Accessories','m',145,30,40,85,2,'T-08','EB-RL-02','White','—'),
      ('PKG-003','Hang Tag — Kraft','Packaging','pcs',1700,400,350,3.2,5,'P-04','HT-KR-04','Kraft','—'),
      ('FAB-006','Denim Twill 10 oz','Greige fabric','m',680,120,160,225,4,'F-08','DT-10-09','Indigo','340'),
    ]
    for i in item_seed:
        code,name,cat,unit,qty,res,level,cost,sid,loc,batch,colour,gsm = i
        ex('INSERT INTO inventory_items(item_code,name,category,supplier_id,unit,on_hand,reserved,reorder_level,safety_stock,avg_cost,warehouse,location,batch_no,colour,gsm,last_movement) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
           (code,name,cat,sid,unit,qty,res,level,level*.5,cost,'Main Warehouse',loc,batch,colour,gsm,dt_days(-2)))
    order_seed = [
      ('ORD-26041',1,None,'Cotton Crew T-shirt','TS-180-NV','Navy','S:400 / M:700 / L:600 / XL:300',2000,215,5,-2,18,'Stitching','High'),
      ('ORD-26042',3,None,'Organic Cotton Tee','OC-TEE-01','Undyed','S:300 / M:500 / L:400',1200,265,5,-5,14,'Cutting','Critical'),
      ('ORD-26043',6,None,'Corporate Polo Shirt','POLO-WH-02','White','S:150 / M:250 / L:250 / XL:150',800,295,5,1,21,'Finishing','Normal'),
      ('ORD-26044',4,None,'Kids Printed T-shirt','KID-PR-44','Micro floral','2Y:200 / 4Y:300 / 6Y:300',800,185,5,3,12,'Materials Allocated','Normal'),
      ('ORD-26045',8,None,'Industrial Workwear Set','WW-SET-08','Charcoal','M:200 / L:400 / XL:300',900,620,5,7,26,'Quality Inspection','High'),
      ('ORD-26046',10,None,'Performance Hoodie','HD-CH-10','Charcoal','S:120 / M:200 / L:200 / XL:80',600,480,5,10,16,'Ready for Dispatch','Normal'),
      ('ORD-26047',2,None,'Retail Oxford Shirt','OX-WH-21','White','S:200 / M:400 / L:400',1000,340,5,12,11,'Dispatched','Normal'),
      ('ORD-26048',12,None,'Export Cotton Polo','EX-PO-12','Bottle Green','S:600 / M:900 / L:900 / XL:600',3000,285,5,16,24,'Stitching','High'),
      ('ORD-26049',11,None,'School Uniform Shirt','SCH-WH-15','White','28:150 / 30:200 / 32:250 / 34:200',800,185,5,5,9,'Delivered','Normal'),
      ('ORD-26050',5,None,'Premium Woven Shirt','WV-SH-05','Sky Blue','S:150 / M:300 / L:300 / XL:150',900,410,5,8,19,'Packing','Normal'),
    ]
    order_ids=[]
    for no,cid,qid,prod,sku,col,sizes,qty,price,tax,odue,days,stage,priority in order_seed:
        order_date=dt_days(-14+len(order_ids))
        promised=dt_days(odue)
        amount=round(qty*price*(1+tax/100),2)
        oid=ex('INSERT INTO sales_orders(order_no,customer_id,quotation_id,product_name,sku,colour,size_breakdown,quantity,unit_price,tax_pct,total_value,order_date,promised_date,stage,priority,notes,tracking_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (no,cid,qid,prod,sku,col,sizes,qty,price,tax,amount,order_date,promised,stage,priority,'Production and packing to approved tech pack.',secrets.token_urlsafe(18)))
        order_ids.append(oid)
    batch_seed = [
      ('BTH-8841',order_ids[0],2000,1120,22,'Stitching',-8,0,'Line A','R. Kumar','High'),
      ('BTH-8842',order_ids[1],1200,390,18,'Cutting',-7,0,'Line B','S. Meena','Critical'),
      ('BTH-8843',order_ids[2],800,760,8,'Finishing',-6,5,'Line C','M. Prakash','Normal'),
      ('BTH-8844',order_ids[3],800,120,4,'Material Preparation',-3,6,'Line A','R. Kumar','Normal'),
      ('BTH-8845',order_ids[4],900,810,24,'Quality Control',-10,4,'Line D','K. Joseph','High'),
      ('BTH-8846',order_ids[7],3000,1380,45,'Stitching',-4,13,'Line B','S. Meena','High'),
    ]
    batch_ids=[]
    for no,oid,planned,done,reject,stage,start,finish,line,sup,pri in batch_seed:
        batch_ids.append(ex('INSERT INTO production_batches(batch_no,sales_order_id,product_name,planned_qty,completed_qty,rejected_qty,stage,start_date,target_date,line_name,supervisor,priority,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (no,oid,'',planned,done,reject,stage,dt_days(start),dt_days(finish),line,sup,pri,'Seeded factory planning record.')))
        db.execute('UPDATE production_batches SET product_name=(SELECT product_name FROM sales_orders WHERE id=?) WHERE id=?',(oid,batch_ids[-1]))
    lead_seed=[
      ('Evergreen Athleisure','Ritika Bose','ritika@evergreen.example','Referral','Negotiation',780000,'Meera Krishnan',-1,'Discussed 2 colourways and recycled packaging.'),
      ('Harbour Uniforms','Imran Aziz','imran@harbour.example','Website','Qualified',420000,'Meera Krishnan',2,'School and hospitality uniform enquiry.'),
      ('Studio 7 Collective','Neha Pillai','neha@studio7.example','Trade show','Sample Requested',560000,'Asha Nair',1,'Awaiting wash-test sample approval.'),
      ('Monsoon Basics','Vivek Iyer','vivek@monsoon.example','LinkedIn','Quotation Sent',920000,'Meera Krishnan',-2,'Follow up on 3-style quotation.'),
      ('Pinecone Juniors','Sarah Dsouza','sarah@pinecone.example','Referral','Contacted',310000,'Asha Nair',4,'Interested in organic cotton jersey.'),
      ('Motion Works','Akash Patel','akash@motionworks.example','Inbound','New Lead',1250000,'Meera Krishnan',0,'Performance wear programme; estimated Q1 launch.'),
      ('Coastline Retail','Mira Nambiar','mira@coastline.example','Trade show','Won',640000,'Asha Nair',-3,'Converted to active customer.'),
      ('Redwood Apparel','Tanya Sen','tanya@redwood.example','Email','Lost',270000,'Meera Krishnan',-10,'Lead closed after price comparison.'),
    ]
    for x in lead_seed:
        company,contact,email,source,stage,value,person,offset,notes=x
        ex('INSERT INTO leads(company,contact_name,email,source,stage,value,salesperson,followup_date,notes) VALUES(?,?,?,?,?,?,?,?,?)',(company,contact,email,source,stage,value,person,dt_days(offset),notes))
    for i in range(6):
        cid=[1,2,3,4,5,6][i]
        ex('INSERT INTO enquiries(enquiry_no,customer_id,product_category,fabric_type,garment_type,quantity,sizes,colour,gsm,target_price,delivery_date,status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
           (f'ENQ-260{i+1:02d}',cid,'Garments','Cotton jersey','T-shirt',600+i*150,'S–XL',['Navy','White','Bottle Green','Multi'][i%4], '180 GSM',190+i*12,dt_days(18+i*3),'Quoted' if i<4 else 'Open','Fixture enquiry record.'))
        ex('INSERT INTO quotations(quote_no,customer_id,enquiry_id,product_name,quantity,unit_price,discount_pct,tax_pct,valid_until,status,payment_terms,delivery_terms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
           (f'QT-260{i+1:02d}',cid,i+1,['Cotton T-shirt','Pique Polo','Organic Cotton Tee','Printed Kids Tee','School Shirt','Workwear Set'][i],600+i*150,215+i*20,2 if i%2==0 else 0,5,dt_days(12+i),'Accepted' if i<2 else ('Sent' if i<5 else 'Draft'),'30 days','FOB Chennai'))
    # Give accepted quotations their order relationships without duplicating generated order references.
    for idx,oid in enumerate(order_ids[:2],start=1):
        db.execute('UPDATE quotations SET converted_order_id=? WHERE id=?',(oid,idx))
        db.execute('UPDATE sales_orders SET quotation_id=? WHERE id=?',(idx,oid))
    # Inventory ledger opening entries
    for item in db.execute('SELECT id,item_code,on_hand FROM inventory_items').fetchall():
        ex('INSERT INTO inventory_movements(inventory_item_id,date,movement_type,qty_in,qty_out,reference,source,destination,notes) VALUES(?,?,?,?,?,?,?,?,?)',
           (item['id'],dt_days(-30),'Opening Balance',item['on_hand'],0,'OPENING','Supplier','Main Warehouse','Seeded opening balance.'))
    for i in range(5):
        bid=batch_ids[i]
        inspected=[160,100,120,80,150][i]
        rejected=[4,8,3,6,7][i]
        status='Passed' if i in (0,2,4) else 'Action Required'
        ex('INSERT INTO quality_inspections(inspection_no,batch_id,order_id,stage,sample_size,units_inspected,passed_units,rejected_units,defect_category,severity,status,notes,corrective_action,inspected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
           (f'QC-260{i+1:02d}',bid,order_ids[i],['Stitching','Cutting','Finishing','Fabric','Final'][i],inspected,inspected,inspected-rejected,rejected,['Loose thread','Shade variation','Measurement deviation','Fabric stain','Missing button'][i],['Low','High','Low','Medium','Medium'][i],status,'Random sample inspection from active batch.','Rework rejected units and re-inspect.' if status!='Passed' else 'Passed tolerance checks.',dt_days(-i)))
    for i,oid in enumerate([order_ids[5],order_ids[6],order_ids[8],order_ids[9],order_ids[2]]):
        status=['Ready to Dispatch','In Transit','Delivered','Packed','Ready to Dispatch'][i]
        shipped_qty=db.execute('SELECT quantity FROM sales_orders WHERE id=?',(oid,)).fetchone()[0]
        sid=ex('INSERT INTO shipments(shipment_no,order_id,quantity,carrier,tracking_no,carton_count,dispatch_date,expected_arrival,status,delivered_at,delivery_address) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
           (f'SHP-260{i+1:02d}',oid,shipped_qty,'LoomExpress' if i%2==0 else 'BlueDart',f'LX2600{i+1:03d}',20+i*4,dt_days(-2+i),dt_days(2+i),status,dt_days(-1) if status=='Delivered' else None,'Customer delivery address on file'))
        ex('INSERT INTO shipment_events(shipment_id,event_date,status,location,note) VALUES(?,?,?,?,?)',(sid,dt_days(-1),status,'Chennai Distribution Hub','Seeded tracking event; carrier integration is not connected.'))
    for i,oid in enumerate([order_ids[0],order_ids[1],order_ids[2],order_ids[4],order_ids[6],order_ids[8]]):
        total=db.execute('SELECT total_value FROM sales_orders WHERE id=?',(oid,)).fetchone()[0]
        paid=round(total*([.45,.0,.7,1,.25,1][i]),2)
        status='Paid' if paid>=total else ('Partially Paid' if paid>0 else 'Issued')
        ex('INSERT INTO invoices(invoice_no,order_id,customer_id,issue_date,due_date,total,paid,status) SELECT ?,id,customer_id,?,?,?, ?,? FROM sales_orders WHERE id=?',
           (f'INV-260{i+1:02d}',dt_days(-20+i),dt_days(10-i),total,paid,status,oid))
        if paid>0: ex('INSERT INTO payments(invoice_id,amount,date,method,reference) VALUES(?,?,?,?,?)',(i+1,paid,dt_days(-3),'Bank Transfer',f'PAY-DEMO-{i+1:03d}'))
    task_seed=[
      ('Confirm bottle-green dye lot','Supplier shade approval required before stitching starts.',dt_days(0),'High','Production','Production'),
      ('Follow up Monsoon Basics quote','Quotation QT-26004 has a follow-up overdue.',dt_days(-2),'High','Meera Krishnan','Sales'),
      ('Reorder greige cotton fabric','Projected demand approaches reorder threshold.',dt_days(1),'High','Nisha Raman','Inventory'),
      ('Close QC corrective action QC-26002','Document re-inspection result for shade variation.',dt_days(0),'Medium','Priya Nair','Quality'),
      ('Prepare SHP-26001 packing documents','Verify carton count and packing list.',dt_days(0),'Medium','Dispatch Team','Dispatch'),
      ('Review overdue customer balance','Confirm payment schedule for current receivables.',dt_days(3),'Normal','Kavin Raj','Finance'),
    ]
    for t in task_seed: ex('INSERT INTO tasks(title,description,due_date,priority,assigned_to,category) VALUES(?,?,?,?,?,?)',t)
    for k,v in [('company_name','LOOMOS TEXTILES'),('company_email','operations@loomos.example'),('company_phone','+91 44 4000 2600'),('currency','INR — ₹'),('timezone','Asia/Kolkata'),('theme','Forest')]:
        ex('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
    for i,oid in enumerate(order_ids[:5]):
        batchid = batch_ids[i] if i<len(batch_ids) else None
        ex('INSERT INTO product_passports(passport_code,order_id,batch_id,style_code,composition,colour,size_range,care_instructions,certification) VALUES(?,?,?,?,?,?,?,?,?)',
           (f'PASS-LOOM-{2601+i}',oid,batchid,f'STY-{400+i}','100% Cotton' if i%2==0 else '95% Cotton / 5% Elastane',['Navy','Undyed','White','Micro Floral','Charcoal'][i],'S–XL','Wash cold with similar colours; do not bleach.','Not recorded' ))
    db.commit(); db.close()

with app.app_context(): init_db()

# ---------- identity, audit and permissions ----------
@app.before_request
def load_user():
    token = csrf_token_value()
    if request.method == 'POST' and not app.testing:
        submitted = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token', '')
        if not submitted or not secrets.compare_digest(str(token), str(submitted)):
            abort(400, description='The form session expired or its security token was missing. Reload the page and try again.')
    uid = session.get('user_id')
    g.user = row('SELECT id,name,email,role,active FROM users WHERE id=? AND active=1',(uid,)) if uid else None
    portal_id = session.get('customer_user_id')
    g.customer_user = row('''SELECT p.id portal_user_id,p.email portal_email,p.customer_id,
                                    c.code customer_code,c.name customer_name,c.type customer_type,
                                    c.email customer_email,c.contact_name,c.city,c.state,c.phone
                             FROM customer_portal_users p JOIN customers c ON c.id=p.customer_id
                             WHERE p.id=? AND p.active=1''',(portal_id,)) if portal_id else None
    if portal_id and not g.customer_user:
        session.pop('customer_user_id', None)

def login_required(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        if not g.user: return redirect(url_for('login', next=request.path))
        return fn(*args,**kwargs)
    return wrapped

def customer_login_required(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        if not getattr(g, 'customer_user', None):
            return redirect(url_for('customer_login', next=request.path))
        return fn(*args,**kwargs)
    return wrapped

def module_allowed(module):
    if not g.user: return False
    allowed=ROLE_RULES.get(g.user['role'],set())
    return '*' in allowed or module in allowed

def require_module(module):
    if not g.user: return redirect(url_for('login', next=request.path))
    if not module_allowed(module): abort(403)

def audit(action, entity, entity_id=None, details=''):
    uid=g.user['id'] if getattr(g,'user',None) else None
    write('INSERT INTO audit_logs(user_id,action,entity,entity_id,details) VALUES(?,?,?,?,?)',(uid,action,entity,entity_id,str(details)[:1000]))

def csrf_token_value():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token

def csrf_field():
    return Markup('<input type="hidden" name="_csrf_token" value="{}">'.format(escape(csrf_token_value())))

app.jinja_env.globals['csrf_token'] = csrf_token_value
app.jinja_env.globals['csrf_field'] = csrf_field

@app.context_processor
def inject_globals():
    try:
        cfg={x['key']:x['value'] for x in rows('SELECT key,value FROM settings')}
    except sqlite3.OperationalError:
        cfg={}
    return {'nav_groups':NAV_GROUPS,'current_user':getattr(g,'user',None),'current_customer':getattr(g,'customer_user',None),'module_allowed':module_allowed,'order_stages':ORDER_STAGES,'crm_stages':CRM_STAGES,'production_stages':PRODUCTION_STAGES,'today_date':today(),'company_name':cfg.get('company_name','LoomOS Textiles'),'company_theme':cfg.get('theme','Forest').lower(),'app_environment':APP_ENV}

def workspace_has_admin():
    try:
        return bool(row('SELECT id FROM users LIMIT 1'))
    except sqlite3.OperationalError:
        return False

@app.route('/setup', methods=['GET', 'POST'])
def first_run_setup():
    if workspace_has_admin():
        return redirect(url_for('login'))
    if request.method == 'POST':
        company_name = request.form.get('company_name', '').strip()
        admin_name = request.form.get('admin_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if len(company_name) < 2 or len(admin_name) < 2 or '@' not in email:
            flash('Enter a company name, administrator name and valid work email.', 'error')
        elif len(password) < 12:
            flash('Choose a password with at least 12 characters.', 'error')
        elif password != confirm:
            flash('The password confirmation does not match.', 'error')
        else:
            db = get_db()
            try:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT id FROM users LIMIT 1').fetchone():
                    db.rollback()
                    return redirect(url_for('login'))
                db.execute('INSERT INTO users(name,email,password_hash,role,active) VALUES(?,?,?,?,1)',
                           (admin_name, email, generate_password_hash(password), 'Administrator'))
                values = {
                    'company_name': company_name,
                    'company_email': request.form.get('company_email', '').strip(),
                    'company_phone': request.form.get('company_phone', '').strip(),
                    'currency': request.form.get('currency', 'INR — ₹').strip(),
                    'timezone': request.form.get('timezone', 'Asia/Kolkata').strip(),
                    'theme': 'Forest',
                }
                db.executemany('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', values.items())
                db.execute('INSERT INTO audit_logs(user_id,action,entity,details) VALUES(NULL,?,?,?)',
                           ('SETUP', 'workspace', f'Workspace initialised for {company_name}; administrator {email}'))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                flash('That email is already in use. Use a different administrator email.', 'error')
                return render_template('setup.html', title='Set up your workspace')
            session.clear()
            flash('Workspace created. Sign in with your new administrator account.', 'success')
            return redirect(url_for('login'))
    return render_template('setup.html', title='Set up your workspace')

@app.route('/login', methods=['GET','POST'])
def login():
    if not workspace_has_admin(): return redirect(url_for('first_run_setup'))
    if g.user: return redirect(url_for('dashboard'))
    if getattr(g, 'customer_user', None): return redirect(url_for('customer_portal'))
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        user=row('SELECT * FROM users WHERE lower(email)=? AND active=1',(email,))
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session['user_id']=user['id']; audit('LOGIN','user',user['id'],f"Signed in as {user['role']}")
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Email or password was not recognised. Check your details and try again.','error')
    return render_template('login.html')

@app.route('/logout',methods=['POST'])
def logout():
    if g.user: audit('LOGOUT','user',g.user['id'],'Signed out')
    session.clear(); return redirect(url_for('login'))

@app.route('/account', methods=['GET', 'POST'])
@login_required
def staff_account():
    if request.method == 'POST':
        current_password=request.form.get('current_password','')
        new_password=request.form.get('new_password','')
        confirm_password=request.form.get('confirm_password','')
        account=row('SELECT * FROM users WHERE id=? AND active=1',(g.user['id'],))
        if not account or not check_password_hash(account['password_hash'],current_password):
            flash('Current password was not recognised.','error')
        elif len(new_password)<12:
            flash('Choose a new password with at least 12 characters.','error')
        elif new_password!=confirm_password:
            flash('The new password confirmation does not match.','error')
        else:
            write('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(new_password),account['id']))
            audit('PASSWORD CHANGE','user',account['id'],'Account password changed by its owner')
            flash('Your password has been changed.','success')
            return redirect(url_for('staff_account'))
    return render_template('account.html', title='Account security', active='', page_label='ACCOUNT')

@app.route('/customer/login', methods=['GET','POST'])
def customer_login():
    if not workspace_has_admin(): return redirect(url_for('first_run_setup'))
    if getattr(g, 'customer_user', None): return redirect(url_for('customer_portal'))
    if g.user: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        portal = row('SELECT * FROM customer_portal_users WHERE lower(email)=? AND active=1',(email,))
        if portal and check_password_hash(portal['password_hash'], password):
            session.clear()
            session['customer_user_id'] = portal['id']
            return redirect(request.args.get('next') or url_for('customer_portal'))
        flash('Email or password was not recognised. Contact your company account administrator if you need access.','error')
    return render_template('customer_login.html')

@app.route('/customer/logout', methods=['POST'])
@customer_login_required
def customer_logout():
    session.clear()
    return redirect(url_for('customer_login'))

@app.route('/portal/account', methods=['GET', 'POST'])
@customer_login_required
def customer_account():
    if request.method == 'POST':
        current_password=request.form.get('current_password','')
        new_password=request.form.get('new_password','')
        confirm_password=request.form.get('confirm_password','')
        portal=row('SELECT * FROM customer_portal_users WHERE id=? AND active=1',(g.customer_user['portal_user_id'],))
        if not portal or not check_password_hash(portal['password_hash'],current_password):
            flash('Current password was not recognised.','error')
        elif len(new_password)<12:
            flash('Choose a new password with at least 12 characters.','error')
        elif new_password!=confirm_password:
            flash('The new password confirmation does not match.','error')
        else:
            write('UPDATE customer_portal_users SET password_hash=? WHERE id=?',(generate_password_hash(new_password),portal['id']))
            flash('Your password has been changed.','success')
            return redirect(url_for('customer_account'))
    return render_template('customer_account.html', title='Account security')

@app.route('/portal')
@customer_login_required
def customer_portal():
    customer = g.customer_user
    orders = rows('''SELECT o.id,o.order_no,o.product_name,o.sku,o.colour,o.quantity,o.total_value,
                            o.order_date,o.promised_date,o.stage,o.priority,o.tracking_token
                     FROM sales_orders o WHERE o.customer_id=? ORDER BY o.created_at DESC,o.id DESC''',
                  (customer['customer_id'],))
    invoices = rows('''SELECT i.invoice_no,i.issue_date,i.due_date,i.total,i.paid,i.status,o.order_no
                       FROM invoices i JOIN sales_orders o ON o.id=i.order_id
                       WHERE i.customer_id=? ORDER BY i.due_date''',(customer['customer_id'],))
    requests = rows('''SELECT r.*,o.order_no FROM customer_update_requests r
                       JOIN sales_orders o ON o.id=r.order_id WHERE r.customer_id=?
                       ORDER BY r.id DESC''',(customer['customer_id'],))
    active_count = sum(1 for o in orders if o['stage'] != 'Delivered')
    outstanding = sum(max(float(i['total'] or 0)-float(i['paid'] or 0),0) for i in invoices)
    open_requests = sum(1 for r in requests if r['status'] != 'Resolved')
    return render_template('customer_portal.html', customer=customer, orders=orders, invoices=invoices,
                           requests=requests, active_count=active_count, outstanding=outstanding,
                           open_requests=open_requests, title='Customer Workspace')

@app.route('/portal/orders/<int:order_id>')
@customer_login_required
def customer_order_detail(order_id):
    customer = g.customer_user
    order = row('''SELECT o.id,o.order_no,o.product_name,o.sku,o.colour,o.size_breakdown,o.quantity,
                          o.total_value,o.order_date,o.promised_date,o.stage,o.priority,o.tracking_token,
                          c.name customer_name
                   FROM sales_orders o JOIN customers c ON c.id=o.customer_id
                   WHERE o.id=? AND o.customer_id=?''',(order_id,customer['customer_id']))
    if not order: abort(404)
    shipments = rows('SELECT shipment_no,quantity,carrier,tracking_no,carton_count,dispatch_date,expected_arrival,status,delivered_at FROM shipments WHERE order_id=? ORDER BY id DESC',(order_id,))
    shipment = shipments[0] if shipments else None
    shipped_quantity = sum(int(item['quantity'] or 0) for item in shipments)
    events = rows('''SELECT e.event_date,e.status,e.location,e.note,s.shipment_no FROM shipment_events e
                     JOIN shipments s ON s.id=e.shipment_id WHERE s.order_id=? ORDER BY e.event_date DESC,e.id DESC''',(order_id,))
    requests = rows('SELECT id,request_type,message,status,created_at FROM customer_update_requests WHERE order_id=? AND customer_id=? ORDER BY id DESC',(order_id,customer['customer_id']))
    return render_template('customer_order_detail.html', customer=customer, order=order,
                           shipment=shipment, shipments=shipments, shipped_quantity=shipped_quantity,
                           events=events, requests=requests, title=order['order_no'])

@app.route('/portal/orders/<int:order_id>/request-update', methods=['POST'])
@customer_login_required
def customer_request_update(order_id):
    customer = g.customer_user
    order = row('SELECT id,order_no FROM sales_orders WHERE id=? AND customer_id=?',(order_id,customer['customer_id']))
    if not order: abort(404)
    request_type = request.form.get('request_type','Order status').strip()
    message = request.form.get('message','').strip()
    allowed_types = {'Production status','Delivery date','Shipment details','Documents','Other'}
    if request_type not in allowed_types or not message or len(message) > 1000:
        flash('Choose a request type and enter a message of up to 1,000 characters.','error')
        return redirect(url_for('customer_order_detail', order_id=order_id))
    rid = write('INSERT INTO customer_update_requests(customer_id,order_id,request_type,message) VALUES(?,?,?,?)',
                (customer['customer_id'],order_id,request_type,message))
    flash('Your update request has been sent to the LoomOS company team.','success')
    return redirect(url_for('customer_order_detail',order_id=order_id))

@app.errorhandler(400)
def bad_request(_e): return render_template('error.html',code=400,title='Request could not be validated',message='Reload the page and submit the form again. If the issue continues, sign in again.'),400
@app.errorhandler(403)
def forbidden(_e): return render_template('error.html',code=403,title='Access restricted',message='Your current role does not have permission to open this area.'),403
@app.errorhandler(404)
def not_found(_e): return render_template('error.html',code=404,title='Page not found',message='The page may have moved or the link is no longer available.'),404

# ---------- shared calculations ----------
def dashboard_data():
    orders=rows('SELECT o.*,c.name customer_name FROM sales_orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.promised_date')
    batches=rows('SELECT * FROM production_batches ORDER BY target_date')
    inventory=rows('SELECT i.*,s.name supplier_name FROM inventory_items i LEFT JOIN suppliers s ON s.id=i.supplier_id ORDER BY i.name')
    invoices=rows('SELECT i.*,c.name customer_name,o.order_no FROM invoices i JOIN customers c ON c.id=i.customer_id JOIN sales_orders o ON o.id=i.order_id')
    today_date=date.today()
    at_risk=[]
    for o in orders:
        due=date.fromisoformat(o['promised_date']) if o['promised_date'] else today_date+timedelta(days=99)
        batch=next((b for b in batches if b['sales_order_id']==o['id']),None)
        remaining=max(0,(batch['planned_qty']-batch['completed_qty']) if batch else 0)
        days=(due-today_date).days
        score=0; reasons=[]
        if days<0: score+=45; reasons.append(f'{abs(days)} day(s) overdue')
        elif days<=3 and o['stage'] not in ('Delivered','Dispatched'): score+=25; reasons.append('delivery buffer under 3 days')
        if batch and remaining>0 and remaining/max(batch['planned_qty'],1)>.45 and days<7: score+=25; reasons.append('production still has significant quantity remaining')
        if o['stage'] in ('Materials Allocated','Cutting') and days<7: score+=20; reasons.append('early production stage close to due date')
        if o['priority']=='Critical': score+=20; reasons.append('critical priority')
        if score>=45: level='Critical'
        elif score>=25: level='High'
        elif score>0: level='Medium'
        else: level='Low'
        o['risk_score']=min(100,score); o['risk_level']=level; o['risk_reason']=', '.join(reasons) or 'Schedule currently has a workable buffer.'
        if level in ('Critical','High'): at_risk.append(o)
    active_orders=[o for o in orders if o['stage']!='Delivered']
    revenue=sum(o['total_value'] for o in orders)
    inventory_value=sum(i['on_hand']*i['avg_cost'] for i in inventory)
    open_receivables=sum(max(0,i['total']-i['paid']) for i in invoices)
    low_stock=[i for i in inventory if i['on_hand']-i['reserved']<=i['reorder_level']]
    pipeline=[]
    stages=['Order Confirmed','Materials Allocated','Cutting','Stitching','Finishing','Quality Inspection','Packing','Ready for Dispatch','Dispatched','Delivered']
    for stage in stages:
        so=[o for o in orders if o['stage']==stage]
        pipeline.append({'stage':stage,'count':len(so),'value':sum(o['total_value'] for o in so)})
    return {'orders':orders,'batches':batches,'inventory':inventory,'invoices':invoices,'at_risk':sorted(at_risk,key=lambda x:x['risk_score'],reverse=True),'active_orders':active_orders,'revenue':revenue,'inventory_value':inventory_value,'open_receivables':open_receivables,'low_stock':low_stock,'pipeline':pipeline}

@app.route('/')
@login_required
def dashboard():
    if not module_allowed('dashboard'): abort(403)
    d=dashboard_data()
    recent=rows('SELECT a.*,u.name user_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 7')
    tasks=rows("SELECT * FROM tasks WHERE status!='Done' ORDER BY CASE priority WHEN 'High' THEN 0 WHEN 'Critical' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, due_date LIMIT 6")
    recorded_months = {item['month']: float(item['value'] or 0) for item in rows(
        'SELECT substr(order_date,1,7) AS month, SUM(total_value) AS value FROM sales_orders GROUP BY substr(order_date,1,7)')}
    monthly=[]
    today_date=date.today()
    for offset in range(5,-1,-1):
        month_index=(today_date.year * 12 + today_date.month - 1) - offset
        y, m = divmod(month_index, 12)
        m += 1
        bucket=date(y,m,1)
        key=bucket.strftime('%Y-%m')
        monthly.append({'label':bucket.strftime('%b'),'value':recorded_months.get(key,0),'month':key})
    has_order_history = bool(d['orders'])
    setup_checklist = [
        {'label':'Add customer accounts','detail':'Create the companies you manufacture for.','url':'/customers#new-customer','complete':bool(row('SELECT id FROM customers LIMIT 1'))},
        {'label':'Set up material and finished stock','detail':'Register SKUs, units, opening balances and reorder thresholds.','url':'/inventory#new-item','complete':bool(row('SELECT id FROM inventory_items LIMIT 1'))},
        {'label':'Register suppliers','detail':'Maintain supplier contacts and lead times.','url':'/procurement','complete':bool(row('SELECT id FROM suppliers LIMIT 1'))},
        {'label':'Create the first sales order','detail':'Start a traceable order through production and dispatch.','url':'/orders#new-order','complete':bool(d['orders'])},
    ]
    setup_complete=sum(1 for item in setup_checklist if item['complete'])
    return render_template('dashboard.html', d=d, recent=recent, tasks=tasks, monthly=monthly,
                           has_order_history=has_order_history, setup_checklist=setup_checklist,
                           setup_complete=setup_complete, title='Command Center', active='/', page_label='OVERVIEW')

@app.route('/analytics')
@login_required
def analytics():
    require_module('analytics')
    d=dashboard_data()
    by_customer=rows('SELECT c.name,COUNT(o.id) orders,COALESCE(SUM(o.total_value),0) value FROM customers c LEFT JOIN sales_orders o ON o.customer_id=c.id GROUP BY c.id ORDER BY value DESC LIMIT 6')
    by_stage=rows('SELECT stage,COUNT(*) count,COALESCE(SUM(total_value),0) value FROM sales_orders GROUP BY stage ORDER BY count DESC')
    quality=rows('SELECT defect_category,COUNT(*) count,SUM(rejected_units) rejected FROM quality_inspections GROUP BY defect_category ORDER BY rejected DESC')
    return render_template('analytics.html',d=d,by_customer=by_customer,by_stage=by_stage,quality=quality,title='Executive Analytics',active='/analytics',page_label='OVERVIEW')

# ---------- record lists ----------
@app.route('/crm')
@login_required
def crm():
    require_module('crm')
    q=request.args.get('q','').strip(); stage=request.args.get('stage','')
    sql='SELECT * FROM leads WHERE 1=1'; params=[]
    if q: sql+=' AND (company LIKE ? OR contact_name LIKE ? OR email LIKE ?)'; params += [f'%{q}%']*3
    if stage: sql+=' AND stage=?'; params.append(stage)
    records=rows(sql+' ORDER BY CASE stage WHEN "New Lead" THEN 0 WHEN "Contacted" THEN 1 WHEN "Qualified" THEN 2 WHEN "Sample Requested" THEN 3 WHEN "Quotation Sent" THEN 4 WHEN "Negotiation" THEN 5 ELSE 6 END, followup_date',params)
    counts={s:len([x for x in rows('SELECT stage FROM leads') if x['stage']==s]) for s in CRM_STAGES}
    return render_template('crm.html',records=records,counts=counts,title='CRM Pipeline',active='/crm',page_label='CUSTOMER & SALES',q=q,stage=stage)

@app.route('/customer-requests')
@login_required
def customer_requests():
    require_module('customer-requests')
    status = request.args.get('status','')
    all_records = rows('''SELECT r.*,c.name customer_name,c.code customer_code,o.order_no,o.product_name,
                             u.name handler_name
                      FROM customer_update_requests r
                      JOIN customers c ON c.id=r.customer_id
                      JOIN sales_orders o ON o.id=r.order_id
                      LEFT JOIN users u ON u.id=r.handled_by
                      ORDER BY CASE r.status WHEN 'Open' THEN 0 WHEN 'In Progress' THEN 1 ELSE 2 END, r.id DESC''')
    counts = {s: sum(1 for r in all_records if r['status'] == s) for s in ('Open','In Progress','Resolved')}
    records = [r for r in all_records if not status or r['status'] == status] if status in {'','Open','In Progress','Resolved'} else all_records
    return render_template('customer_requests.html', records=records, counts=counts, status=status,
                           title='Customer Requests', active='/customer-requests', page_label='CUSTOMER & SALES')

@app.route('/customers')
@login_required
def customers():
    require_module('customers'); q=request.args.get('q','').strip()
    sql='SELECT c.*,COUNT(o.id) order_count,COALESCE(SUM(o.total_value),0) order_value,MAX(cpu.id) portal_user_id FROM customers c LEFT JOIN sales_orders o ON o.customer_id=c.id LEFT JOIN customer_portal_users cpu ON cpu.customer_id=c.id'
    params=[]
    if q: sql+=' WHERE c.name LIKE ? OR c.code LIKE ? OR c.city LIKE ? OR c.contact_name LIKE ?'; params=[f'%{q}%']*4
    records=rows(sql+' GROUP BY c.id ORDER BY c.name',params)
    return render_template('section.html',section='customers',records=records,title='Customers',active='/customers',page_label='CUSTOMER & SALES',q=q)

@app.route('/quotations')
@login_required
def quotations():
    require_module('quotations')
    records=rows('SELECT q.*,c.name customer_name,e.enquiry_no FROM quotations q JOIN customers c ON c.id=q.customer_id LEFT JOIN enquiries e ON e.id=q.enquiry_id ORDER BY q.id DESC')
    enquiries=rows('SELECT e.*,c.name customer_name FROM enquiries e JOIN customers c ON c.id=e.customer_id ORDER BY e.id DESC')
    customers_list=rows('SELECT id,name FROM customers ORDER BY name')
    return render_template('quotations.html',records=records,enquiries=enquiries,customers=customers_list,title='Enquiries & Quotations',active='/quotations',page_label='CUSTOMER & SALES')

@app.route('/orders')
@login_required
def orders():
    require_module('orders'); d=dashboard_data(); q=request.args.get('q','').strip(); stage=request.args.get('stage','')
    records=d['orders']
    if q: records=[o for o in records if q.lower() in (o['order_no']+' '+o['customer_name']+' '+o['product_name']).lower()]
    if stage: records=[o for o in records if o['stage']==stage]
    return render_template('section.html',section='orders',records=records,customers=rows('SELECT id,name FROM customers ORDER BY name'),title='Sales Orders',active='/orders',page_label='CUSTOMER & SALES',q=q,stage=stage,stages=ORDER_STAGES)

@app.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    require_module('orders')
    o=row('SELECT o.*,c.name customer_name,c.city,c.state,c.contact_name,c.email customer_email FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?',(order_id,))
    if not o: abort(404)
    o['risk_level']='Low'; o['risk_reason']='Schedule has a workable buffer.'
    d=dashboard_data(); match=next((x for x in d['orders'] if x['id']==order_id),None)
    if match: o['risk_level']=match['risk_level']; o['risk_reason']=match['risk_reason']; o['risk_score']=match['risk_score']
    batch=row('SELECT * FROM production_batches WHERE sales_order_id=? ORDER BY id DESC LIMIT 1',(order_id,))
    shipments=rows('SELECT * FROM shipments WHERE order_id=? ORDER BY id DESC',(order_id,))
    shipment=shipments[0] if shipments else None
    shipped_quantity=sum(int(item['quantity'] or 0) for item in shipments)
    events=rows('SELECT e.*,s.shipment_no FROM shipment_events e JOIN shipments s ON s.id=e.shipment_id WHERE s.order_id=? ORDER BY e.event_date DESC,e.id DESC',(order_id,))
    inspection=rows('SELECT * FROM quality_inspections WHERE order_id=? ORDER BY id DESC',(order_id,))
    movements=rows("SELECT m.*,i.name item_name FROM inventory_movements m JOIN inventory_items i ON i.id=m.inventory_item_id WHERE m.reference=? ORDER BY m.id DESC",(o['order_no'],))
    audit_rows=rows('SELECT a.*,u.name user_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id WHERE a.entity_id=? AND a.entity IN ("order","sales_order") ORDER BY a.id DESC LIMIT 20',(order_id,))
    return render_template('order_detail.html',o=o,batch=batch,shipment=shipment,shipments=shipments,shipped_quantity=shipped_quantity,events=events,inspections=inspection,movements=movements,audit_rows=audit_rows,title=o['order_no'],active='/orders',page_label='ORDER DETAIL')

@app.route('/production')
@login_required
def production():
    require_module('production')
    records=rows('SELECT b.*,o.order_no,o.promised_date,o.stage order_stage,c.name customer_name FROM production_batches b JOIN sales_orders o ON o.id=b.sales_order_id JOIN customers c ON c.id=o.customer_id ORDER BY b.target_date')
    order_choices=rows("SELECT id,order_no,product_name,quantity FROM sales_orders WHERE stage NOT IN ('Delivered','Dispatched') ORDER BY promised_date")
    return render_template('section.html',section='production',records=records,orders=order_choices,title='Production Planning',active='/production',page_label='OPERATIONS')

@app.route('/production-floor')
@login_required
def production_floor():
    require_module('production-floor')
    records=rows('SELECT b.*,o.order_no,o.promised_date,c.name customer_name FROM production_batches b JOIN sales_orders o ON o.id=b.sales_order_id JOIN customers c ON c.id=o.customer_id ORDER BY b.priority DESC,b.target_date')
    updates=rows('SELECT p.*,b.batch_no,u.name user_name FROM production_updates p JOIN production_batches b ON b.id=p.batch_id LEFT JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 12')
    return render_template('production_floor.html',records=records,updates=updates,title='Production Floor',active='/production-floor',page_label='OPERATIONS')

@app.route('/inventory')
@login_required
def inventory():
    require_module('inventory'); q=request.args.get('q','').strip(); category=request.args.get('category','')
    sql='SELECT i.*,s.name supplier_name FROM inventory_items i LEFT JOIN suppliers s ON s.id=i.supplier_id WHERE 1=1'; p=[]
    if q: sql+=' AND (i.item_code LIKE ? OR i.name LIKE ? OR i.batch_no LIKE ?)'; p += [f'%{q}%']*3
    if category: sql+=' AND i.category=?'; p.append(category)
    records=rows(sql+' ORDER BY i.name',p)
    categories=rows('SELECT DISTINCT category FROM inventory_items ORDER BY category')
    suppliers=rows('SELECT id,name FROM suppliers ORDER BY name')
    movements=rows('SELECT m.*,i.item_code,i.name item_name FROM inventory_movements m JOIN inventory_items i ON i.id=m.inventory_item_id ORDER BY m.id DESC LIMIT 12')
    valuation=sum(i['on_hand']*i['avg_cost'] for i in records); low=[i for i in records if i['on_hand']-i['reserved']<=i['reorder_level']]
    return render_template('inventory.html',records=records,categories=categories,suppliers=suppliers,movements=movements,valuation=valuation,low=low,title='Inventory & Warehouse',active='/inventory',page_label='OPERATIONS',q=q,category=category)

@app.route('/quality')
@login_required
def quality():
    require_module('quality')
    records=rows('SELECT qi.*,b.batch_no,o.order_no,o.product_name FROM quality_inspections qi LEFT JOIN production_batches b ON b.id=qi.batch_id LEFT JOIN sales_orders o ON o.id=qi.order_id ORDER BY qi.id DESC')
    batches=rows('SELECT b.id,b.batch_no,b.product_name,o.order_no FROM production_batches b JOIN sales_orders o ON o.id=b.sales_order_id ORDER BY b.id DESC')
    return render_template('section.html',section='quality',records=records,batches=batches,title='Quality Control',active='/quality',page_label='OPERATIONS')

@app.route('/procurement')
@login_required
def procurement():
    require_module('procurement')
    suppliers=rows('SELECT s.*,COUNT(p.id) po_count FROM suppliers s LEFT JOIN purchase_orders p ON p.supplier_id=s.id GROUP BY s.id ORDER BY s.name')
    pos=rows('SELECT p.*,s.name supplier_name,i.name item_name,i.item_code,i.unit FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id JOIN inventory_items i ON i.id=p.item_id ORDER BY p.id DESC')
    items=rows('SELECT id,item_code,name,on_hand,reorder_level,unit FROM inventory_items ORDER BY name')
    return render_template('procurement.html',suppliers=suppliers,records=pos,items=items,title='Procurement & Suppliers',active='/procurement',page_label='BUSINESS')

@app.route('/dispatch')
@login_required
def dispatch():
    require_module('dispatch')
    records=rows('SELECT s.*,o.order_no,o.product_name,o.quantity order_quantity,o.tracking_token,c.name customer_name,c.city,c.state FROM shipments s JOIN sales_orders o ON o.id=s.order_id JOIN customers c ON c.id=o.customer_id ORDER BY s.id DESC')
    candidates=rows("SELECT o.id,o.order_no,o.product_name,o.sku,o.quantity,o.stage,c.name customer_name FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE o.stage IN ('Packing','Ready for Dispatch') AND NOT EXISTS (SELECT 1 FROM quality_inspections qi WHERE qi.order_id=o.id AND qi.status='Action Required') ORDER BY o.promised_date")
    eligible=[]
    for order in candidates:
        dispatched=float(row('SELECT COALESCE(SUM(quantity),0) AS qty FROM shipments WHERE order_id=?',(order['id'],))['qty'] or 0)
        finished = row("SELECT id,on_hand,reserved FROM inventory_items WHERE category='Finished garments' AND item_code=? LIMIT 1",(order['sku'],)) if order['sku'] else None
        if not finished:
            finished=row("SELECT id,on_hand,reserved FROM inventory_items WHERE category='Finished garments' AND item_code=? LIMIT 1",(f"FG-ORD-{order['id']}",))
        order['remaining_qty']=max(0,int(order['quantity']-dispatched))
        order['available_stock']=max(0,int((finished['on_hand']-finished['reserved']) if finished else 0))
        order['can_dispatch']=order['remaining_qty']>0 and order['available_stock']>0
        eligible.append(order)
    return render_template('section.html',section='dispatch',records=records,eligible=eligible,title='Dispatch & Logistics',active='/dispatch',page_label='OPERATIONS')

@app.route('/finance')
@login_required
def finance():
    require_module('finance')
    records=rows('SELECT i.*,c.name customer_name,o.order_no FROM invoices i JOIN customers c ON c.id=i.customer_id JOIN sales_orders o ON o.id=i.order_id ORDER BY i.due_date')
    eligible=rows("SELECT o.id,o.order_no,o.customer_id,o.total_value,c.name customer_name FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE NOT EXISTS(SELECT 1 FROM invoices i WHERE i.order_id=o.id) ORDER BY o.id DESC")
    payments=rows('SELECT p.*,i.invoice_no,c.name customer_name FROM payments p JOIN invoices i ON i.id=p.invoice_id JOIN customers c ON c.id=i.customer_id ORDER BY p.id DESC LIMIT 10')
    outstanding=sum(max(0,i['total']-i['paid']) for i in records)
    return render_template('finance.html',records=records,eligible=eligible,payments=payments,outstanding=outstanding,title='Invoices & Payments',active='/finance',page_label='BUSINESS')

@app.route('/reports')
@login_required
def reports():
    require_module('reports')
    d=dashboard_data()
    report=request.args.get('report','orders')
    return render_template('reports.html',d=d,report=report,title='Reports & Exports',active='/reports',page_label='BUSINESS')

@app.route('/tasks')
@login_required
def tasks_page():
    require_module('tasks')
    records=rows('SELECT * FROM tasks ORDER BY CASE status WHEN "Open" THEN 0 WHEN "In Progress" THEN 1 ELSE 2 END, due_date')
    recent=rows('SELECT a.*,u.name user_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 30')
    return render_template('tasks.html',records=records,recent=recent,title='Notifications & Tasks',active='/tasks',page_label='SYSTEM')

@app.route('/users')
@login_required
def users_page():
    require_module('users')
    records=rows('SELECT id,name,email,role,active,created_at FROM users ORDER BY id')
    audit_rows=rows('SELECT a.*,u.name user_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 20')
    return render_template('users.html',records=records,audit_rows=audit_rows,title='User Management',active='/users',page_label='SYSTEM')

@app.route('/settings',methods=['GET','POST'])
@login_required
def settings():
    require_module('settings')
    if request.method=='POST':
        for key in ['company_name','company_email','company_phone','currency','timezone','theme']:
            val=request.form.get(key,'').strip()
            if val: write('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,val))
        audit('UPDATE','settings',None,'Updated company settings'); flash('Company settings saved.','success'); return redirect(url_for('settings'))
    vals={x['key']:x['value'] for x in rows('SELECT key,value FROM settings')}
    return render_template('settings.html',vals=vals,title='Settings',active='/settings',page_label='SYSTEM')

@app.route('/intelligence')
@login_required
def intelligence():
    require_module('intelligence'); d=dashboard_data()
    return render_template('intelligence.html',d=d,title='Textile Intelligence Copilot',active='/intelligence',page_label='INTELLIGENCE')

@app.route('/simulator')
@login_required
def simulator():
    require_module('simulator'); d=dashboard_data()
    return render_template('simulator.html',d=d,title='What-if Business Simulator',active='/simulator',page_label='INTELLIGENCE')

@app.route('/sustainability')
@login_required
def sustainability():
    require_module('sustainability')
    d=dashboard_data()
    material_usage=rows("SELECT i.category,SUM(m.qty_out) consumed FROM inventory_movements m JOIN inventory_items i ON i.id=m.inventory_item_id WHERE m.qty_out>0 GROUP BY i.category ORDER BY consumed DESC")
    entries=rows('SELECT e.*,b.batch_no,u.name user_name FROM sustainability_entries e LEFT JOIN production_batches b ON b.id=e.batch_id LEFT JOIN users u ON u.id=e.entered_by ORDER BY e.id DESC LIMIT 50')
    batches=rows('SELECT id,batch_no,product_name FROM production_batches ORDER BY id DESC')
    return render_template('sustainability.html',d=d,material_usage=material_usage,entries=entries,batches=batches,title='Sustainability Tracker',active='/sustainability',page_label='INTELLIGENCE')

@app.route('/passports')
@login_required
def passports():
    require_module('passports')
    records=rows('SELECT p.*,o.order_no,o.product_name,b.batch_no FROM product_passports p LEFT JOIN sales_orders o ON o.id=p.order_id LEFT JOIN production_batches b ON b.id=p.batch_id ORDER BY p.id DESC')
    return render_template('passports.html',records=records,title='Digital Product Passports',active='/passports',page_label='INTELLIGENCE')

@app.route('/track/<token>')
def public_tracking(token):
    o=row('SELECT o.*,c.name customer_name FROM sales_orders o JOIN customers c ON c.id=o.customer_id WHERE tracking_token=?',(token,))
    if not o: abort(404)
    shipments=rows('SELECT * FROM shipments WHERE order_id=? ORDER BY id DESC',(o['id'],))
    shipment=shipments[0] if shipments else None
    shipped_quantity=sum(int(item['quantity'] or 0) for item in shipments)
    events=rows('SELECT e.*,s.shipment_no FROM shipment_events e JOIN shipments s ON s.id=e.shipment_id WHERE s.order_id=? ORDER BY e.event_date DESC,e.id DESC',(o['id'],))
    return render_template('public_tracking.html',o=o,shipment=shipment,shipments=shipments,shipped_quantity=shipped_quantity,events=events)

@app.route('/passport/<code>')
def public_passport(code):
    p=row('SELECT p.*,o.order_no,o.product_name,b.batch_no FROM product_passports p LEFT JOIN sales_orders o ON o.id=p.order_id LEFT JOIN production_batches b ON b.id=p.batch_id WHERE p.passport_code=?',(code,))
    if not p: abort(404)
    inspections=rows('SELECT stage,units_inspected,passed_units,rejected_units,status,inspected_at FROM quality_inspections WHERE order_id=? ORDER BY id DESC',(p['order_id'],)) if p.get('order_id') else []
    shipment=row('SELECT status,carrier,tracking_no,expected_arrival FROM shipments WHERE order_id=? ORDER BY id DESC LIMIT 1',(p['order_id'],)) if p.get('order_id') else None
    return render_template('public_passport.html',p=p,inspections=inspections,shipment=shipment)

@app.route('/passport/<code>/qr.png')
@login_required
def passport_qr(code):
    require_module('passports')
    p=row('SELECT passport_code FROM product_passports WHERE passport_code=?',(code,))
    if not p: abort(404)
    target=url_for('public_passport',code=code,_external=True)
    qr=qrcode.QRCode(box_size=7,border=2); qr.add_data(target); qr.make(fit=True)
    image=qr.make_image(fill_color='#173d2d',back_color='white')
    buf=BytesIO(); image.save(buf,format='PNG'); buf.seek(0)
    return send_file(buf,mimetype='image/png',download_name=f'{code}-qr.png')

@app.route('/actions/sustainability',methods=['POST'])
@login_required
def action_sustainability():
    denied=role_action(['Production Manager','Inventory Manager','Quality Inspector']);
    if denied: return denied
    resource=request.form.get('resource_type','')
    allowed={'Fabric consumed','Cutting waste','Reusable offcuts','Rejected garments','Water usage','Energy usage'}
    if resource not in allowed: abort(400)
    quantity=form_num('quantity',0); reusable=form_num('reusable_qty',0)
    if quantity<=0 or reusable<0 or reusable>quantity:
        flash('Enter a positive quantity. Reusable quantity cannot exceed total quantity.','error'); return redirect(url_for('sustainability'))
    unit=request.form.get('unit','kg')
    allowed_units={'m','kg','pcs','litres','kWh'}
    if unit not in allowed_units: abort(400)
    bid=form_num('batch_id',None,int) if request.form.get('batch_id') else None
    eid=write('INSERT INTO sustainability_entries(entry_date,resource_type,quantity,unit,category,batch_id,reusable_qty,notes,entered_by) VALUES(?,?,?,?,?,?,?,?,?)',(request.form.get('entry_date') or today(),resource,quantity,unit,request.form.get('category',''),bid,reusable,request.form.get('notes',''),g.user['id']))
    audit('CREATE','sustainability_entry',eid,f'{resource}: {quantity:g} {unit}; reusable {reusable:g} {unit}')
    flash('Sustainability entry recorded as manually entered operational data.','success'); return redirect(url_for('sustainability'))

@app.route('/favicon.ico')
def favicon():
    return send_file(BASE_DIR / 'static' / 'favicon.svg', mimetype='image/svg+xml')

# ---------- workflow actions ----------
def form_num(name, default=0, typ=float):
    try: return typ(request.form.get(name,default) or default)
    except (ValueError,TypeError): return default

def role_action(roles):
    if not g.user: return redirect(url_for('login', next=request.path))
    if g.user['role']!='Administrator' and g.user['role'] not in roles: abort(403)
    return None

@app.route('/actions/customer',methods=['POST'])
@login_required
def action_customer():
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    name=request.form.get('name','').strip()
    if not name: flash('Customer name is required.','error'); return redirect(url_for('customers'))
    code='CUS-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
    email=request.form.get('email','').strip().lower()
    portal_password=request.form.get('portal_password','')
    if portal_password and not email:
        flash('A contact email is required before enabling customer portal access.','error'); return redirect(url_for('customers'))
    if portal_password and len(portal_password)<12:
        flash('Customer portal passwords must contain at least 12 characters.','error'); return redirect(url_for('customers'))
    if portal_password and row('SELECT id FROM customer_portal_users WHERE lower(email)=?',(email,)):
        flash('That email already has customer portal access. Use a unique email for this account.','error'); return redirect(url_for('customers'))
    try:
        cid=write('INSERT INTO customers(code,name,type,segment,city,state,contact_name,email,phone,credit_limit) VALUES(?,?,?,?,?,?,?,?,?,?)',(code,name,request.form.get('type','Brand'),request.form.get('segment','Standard'),request.form.get('city',''),request.form.get('state',''),request.form.get('contact_name',''),email,request.form.get('phone',''),form_num('credit_limit',0)))
        if portal_password:
            write('INSERT INTO customer_portal_users(customer_id,email,password_hash) VALUES(?,?,?)',(cid,email,generate_password_hash(portal_password)))
    except sqlite3.IntegrityError:
        flash('A customer or portal account with these details already exists. Check the email and retry.','error'); return redirect(url_for('customers'))
    audit('CREATE','customer',cid,f'Created {code} — {name}')
    if portal_password:
        flash(f'Customer {name} created and portal access enabled. Share the initial password securely; the customer can change it in Account settings.','success')
    else:
        flash(f'Customer {name} created. Portal access was not enabled. Add a portal password when creating an account for this customer.','success')
    return redirect(url_for('customers'))

@app.route('/actions/customer-request/<int:request_id>/status', methods=['POST'])
@login_required
def action_customer_request_status(request_id):
    denied = role_action(['Sales Manager','Sales Executive'])
    if denied: return denied
    status = request.form.get('status','Open')
    if status not in {'Open','In Progress','Resolved'}: abort(400)
    item = row('SELECT id,request_type FROM customer_update_requests WHERE id=?',(request_id,))
    if not item: abort(404)
    write('UPDATE customer_update_requests SET status=?,resolved_at=?,handled_by=? WHERE id=?',
          (status, datetime.now().isoformat(timespec='seconds') if status=='Resolved' else None, g.user['id'], request_id))
    audit('UPDATE','customer_request',request_id,f"{item['request_type']} request set to {status}")
    flash('Customer request updated.','success')
    return redirect(url_for('customer_requests'))

@app.route('/actions/lead',methods=['POST'])
@login_required
def action_lead():
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    company=request.form.get('company','').strip()
    if not company: flash('Company name is required.','error'); return redirect(url_for('crm'))
    lid=write('INSERT INTO leads(company,contact_name,email,source,stage,value,salesperson,followup_date,notes) VALUES(?,?,?,?,?,?,?,?,?)',(company,request.form.get('contact_name',''),request.form.get('email',''),request.form.get('source','Website'),request.form.get('stage','New Lead'),form_num('value',0),request.form.get('salesperson',g.user['name']),(request.form.get('followup_date') or dt_days(2)),request.form.get('notes','')))
    audit('CREATE','lead',lid,f'Created lead {company}'); flash('Lead added to CRM pipeline.','success'); return redirect(url_for('crm'))

@app.route('/actions/lead/<int:lead_id>/stage',methods=['POST'])
@login_required
def action_lead_stage(lead_id):
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    stage=request.form.get('stage','New Lead')
    if stage not in CRM_STAGES: abort(400)
    write('UPDATE leads SET stage=? WHERE id=?',(stage,lead_id)); audit('UPDATE','lead',lead_id,f'Stage changed to {stage}'); flash('Lead stage updated.','success'); return redirect(url_for('crm'))

@app.route('/actions/enquiry',methods=['POST'])
@login_required
def action_enquiry():
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    cid=form_num('customer_id',0,int); customer=row('SELECT id FROM customers WHERE id=?',(cid,))
    if not customer: flash('Select a valid customer.','error'); return redirect(url_for('quotations'))
    no='ENQ-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
    eid=write('INSERT INTO enquiries(enquiry_no,customer_id,product_category,fabric_type,garment_type,quantity,sizes,colour,gsm,target_price,delivery_date,status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(no,cid,request.form.get('product_category','Garments'),request.form.get('fabric_type','Cotton'),request.form.get('garment_type','T-shirt'),form_num('quantity',1,int),request.form.get('sizes','S–XL'),request.form.get('colour',''),request.form.get('gsm','180 GSM'),form_num('target_price',0),(request.form.get('delivery_date') or dt_days(14)),'Open',request.form.get('notes','')))
    audit('CREATE','enquiry',eid,f'Created {no}'); flash(f'Enquiry {no} created.','success'); return redirect(url_for('quotations'))

@app.route('/actions/quotation',methods=['POST'])
@login_required
def action_quotation():
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    cid=form_num('customer_id',0,int); product=request.form.get('product_name','').strip(); qty=form_num('quantity',0,int); price=form_num('unit_price',0)
    if not row('SELECT id FROM customers WHERE id=?',(cid,)) or not product or qty<1 or price<0: flash('Check customer, product, quantity and unit price.','error'); return redirect(url_for('quotations'))
    no='QT-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
    qid=write('INSERT INTO quotations(quote_no,customer_id,enquiry_id,product_name,quantity,unit_price,discount_pct,tax_pct,valid_until,status,payment_terms,delivery_terms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(no,cid,form_num('enquiry_id',None,int) if request.form.get('enquiry_id') else None,product,qty,price,form_num('discount_pct',0),form_num('tax_pct',5),(request.form.get('valid_until') or dt_days(14)),'Draft',request.form.get('payment_terms','30 days'),request.form.get('delivery_terms','FOB Chennai')))
    audit('CREATE','quotation',qid,f'Created {no}'); flash(f'Quotation {no} created as a draft.','success'); return redirect(url_for('quotations'))

@app.route('/actions/quotation/<int:qid>/status',methods=['POST'])
@login_required
def action_quotation_status(qid):
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    q=row('SELECT * FROM quotations WHERE id=?',(qid,))
    if not q: abort(404)
    status=request.form.get('status','Draft')
    if status not in ['Draft','Sent','Accepted','Rejected','Expired']: abort(400)
    write('UPDATE quotations SET status=? WHERE id=?',(status,qid)); audit('UPDATE','quotation',qid,f'{q["quote_no"]} status changed to {status}')
    flash(f'{q["quote_no"]} updated to {status}.','success'); return redirect(url_for('quotations'))

@app.route('/actions/quotation/<int:qid>/convert',methods=['POST'])
@login_required
def action_convert_quotation(qid):
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    db=get_db(); q=row('SELECT * FROM quotations WHERE id=?',(qid,))
    if not q: abort(404)
    if q['status']!='Accepted': flash('Only an accepted quotation can be converted.','error'); return redirect(url_for('quotations'))
    if q['converted_order_id']:
        flash('This quotation has already been converted; duplicate order creation was prevented.','warning'); return redirect(url_for('orders'))
    conn=get_db(); conn.execute('BEGIN IMMEDIATE')
    try:
        code='ORD-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
        gross=q['quantity']*q['unit_price']*(1-q['discount_pct']/100)*(1+q['tax_pct']/100)
        token=secrets.token_urlsafe(24)
        cur=conn.execute('INSERT INTO sales_orders(order_no,customer_id,quotation_id,product_name,sku,colour,size_breakdown,quantity,unit_price,tax_pct,total_value,order_date,promised_date,stage,priority,notes,tracking_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (code,q['customer_id'],qid,q['product_name'],'', '', 'S–XL',q['quantity'],q['unit_price'],q['tax_pct'],round(gross,2),today(),dt_days(21),'Order Confirmed','Normal',f'Converted from quotation {q["quote_no"]}',token))
        oid=cur.lastrowid; conn.execute('UPDATE quotations SET converted_order_id=? WHERE id=?',(oid,qid)); conn.execute('UPDATE sales_orders SET quotation_id=? WHERE id=?',(qid,oid)); conn.commit()
    except Exception:
        conn.rollback(); raise
    audit('CONVERT','quotation',qid,f'Converted {q["quote_no"]} into {code}'); audit('CREATE','sales_order',oid,f'Created from quotation {q["quote_no"]}')
    flash(f'{q["quote_no"]} converted into sales order {code}.','success'); return redirect(url_for('orders'))

@app.route('/actions/order',methods=['POST'])
@login_required
def action_order():
    denied=role_action(['Sales Manager','Sales Executive']);
    if denied: return denied
    cid=form_num('customer_id',0,int); product=request.form.get('product_name','').strip(); qty=form_num('quantity',0,int); price=form_num('unit_price',0); tax=form_num('tax_pct',5)
    if not row('SELECT id FROM customers WHERE id=?',(cid,)) or not product or qty<1 or price<0: flash('Check customer, product, quantity and price.','error'); return redirect(url_for('orders'))
    no='ORD-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); total=round(qty*price*(1+tax/100),2)
    oid=write('INSERT INTO sales_orders(order_no,customer_id,product_name,sku,colour,size_breakdown,quantity,unit_price,tax_pct,total_value,order_date,promised_date,stage,priority,notes,tracking_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(no,cid,product,request.form.get('sku',''),request.form.get('colour',''),request.form.get('size_breakdown','S–XL'),qty,price,tax,total,today(),(request.form.get('promised_date') or dt_days(21)),'Order Confirmed',request.form.get('priority','Normal'),request.form.get('notes',''),secrets.token_urlsafe(24)))
    audit('CREATE','sales_order',oid,f'Created order {no}'); flash(f'Sales order {no} created.','success'); return redirect(url_for('order_detail',order_id=oid))

@app.route('/actions/order/<int:oid>/stage',methods=['POST'])
@login_required
def action_order_stage(oid):
    if g.user['role'] not in ['Administrator','Sales Manager','Production Manager','Dispatch Manager']: abort(403)
    stage=request.form.get('stage','')
    if stage not in ORDER_STAGES: abort(400)
    o=row('SELECT * FROM sales_orders WHERE id=?',(oid,))
    if not o: abort(404)
    write('UPDATE sales_orders SET stage=? WHERE id=?',(stage,oid)); audit('UPDATE','sales_order',oid,f'{o["order_no"]} stage changed {o["stage"]} → {stage}'); flash(f'{o["order_no"]} progress updated.','success')
    return redirect(url_for('order_detail',order_id=oid))

@app.route('/actions/inventory/item',methods=['POST'])
@login_required
def action_inventory_item():
    denied=role_action(['Inventory Manager']);
    if denied: return denied
    code=request.form.get('item_code','').strip().upper(); name=request.form.get('name','').strip()
    if not code or not name: flash('Item code and name are required.','error'); return redirect(url_for('inventory'))
    try:
        iid=write('INSERT INTO inventory_items(item_code,name,category,supplier_id,unit,on_hand,reserved,reorder_level,safety_stock,avg_cost,warehouse,location,batch_no,colour,gsm,last_movement) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,name,request.form.get('category','Accessories'),form_num('supplier_id',None,int) if request.form.get('supplier_id') else None,request.form.get('unit','pcs'),form_num('on_hand',0),0,form_num('reorder_level',0),form_num('safety_stock',0),form_num('avg_cost',0),request.form.get('warehouse','Main Warehouse'),request.form.get('location',''),request.form.get('batch_no',''),request.form.get('colour',''),request.form.get('gsm',''),today()))
    except sqlite3.IntegrityError: flash('That item code already exists. Use a unique code.','error'); return redirect(url_for('inventory'))
    if form_num('on_hand',0)>0: write('INSERT INTO inventory_movements(inventory_item_id,date,movement_type,qty_in,reference,source,destination,user_id,notes) VALUES(?,?,?,?,?,?,?,?,?)',(iid,today(),'Opening Balance',form_num('on_hand',0),'NEW ITEM','Supplier','Main Warehouse',g.user['id'],'Opening quantity entered during item creation.'))
    audit('CREATE','inventory_item',iid,f'Created {code} — {name}'); flash(f'Inventory item {code} created.','success'); return redirect(url_for('inventory'))

@app.route('/actions/inventory/<int:iid>/movement',methods=['POST'])
@login_required
def action_inventory_movement(iid):
    denied=role_action(['Inventory Manager','Production Manager']);
    if denied: return denied
    item=row('SELECT * FROM inventory_items WHERE id=?',(iid,))
    if not item: abort(404)
    kind=request.form.get('movement_type','Receipt'); qty=form_num('quantity',0)
    if qty<=0: flash('Movement quantity must be greater than zero.','error'); return redirect(url_for('inventory'))
    if kind not in ['Receipt','Issue','Return','Adjustment +','Adjustment -','Transfer']: abort(400)
    qty_in=qty if kind in ['Receipt','Return','Adjustment +'] else 0
    qty_out=qty if kind in ['Issue','Adjustment -'] else 0
    if kind=='Transfer': qty_in=qty; qty_out=qty
    if qty_out and item['on_hand']-item['reserved']<qty: flash('Insufficient available stock; negative stock is blocked.','error'); return redirect(url_for('inventory'))
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE inventory_items SET on_hand=on_hand+?-?,last_movement=? WHERE id=?',(qty_in,qty_out,today(),iid))
        cur=db.execute('INSERT INTO inventory_movements(inventory_item_id,date,movement_type,qty_in,qty_out,reference,source,destination,user_id,notes) VALUES(?,?,?,?,?,?,?,?,?,?)',(iid,today(),kind,qty_in,qty_out,request.form.get('reference','MANUAL'),request.form.get('source',''),request.form.get('destination',''),g.user['id'],request.form.get('notes','')))
        db.commit(); mid=cur.lastrowid
    except Exception: db.rollback(); raise
    audit('STOCK MOVEMENT','inventory_item',iid,f'{kind}: {qty:g} {item["unit"]}; movement #{mid}')
    flash(f'{kind} of {qty:g} {item["unit"]} recorded with ledger reference #{mid}.','success'); return redirect(url_for('inventory'))

@app.route('/actions/production',methods=['POST'])
@login_required
def action_production():
    denied=role_action(['Production Manager']);
    if denied: return denied
    oid=form_num('sales_order_id',0,int); o=row('SELECT * FROM sales_orders WHERE id=?',(oid,)); planned=form_num('planned_qty',0,int)
    if not o or planned<1: flash('Select an order and enter a positive planned quantity.','error'); return redirect(url_for('production'))
    code='BTH-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
    bid=write('INSERT INTO production_batches(batch_no,sales_order_id,product_name,planned_qty,completed_qty,rejected_qty,stage,start_date,target_date,line_name,supervisor,priority,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,oid,o['product_name'],planned,0,0,'Material Preparation',today(),(request.form.get('target_date') or o['promised_date']),request.form.get('line_name','Line A'),request.form.get('supervisor',g.user['name']),request.form.get('priority','Normal'),request.form.get('notes','')))
    audit('CREATE','production_batch',bid,f'Created batch {code} for {o["order_no"]}'); flash(f'Production batch {code} created.','success'); return redirect(url_for('production'))

@app.route('/actions/production/<int:bid>/update',methods=['POST'])
@login_required
def action_production_update(bid):
    denied=role_action(['Production Manager','Production Operator']);
    if denied: return denied
    b=row('SELECT * FROM production_batches WHERE id=?',(bid,));
    if not b: abort(404)
    completed=form_num('completed_delta',0,int); rejected=form_num('rejected_delta',0,int); stage=request.form.get('stage',b['stage'])
    if stage not in PRODUCTION_STAGES or completed<0 or rejected<0 or completed+rejected<1: flash('Enter a valid stage and a positive completed/rejected quantity.','error'); return redirect(url_for('production_floor'))
    if b['completed_qty']+b['rejected_qty']+completed+rejected>b['planned_qty'] and g.user['role'] not in ['Administrator','Production Manager']:
        flash('Reported quantity exceeds plan; an authorised manager must adjust the batch.','error'); return redirect(url_for('production_floor'))
    if b['completed_qty']+b['rejected_qty']+completed+rejected>b['planned_qty']:
        flash('Reported quantity exceeds the plan. Reduce the quantities or use an authorised plan revision process.','error'); return redirect(url_for('production_floor'))
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE production_batches SET completed_qty=completed_qty+?,rejected_qty=rejected_qty+?,stage=?,downtime_reason=? WHERE id=?',(completed,rejected,stage,request.form.get('downtime_reason',''),bid))
        db.execute('INSERT INTO production_updates(batch_id,date,stage,completed_delta,rejected_delta,downtime_minutes,defect_reason,notes,user_id) VALUES(?,?,?,?,?,?,?,?,?)',(bid,today(),stage,completed,rejected,form_num('downtime_minutes',0,int),request.form.get('defect_reason',''),request.form.get('notes',''),g.user['id']))
        bnew=db.execute('SELECT * FROM production_batches WHERE id=?',(bid,)).fetchone()
        order_stage={'Material Preparation':'Materials Allocated','Cutting':'Cutting','Stitching':'Stitching','Finishing':'Finishing','Quality Control':'Quality Inspection','Packing':'Packing','Completed':'Ready for Dispatch'}[stage]
        db.execute('UPDATE sales_orders SET stage=? WHERE id=? AND stage NOT IN ("Delivered","Dispatched")',(order_stage,b['sales_order_id']))
        # Post completed good units to finished-goods inventory once, using the auditable stock ledger.
        if stage == 'Completed' and bnew['completed_qty'] > bnew['finished_qty_posted']:
            order = db.execute('SELECT * FROM sales_orders WHERE id=?',(b['sales_order_id'],)).fetchone()
            finished = None
            if order['sku']:
                finished = db.execute("SELECT * FROM inventory_items WHERE item_code=? AND category='Finished garments' LIMIT 1",(order['sku'].strip(),)).fetchone()
            if not finished:
                finished_code=f'FG-ORD-{order["id"]}'
                finished = db.execute('SELECT * FROM inventory_items WHERE item_code=?',(finished_code,)).fetchone()
                if finished and finished['category'] != 'Finished garments':
                    finished_code=f'FG-ORD-{order["id"]}-{bid}'
                    finished = db.execute('SELECT * FROM inventory_items WHERE item_code=?',(finished_code,)).fetchone()
                if not finished:
                    cur_item=db.execute("INSERT INTO inventory_items(item_code,name,category,unit,on_hand,reserved,reorder_level,safety_stock,avg_cost,warehouse,location,batch_no,colour,last_movement) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (finished_code,order['product_name'] + (f" — {order['colour']}" if order['colour'] else ''),'Finished garments','pcs',0,0,0,0,0,'Finished Goods','',bnew['batch_no'],order['colour'],today()))
                    finished=db.execute('SELECT * FROM inventory_items WHERE id=?',(cur_item.lastrowid,)).fetchone()
            post_qty=int(bnew['completed_qty']-bnew['finished_qty_posted'])
            db.execute('UPDATE inventory_items SET on_hand=on_hand+?,last_movement=?,batch_no=? WHERE id=?',(post_qty,today(),bnew['batch_no'],finished['id']))
            db.execute('INSERT INTO inventory_movements(inventory_item_id,date,movement_type,qty_in,qty_out,reference,source,destination,user_id,notes) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (finished['id'],today(),'Production Output',post_qty,0,bnew['batch_no'],'Production Floor','Finished Goods',g.user['id'],'Completed good units posted from production batch.'))
            db.execute('UPDATE production_batches SET finished_qty_posted=completed_qty WHERE id=?',(bid,))
        db.commit()
    except Exception: db.rollback(); raise
    audit('UPDATE','production_batch',bid,f'{b["batch_no"]}: +{completed} good, +{rejected} rejected; stage {stage}')
    flash(f'Production progress saved for {b["batch_no"]}.','success'); return redirect(url_for('production_floor'))

@app.route('/actions/quality',methods=['POST'])
@login_required
def action_quality():
    denied=role_action(['Quality Inspector','Production Manager']);
    if denied: return denied
    bid=form_num('batch_id',None,int) if request.form.get('batch_id') else None
    b=row('SELECT * FROM production_batches WHERE id=?',(bid,)) if bid else None
    oid=b['sales_order_id'] if b else form_num('order_id',None,int) if request.form.get('order_id') else None
    inspected=form_num('units_inspected',0,int); rejected=form_num('rejected_units',0,int)
    if inspected<1 or rejected<0 or rejected>inspected: flash('Inspection quantities are invalid.','error'); return redirect(url_for('quality'))
    no='QC-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); passed=inspected-rejected; status='Passed' if rejected==0 else 'Action Required'
    iid=write('INSERT INTO quality_inspections(inspection_no,batch_id,order_id,stage,sample_size,units_inspected,passed_units,rejected_units,defect_category,severity,status,notes,corrective_action,inspected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(no,bid,oid,request.form.get('stage','Final Inspection'),form_num('sample_size',inspected,int),inspected,passed,rejected,request.form.get('defect_category','None recorded'),request.form.get('severity','Low'),status,request.form.get('notes',''),request.form.get('corrective_action',''),today()))
    audit('CREATE','quality_inspection',iid,f'{no}: {status}, rejects {rejected}/{inspected}')
    if rejected: write('INSERT INTO tasks(title,description,due_date,priority,status,assigned_to,category) VALUES(?,?,?,?,?,?,?)',(f'Resolve quality exception {no}',f'{rejected} unit(s) rejected during {request.form.get("stage","inspection")}. Document corrective action before progression.',dt_days(1),'High','Open',g.user['name'],'Quality'))
    flash(f'Inspection {no} recorded: {status}.','success'); return redirect(url_for('quality'))

@app.route('/actions/quality/<int:iid>/resolve', methods=['POST'])
@login_required
def action_quality_resolve(iid):
    denied=role_action(['Quality Inspector','Production Manager']);
    if denied: return denied
    inspection=row('SELECT * FROM quality_inspections WHERE id=?',(iid,))
    if not inspection: abort(404)
    if inspection['status']!='Action Required':
        flash('Only inspections requiring action can be resolved.','warning'); return redirect(url_for('quality'))
    resolution=request.form.get('resolution','').strip()
    if not resolution or len(resolution)>1000:
        flash('Enter a documented corrective action of up to 1,000 characters.','error'); return redirect(url_for('quality'))
    note=(inspection.get('corrective_action') or '').strip()
    combined=(note + ('\n' if note else '') + 'Resolution: ' + resolution).strip()[:2000]
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute("UPDATE quality_inspections SET status='Resolved',corrective_action=? WHERE id=?",(combined,iid))
        db.execute("UPDATE tasks SET status='Done' WHERE title=? AND status!='Done'",(f'Resolve quality exception {inspection["inspection_no"]}',))
        db.commit()
    except Exception:
        db.rollback(); raise
    audit('RESOLVE','quality_inspection',iid,f'{inspection["inspection_no"]}: {resolution}')
    flash(f'Quality exception {inspection["inspection_no"]} resolved and documented.','success'); return redirect(url_for('quality'))

@app.route('/actions/supplier',methods=['POST'])
@login_required
def action_supplier():
    denied=role_action(['Procurement Manager']);
    if denied: return denied
    name=request.form.get('name','').strip();
    if not name: flash('Supplier name is required.','error'); return redirect(url_for('procurement'))
    code='SUP-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
    sid=write('INSERT INTO suppliers(code,name,category,city,lead_time_days,rating,email,phone) VALUES(?,?,?,?,?,?,?,?)',(code,name,request.form.get('category','Fabric'),request.form.get('city',''),form_num('lead_time_days',7,int),form_num('rating',4),request.form.get('email',''),request.form.get('phone','')))
    audit('CREATE','supplier',sid,f'Created supplier {name}'); flash(f'Supplier {name} added.','success'); return redirect(url_for('procurement'))

@app.route('/actions/po',methods=['POST'])
@login_required
def action_po():
    denied=role_action(['Procurement Manager','Inventory Manager']);
    if denied: return denied
    supplier_id=form_num('supplier_id',0,int); item_id=form_num('item_id',0,int); qty=form_num('quantity',0); cost=form_num('unit_cost',0)
    if not row('SELECT id FROM suppliers WHERE id=?',(supplier_id,)) or not row('SELECT id FROM inventory_items WHERE id=?',(item_id,)) or qty<=0 or cost<0: flash('Supplier, item, quantity and cost must be valid.','error'); return redirect(url_for('procurement'))
    no='PO-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); pid=write('INSERT INTO purchase_orders(po_no,supplier_id,item_id,quantity,received_qty,unit_cost,status,expected_date) VALUES(?,?,?,?,?,?,?,?)',(no,supplier_id,item_id,qty,0,cost,'Pending Approval',request.form.get('expected_date',dt_days(7))))
    audit('CREATE','purchase_order',pid,f'Created {no}'); flash(f'Purchase order {no} created and awaits approval.','success'); return redirect(url_for('procurement'))

@app.route('/actions/po/<int:pid>/approve',methods=['POST'])
@login_required
def action_po_approve(pid):
    denied=role_action(['Procurement Manager']);
    if denied: return denied
    p=row('SELECT * FROM purchase_orders WHERE id=?',(pid,));
    if not p: abort(404)
    if p['status']!='Pending Approval': flash('Only pending purchase orders can be approved.','error')
    else:
        write('UPDATE purchase_orders SET status="Approved" WHERE id=?',(pid,)); audit('APPROVE','purchase_order',pid,f'Approved {p["po_no"]}'); flash(f'{p["po_no"]} approved.','success')
    return redirect(url_for('procurement'))

@app.route('/actions/po/<int:pid>/receive',methods=['POST'])
@login_required
def action_po_receive(pid):
    denied=role_action(['Procurement Manager','Inventory Manager']);
    if denied: return denied
    p=row('SELECT p.*,i.name item_name,i.unit FROM purchase_orders p JOIN inventory_items i ON i.id=p.item_id WHERE p.id=?',(pid,));
    if not p: abort(404)
    qty=form_num('quantity',0)
    if p['status'] not in ['Approved','Sent','Partially Received'] or qty<=0 or p['received_qty']+qty>p['quantity']:
        flash('Receipt must be positive and cannot exceed the approved outstanding quantity. Approve the PO first.','error'); return redirect(url_for('procurement'))
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE inventory_items SET on_hand=on_hand+?,avg_cost=?,last_movement=? WHERE id=?',(qty,p['unit_cost'],today(),p['item_id']))
        new_received=p['received_qty']+qty; status='Received' if new_received>=p['quantity'] else 'Partially Received'
        db.execute('UPDATE purchase_orders SET received_qty=?,status=? WHERE id=?',(new_received,status,pid))
        db.execute('INSERT INTO inventory_movements(inventory_item_id,date,movement_type,qty_in,reference,source,destination,user_id,notes) VALUES(?,?,?,?,?,?,?,?,?)',(p['item_id'],today(),'Goods Receipt',qty,p['po_no'],p['supplier_id'],'Main Warehouse',g.user['id'],'Received against approved purchase order.'))
        db.commit()
    except Exception: db.rollback(); raise
    audit('GOODS RECEIPT','purchase_order',pid,f'{qty:g} {p["unit"]} received for {p["po_no"]}'); flash(f'{qty:g} {p["unit"]} added to stock through a ledger entry.','success'); return redirect(url_for('procurement'))

@app.route('/actions/shipment',methods=['POST'])
@login_required
def action_shipment():
    denied=role_action(['Dispatch Manager']);
    if denied: return denied
    oid=form_num('order_id',0,int); qty=form_num('quantity',0,int)
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        o=db.execute('SELECT * FROM sales_orders WHERE id=?',(oid,)).fetchone()
        if not o or o['stage'] not in ['Packing','Ready for Dispatch']:
            db.rollback(); flash('Only orders in Packing or Ready for Dispatch can be dispatched.','error'); return redirect(url_for('dispatch'))
        unresolved=db.execute("SELECT id FROM quality_inspections WHERE order_id=? AND status='Action Required' LIMIT 1",(oid,)).fetchone()
        if unresolved:
            db.rollback(); flash('Resolve outstanding quality actions before dispatching this order.','error'); return redirect(url_for('dispatch'))
        already=db.execute('SELECT COALESCE(SUM(quantity),0) AS qty FROM shipments WHERE order_id=?',(oid,)).fetchone()['qty'] or 0
        remaining=max(0,int(o['quantity']-already))
        if qty<1 or qty>remaining:
            db.rollback(); flash(f'Enter a dispatch quantity from 1 to {remaining} pieces for this order.','error'); return redirect(url_for('dispatch'))
        finished=None
        if o['sku']:
            finished=db.execute("SELECT * FROM inventory_items WHERE category='Finished garments' AND item_code=? LIMIT 1",(o['sku'].strip(),)).fetchone()
        if not finished:
            finished=db.execute("SELECT * FROM inventory_items WHERE category='Finished garments' AND item_code=? LIMIT 1",(f'FG-ORD-{oid}',)).fetchone()
        if not finished:
            db.rollback(); flash('No finished-garment stock is recorded for this order. Complete the linked production batch or record the finished stock item before dispatch.','error'); return redirect(url_for('dispatch'))
        available=max(0,int(finished['on_hand']-finished['reserved']))
        if qty>available:
            db.rollback(); flash(f'Only {available} pieces are available for dispatch. Record production output or replenish finished stock first.','error'); return redirect(url_for('dispatch'))
        no='SHP-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
        tracking=request.form.get('tracking_no','').strip()
        cur=db.execute('INSERT INTO shipments(shipment_no,order_id,quantity,carrier,tracking_no,carton_count,dispatch_date,expected_arrival,status,delivery_address) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (no,oid,qty,request.form.get('carrier','').strip() or 'Not assigned',tracking,form_num('carton_count',1,int),today(),(request.form.get('expected_arrival') or dt_days(3)),'Dispatched',request.form.get('delivery_address','').strip()))
        sid=cur.lastrowid
        db.execute('UPDATE inventory_items SET on_hand=on_hand-?,last_movement=? WHERE id=?',(qty,today(),finished['id']))
        db.execute('INSERT INTO inventory_movements(inventory_item_id,date,movement_type,qty_in,qty_out,reference,source,destination,user_id,notes) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (finished['id'],today(),'Dispatch',0,qty,no,'Finished Goods','Customer Shipment',g.user['id'],f'Dispatched against order {o["order_no"]}.'))
        db.execute('INSERT INTO shipment_events(shipment_id,event_date,status,location,note) VALUES(?,?,?,?,?)',(sid,today(),'Dispatched','Factory dispatch bay','Shipment created by the dispatch team.'))
        fully_dispatched=already+qty >= o['quantity']
        db.execute('UPDATE sales_orders SET stage=? WHERE id=?',('Dispatched' if fully_dispatched else 'Ready for Dispatch',oid))
        db.commit()
    except Exception:
        db.rollback(); raise
    audit('CREATE','shipment',sid,f'Created {no} for {o["order_no"]}; quantity {qty}; carrier reference {tracking or "not provided"}')
    flash(f'{no} recorded for {qty} pieces. Finished-goods stock and the movement ledger were updated.','success'); return redirect(url_for('dispatch'))

@app.route('/actions/shipment/<int:sid>/update',methods=['POST'])
@login_required
def action_shipment_update(sid):
    denied=role_action(['Dispatch Manager']);
    if denied: return denied
    s=row('SELECT * FROM shipments WHERE id=?',(sid,));
    if not s: abort(404)
    status=request.form.get('status','In Transit')
    if status not in ['Packed','Dispatched','In Transit','Out for Delivery','Delivered','Exception']: abort(400)
    if s['status']=='Delivered' and status!='Delivered':
        flash('A delivered shipment cannot be moved back to an earlier status.','error'); return redirect(url_for('dispatch'))
    delivered=today() if status=='Delivered' else s['delivered_at']
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE shipments SET status=?,delivered_at=? WHERE id=?',(status,delivered,sid))
        db.execute('INSERT INTO shipment_events(shipment_id,event_date,status,location,note) VALUES(?,?,?,?,?)',(sid,today(),status,request.form.get('location','Manually updated'),request.form.get('note','Dispatch team update.')))
        order=db.execute('SELECT quantity FROM sales_orders WHERE id=?',(s['order_id'],)).fetchone()
        totals=db.execute("SELECT COALESCE(SUM(quantity),0) shipped,COALESCE(SUM(CASE WHEN status!='Delivered' THEN 1 ELSE 0 END),0) open_shipments FROM shipments WHERE order_id=?",(s['order_id'],)).fetchone()
        if status=='Delivered' and totals['open_shipments']==0 and totals['shipped']>=order['quantity']:
            db.execute('UPDATE sales_orders SET stage="Delivered" WHERE id=?',(s['order_id'],))
        elif status!='Delivered' and totals['shipped']<order['quantity']:
            db.execute('UPDATE sales_orders SET stage="Ready for Dispatch" WHERE id=? AND stage!="Delivered"',(s['order_id'],))
        db.commit()
    except Exception:
        db.rollback(); raise
    audit('UPDATE','shipment',sid,f'{s["shipment_no"]} status changed to {status}'); flash(f'{s["shipment_no"]} tracking updated: {status}.','success'); return redirect(url_for('dispatch'))

@app.route('/actions/invoice',methods=['POST'])
@login_required
def action_invoice():
    denied=role_action(['Finance Executive']);
    if denied: return denied
    oid=form_num('order_id',0,int); o=row('SELECT * FROM sales_orders WHERE id=?',(oid,));
    if not o: flash('Select a valid order.','error'); return redirect(url_for('finance'))
    if row('SELECT id FROM invoices WHERE order_id=?',(oid,)): flash('An invoice already exists for this order.','warning'); return redirect(url_for('finance'))
    no='INV-'+datetime.now().strftime('%y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); inv=write('INSERT INTO invoices(invoice_no,order_id,customer_id,issue_date,due_date,total,paid,status) VALUES(?,?,?,?,?,?,?,?)',(no,oid,o['customer_id'],today(),(request.form.get('due_date') or dt_days(30)),o['total_value'],0,'Issued'))
    audit('CREATE','invoice',inv,f'Created invoice {no}'); flash(f'Invoice {no} issued.','success'); return redirect(url_for('finance'))

@app.route('/actions/invoice/<int:inv_id>/payment',methods=['POST'])
@login_required
def action_payment(inv_id):
    denied=role_action(['Finance Executive']);
    if denied: return denied
    inv=row('SELECT * FROM invoices WHERE id=?',(inv_id,));
    if not inv: abort(404)
    amount=form_num('amount',0)
    if amount<=0 or amount>round(inv['total']-inv['paid']+.0001,2): flash('Payment must be positive and cannot exceed the outstanding balance.','error'); return redirect(url_for('finance'))
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('INSERT INTO payments(invoice_id,amount,date,method,reference) VALUES(?,?,?,?,?)',(inv_id,amount,today(),request.form.get('method','Bank Transfer'),request.form.get('reference','')))
        paid=round(inv['paid']+amount,2); status='Paid' if paid>=inv['total'] else 'Partially Paid'
        db.execute('UPDATE invoices SET paid=?,status=? WHERE id=?',(paid,status,inv_id)); db.commit()
    except Exception: db.rollback(); raise
    audit('PAYMENT','invoice',inv_id,f'Payment {amount:.2f}; status {status}'); flash(f'Payment of {money(amount)} recorded. Invoice is {status}.','success'); return redirect(url_for('finance'))

@app.route('/actions/task',methods=['POST'])
@login_required
def action_task():
    denied=role_action(['Administrator','Sales Manager','Production Manager','Inventory Manager','Quality Inspector','Procurement Manager','Finance Executive','Dispatch Manager']);
    if denied: return denied
    title=request.form.get('title','').strip()
    if not title: flash('Task title is required.','error'); return redirect(url_for('tasks_page'))
    tid=write('INSERT INTO tasks(title,description,due_date,priority,status,assigned_to,category) VALUES(?,?,?,?,?,?,?)',(title,request.form.get('description',''),(request.form.get('due_date') or dt_days(1)),request.form.get('priority','Normal'),'Open',request.form.get('assigned_to',g.user['name']),request.form.get('category','Operations')))
    audit('CREATE','task',tid,f'Created task {title}'); flash('Task created.','success'); return redirect(url_for('tasks_page'))

@app.route('/actions/task/<int:tid>/status',methods=['POST'])
@login_required
def action_task_status(tid):
    status=request.form.get('status','Done')
    if status not in ['Open','In Progress','Done']: abort(400)
    write('UPDATE tasks SET status=? WHERE id=?',(status,tid)); audit('UPDATE','task',tid,f'Status changed to {status}'); flash(f'Task updated to {status}.','success'); return redirect(url_for('tasks_page'))

@app.route('/actions/user',methods=['POST'])
@login_required
def action_user():
    denied=role_action(['Administrator']);
    if denied: return denied
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); role=request.form.get('role','Read-only Executive'); pwd=request.form.get('password','')
    if not name or '@' not in email or len(pwd)<12 or role not in ROLE_RULES: flash('Name, valid email, role and password of at least 12 characters are required.','error'); return redirect(url_for('users_page'))
    try: uid=write('INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)',(name,email,generate_password_hash(pwd),role))
    except sqlite3.IntegrityError: flash('That email is already registered.','error'); return redirect(url_for('users_page'))
    audit('CREATE','user',uid,f'Created account {email} with role {role}'); flash(f'User {email} created.','success'); return redirect(url_for('users_page'))

@app.route('/actions/user/<int:uid>/toggle',methods=['POST'])
@login_required
def action_user_toggle(uid):
    denied=role_action(['Administrator']);
    if denied: return denied
    if uid==g.user['id']: flash('You cannot deactivate your own active session.','error'); return redirect(url_for('users_page'))
    u=row('SELECT * FROM users WHERE id=?',(uid,));
    if not u: abort(404)
    active=0 if u['active'] else 1
    write('UPDATE users SET active=? WHERE id=?',(active,uid)); audit('UPDATE','user',uid,f'Account active set to {active}'); flash('User access updated.','success'); return redirect(url_for('users_page'))

@app.route('/actions/copilot',methods=['POST'])
@login_required
def action_copilot():
    require_module('intelligence')
    question=request.form.get('question','').strip().lower()
    d=dashboard_data()
    if any(x in question for x in ['risk','late','delay','delivery']):
        data=d['at_risk'][:5]
        answer='Delivery risk is calculated with transparent rules using due-date buffer, production completion, current stage and priority.'
        results=[{'title':x['order_no']+' · '+x['customer_name'],'value':x['risk_level']+' risk', 'detail':x['risk_reason'], 'metric':x['promised_date']} for x in data]
        if not results: answer='No orders currently cross the configured High/Critical delivery-risk threshold.'
    elif any(x in question for x in ['reorder','shortage','material','stock','inventory']):
        data=sorted(d['low_stock'],key=lambda x:(x['on_hand']-x['reserved'])-x['reorder_level'])[:8]
        answer='Reorder candidates use available stock (on-hand less reserved) compared with each item’s reorder level. Forecast demand is a rules-based estimate, not a validated prediction.'
        results=[{'title':x['item_code']+' · '+x['name'],'value':f"{x['on_hand']-x['reserved']:g} {x['unit']} available",'detail':f"Reorder level {x['reorder_level']:g}; supplier lead time should be checked before buying.",'metric':money(x['avg_cost']*max(0,x['reorder_level']-(x['on_hand']-x['reserved'])))} for x in data]
        if not results: answer='No items are below their configured reorder threshold.'
    elif any(x in question for x in ['receivable','outstanding','invoice','payment','balance']):
        data=sorted([i for i in d['invoices'] if i['total']>i['paid']],key=lambda x:x['total']-x['paid'],reverse=True)[:6]
        answer=f"Open customer receivables total {money(d['open_receivables'])}. These figures are calculated from current invoice balances."
        results=[{'title':x['invoice_no']+' · '+x['customer_name'],'value':money(x['total']-x['paid']),'detail':f"Due {x['due_date']}; status {x['status']}.",'metric':x['order_no']} for x in data]
    elif any(x in question for x in ['reject','quality','defect']):
        results=rows('SELECT defect_category,COUNT(*) inspections,SUM(rejected_units) rejects FROM quality_inspections GROUP BY defect_category ORDER BY rejects DESC LIMIT 6')
        answer='Defect categories are ranked by rejected units recorded in inspections. Review the inspection volume and context before treating trends as conclusive.'
        results=[{'title':x['defect_category'] or 'Unspecified defect','value':f"{x['rejects'] or 0} rejects",'detail':f"Recorded in {x['inspections']} inspection(s).",'metric':'Quality data'} for x in results]
    elif any(x in question for x in ['production','line','output','batch']):
        data=rows('SELECT b.*,o.order_no FROM production_batches b JOIN sales_orders o ON o.id=b.sales_order_id ORDER BY (CAST(b.completed_qty AS REAL)/MAX(b.planned_qty,1)) ASC LIMIT 6')
        answer='Production progress compares completed + rejected units against planned quantity. Check the floor updates for detail on each shift.'
        results=[{'title':x['batch_no']+' · '+x['order_no'],'value':f"{round((x['completed_qty']+x['rejected_qty'])/max(x['planned_qty'],1)*100)}% accounted",'detail':f"Stage: {x['stage']}; {x['planned_qty']-x['completed_qty']-x['rejected_qty']} units remain to account for.",'metric':x['target_date']} for x in data]
    elif any(x in question for x in ['value','valuation','worth']):
        results=[{'title':'Current inventory valuation','value':money(d['inventory_value']),'detail':'Calculated from on-hand quantity × average cost across inventory items.','metric':f'{len(d["inventory"])} SKUs'}]
        answer='Inventory valuation is calculated from recorded on-hand balances and average item cost.'
    else:
        results=[{'title':'Orders at risk','value':str(len(d['at_risk'])),'detail':'Ask “Which orders are at risk of missing delivery?”','metric':'Delivery Risk Radar'}, {'title':'Low-stock SKUs','value':str(len(d['low_stock'])),'detail':'Ask “Which materials should we reorder?”','metric':'Material planning'}, {'title':'Outstanding receivables','value':money(d['open_receivables']),'detail':'Ask “Which customers have the highest outstanding balances?”','metric':'Finance'}]
        answer='I analyse the records saved in this workspace using transparent business rules. Try asking about delivery risk, stock shortages, invoice balances, quality defects, production output, or inventory valuation.'
    return jsonify({'answer':answer,'results':results,'data_source':'workspace_records','analysis_type':'rule_based'})

@app.route('/export/<kind>.csv')
@login_required
def export_csv(kind):
    require_module('reports')
    if kind=='orders':
        data=rows('SELECT o.order_no,c.name customer,o.product_name,o.quantity,o.total_value,o.stage,o.priority,o.promised_date FROM sales_orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.id DESC')
    elif kind=='inventory':
        data=rows('SELECT item_code,name,category,unit,on_hand,reserved,(on_hand-reserved) available,reorder_level,avg_cost,(on_hand*avg_cost) value,warehouse,location FROM inventory_items ORDER BY item_code')
    elif kind=='receivables':
        data=rows('SELECT i.invoice_no,c.name customer,o.order_no,i.issue_date,i.due_date,i.total,i.paid,(i.total-i.paid) outstanding,i.status FROM invoices i JOIN customers c ON c.id=i.customer_id JOIN sales_orders o ON o.id=i.order_id ORDER BY i.due_date')
    elif kind=='movements':
        data=rows('SELECT m.id,m.date,i.item_code,i.name item,m.movement_type,m.qty_in,m.qty_out,m.reference,m.source,m.destination,m.notes FROM inventory_movements m JOIN inventory_items i ON i.id=m.inventory_item_id ORDER BY m.id DESC')
    else: abort(404)
    output=io.StringIO(); writer=csv.DictWriter(output,fieldnames=list(data[0].keys()) if data else ['message']); writer.writeheader()
    if data: writer.writerows(data)
    else: writer.writerow({'message':'No records found'})
    resp=make_response('\ufeff'+output.getvalue()); resp.headers['Content-Type']='text/csv; charset=utf-8'; resp.headers['Content-Disposition']=f'attachment; filename=loomos_{kind}_{today()}.csv'; return resp

@app.route('/health')
def health(): return jsonify({'status':'ok','app':'LoomOS','environment':APP_ENV,'database':'sqlite','workspace_setup_required':not workspace_has_admin()})

if __name__ == '__main__':
    app.run(host=os.environ.get('LOOMOS_HOST','127.0.0.1'), port=int(os.environ.get('PORT',5000)), debug=os.environ.get('FLASK_DEBUG','0')=='1')
