import os
import sqlite3
from datetime import date, datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "orderflow.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_IN_RENDER")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def q(sql, args=(), one=False):
    c = conn()
    rows = c.execute(sql, args).fetchall()
    c.close()
    return rows[0] if one and rows else (None if one else rows)

def execute(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    last = cur.lastrowid
    c.close()
    return last

def now():
    return datetime.utcnow().isoformat(timespec="seconds")

def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS organisations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        business_type TEXT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'GBP',
        vat_registered INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        address TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        UNIQUE(organisation_id,email),
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS sales(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        sale_date TEXT NOT NULL,
        category TEXT NOT NULL,
        net REAL NOT NULL DEFAULT 0,
        vat REAL NOT NULL DEFAULT 0,
        gross REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'Manual',
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS expenses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        expense_date TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        supplier TEXT DEFAULT '',
        net REAL NOT NULL DEFAULT 0,
        vat REAL NOT NULL DEFAULT 0,
        gross REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'Posted',
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        supplier TEXT NOT NULL,
        invoice_number TEXT NOT NULL,
        invoice_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        category TEXT NOT NULL,
        net REAL NOT NULL,
        vat REAL NOT NULL,
        gross REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'Awaiting approval',
        approved_by INTEGER,
        approved_at TEXT,
        paid_at TEXT,
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
        FOREIGN KEY(approved_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        payment_type TEXT NOT NULL,
        payee TEXT NOT NULL,
        reference TEXT DEFAULT '',
        amount REAL NOT NULL,
        payment_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Pending approval',
        method TEXT NOT NULL DEFAULT 'Bank transfer',
        source_id INTEGER,
        approved_by INTEGER,
        approved_at TEXT,
        paid_at TEXT,
        external_reference TEXT DEFAULT '',
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
        FOREIGN KEY(approved_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS employees(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT DEFAULT '',
        department TEXT NOT NULL DEFAULT 'FOH',
        job_title TEXT DEFAULT '',
        pay_type TEXT NOT NULL DEFAULT 'Hourly',
        pay_rate REAL NOT NULL DEFAULT 0,
        ni_rate REAL NOT NULL DEFAULT 0,
        pension_rate REAL NOT NULL DEFAULT 0,
        holiday_allowance REAL NOT NULL DEFAULT 28,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS shifts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        employee_id INTEGER NOT NULL,
        shift_date TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        break_minutes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'Scheduled',
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
        FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS payroll_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        gross_pay REAL NOT NULL,
        employer_costs REAL NOT NULL DEFAULT 0,
        deductions REAL NOT NULL DEFAULT 0,
        net_pay REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'Draft',
        created_at TEXT NOT NULL,
        approved_by INTEGER,
        approved_at TEXT,
        paid_at TEXT,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
        FOREIGN KEY(approved_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS stock_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        unit TEXT NOT NULL,
        on_hand REAL NOT NULL DEFAULT 0,
        par_level REAL NOT NULL DEFAULT 0,
        unit_cost REAL NOT NULL DEFAULT 0,
        supplier TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS stock_movements(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        stock_item_id INTEGER NOT NULL,
        quantity REAL NOT NULL,
        movement_type TEXT NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(stock_item_id) REFERENCES stock_items(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS suppliers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        contact TEXT DEFAULT '',
        email TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        payment_terms INTEGER NOT NULL DEFAULT 30,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS purchase_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        supplier_id INTEGER NOT NULL,
        order_date TEXT NOT NULL,
        delivery_date TEXT,
        total REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'Draft',
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS menu_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        selling_price REAL NOT NULL,
        recipe_cost REAL NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS budgets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER NOT NULL,
        site_id INTEGER NOT NULL,
        month TEXT NOT NULL,
        revenue REAL NOT NULL DEFAULT 0,
        food_cost REAL NOT NULL DEFAULT 0,
        drink_cost REAL NOT NULL DEFAULT 0,
        labour REAL NOT NULL DEFAULT 0,
        overheads REAL NOT NULL DEFAULT 0,
        UNIQUE(site_id,month),
        FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
        FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        organisation_id INTEGER,
        user_id INTEGER,
        action TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id INTEGER,
        detail TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    """)
    c.commit()
    c.close()

init_db()

def user():
    uid = session.get("user_id")
    if not uid:
        return None
    return q("SELECT * FROM users WHERE id=? AND active=1", (uid,), True)

def org():
    u = user()
    return q("SELECT * FROM organisations WHERE id=?", (u["organisation_id"],), True) if u else None

def current_site():
    u = user()
    if not u: return None
    sid = session.get("site_id")
    if sid:
        s = q("SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1",(sid,u["organisation_id"]),True)
        if s: return s
    return q("SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1",(u["organisation_id"],),True)

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def manager_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = user()
        if not u or u["role"] not in ("Owner","Admin","Finance","General Manager"):
            return jsonify(error="Manager permission required"), 403
        return fn(*args, **kwargs)
    return wrapped

def audit(action, entity, entity_id=None, detail=""):
    u=user()
    execute("INSERT INTO audit_log(organisation_id,user_id,action,entity,entity_id,detail,created_at) VALUES(?,?,?,?,?,?,?)",
            (u["organisation_id"] if u else None,u["id"] if u else None,action,entity,entity_id,detail,now()))

def month_bounds(month):
    try:
        y,m=map(int,month.split("-"))
    except:
        y,m=date.today().year,date.today().month
    start=f"{y:04d}-{m:02d}-01"
    if m==12: end=f"{y+1:04d}-01-01"
    else: end=f"{y:04d}-{m+1:02d}-01"
    return start,end

def finance_summary(month=None):
    u=user(); s=current_site()
    month=month or date.today().strftime("%Y-%m")
    start,end=month_bounds(month)
    a=q("""SELECT COALESCE(SUM(gross),0) gross FROM sales
           WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<?""",
        (u["organisation_id"],s["id"],start,end),True)
    food=q("""SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND
              sale_date>=? AND sale_date<? AND category='Food'""",(u["organisation_id"],s["id"],start,end),True)["x"]
    drink=q("""SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND
              sale_date>=? AND sale_date<? AND category='Drink'""",(u["organisation_id"],s["id"],start,end),True)["x"]
    rev=float(a["gross"] or 0)
    food=float(food or 0); drink=float(drink or 0)
    # COGS is sourced from stock movement costs when available; otherwise zero until actual purchases/stock are entered.
    cogs=q("""SELECT COALESCE(SUM(ABS(sm.quantity)*si.unit_cost),0) x FROM stock_movements sm
              JOIN stock_items si ON si.id=sm.stock_item_id
              WHERE sm.organisation_id=? AND sm.site_id=? AND sm.created_at>=? AND sm.created_at<?""",
           (u["organisation_id"],s["id"],start,end),True)["x"]
    labour=q("""SELECT COALESCE(SUM(
                CASE WHEN sh.status!='Cancelled' THEN
                MAX(0,((CAST(substr(sh.end_time,1,2) AS INTEGER)*60+CAST(substr(sh.end_time,4,2) AS INTEGER))-
                (CAST(substr(sh.start_time,1,2) AS INTEGER)*60+CAST(substr(sh.start_time,4,2) AS INTEGER))) / 60.0
                - sh.break_minutes/60.0) * e.pay_rate ELSE 0 END),0) x
              FROM shifts sh JOIN employees e ON e.id=sh.employee_id
              WHERE sh.organisation_id=? AND sh.site_id=? AND sh.shift_date>=? AND sh.shift_date<?""",
           (u["organisation_id"],s["id"],start,end),True)["x"]
    overheads=q("""SELECT COALESCE(SUM(gross),0) x FROM expenses
                   WHERE organisation_id=? AND site_id=? AND expense_date>=? AND expense_date<? 
                   AND category NOT IN ('Food','Drink','Labour')""",
                (u["organisation_id"],s["id"],start,end),True)["x"]
    gross_profit=rev-float(cogs)
    ebitda=gross_profit-float(labour)-float(overheads)
    return dict(month=month,revenue=rev,food=food,drink=drink,cogs=float(cogs),gross_profit=gross_profit,
                labour=float(labour),overheads=float(overheads),ebitda=ebitda,
                gross_margin=(gross_profit/rev*100 if rev else 0),
                ebitda_margin=(ebitda/rev*100 if rev else 0))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=(request.form.get("email") or "").strip().lower()
        password=request.form.get("password") or ""
        u=q("SELECT * FROM users WHERE email=? AND active=1",(email,),True)
        if u and check_password_hash(u["password_hash"],password):
            session.clear(); session["user_id"]=u["id"]
            site=q("SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1",(u["organisation_id"],),True)
            if site: session["site_id"]=site["id"]
            return redirect(url_for("home"))
        return render_template("login.html",error="Incorrect email or password.")
    return render_template("login.html")

@app.route("/logout",methods=["POST","GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/onboarding", methods=["GET","POST"])
def onboarding():
    if user(): return redirect(url_for("home"))
    if request.method=="POST":
        business=(request.form.get("business_name") or "").strip()
        btype=(request.form.get("business_type") or "Restaurant").strip()
        site=(request.form.get("site_name") or "").strip()
        address=(request.form.get("address") or "").strip()
        name=(request.form.get("owner_name") or "").strip()
        email=(request.form.get("email") or "").strip().lower()
        password=request.form.get("password") or ""
        if not all([business,site,name,email]) or len(password)<8:
            return render_template("onboarding.html",error="Complete all fields and use a password of at least 8 characters.")
        c=conn()
        try:
            cur=c.execute("INSERT INTO organisations(name,business_type,created_at) VALUES(?,?,?)",(business,btype,now()))
            oid=cur.lastrowid
            cur=c.execute("INSERT INTO sites(organisation_id,name,address,created_at) VALUES(?,?,?,?)",(oid,site,address,now()))
            sid=cur.lastrowid
            cur=c.execute("""INSERT INTO users(organisation_id,name,email,password_hash,role,created_at)
                             VALUES(?,?,?,?,?,?)""",(oid,name,email,generate_password_hash(password),"Owner",now()))
            uid=cur.lastrowid
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback(); c.close()
            return render_template("onboarding.html",error="That email is already registered for this business.")
        c.close()
        session["user_id"]=uid; session["site_id"]=sid
        return redirect(url_for("home"))
    return render_template("onboarding.html")

@app.get("/")
@login_required
def home():
    return render_template("app.html", user=user(), organisation=org(), site=current_site())

@app.get("/api/me")
@login_required
def api_me():
    u=user(); o=org(); s=current_site()
    return jsonify(user=dict(u),organisation=dict(o),site=dict(s) if s else None)

@app.post("/api/site")
@login_required
@manager_required
def add_site():
    u=user(); data=request.get_json() or {}
    name=(data.get("name") or "").strip()
    if not name: return jsonify(error="Site name is required"),400
    sid=execute("INSERT INTO sites(organisation_id,name,address,created_at) VALUES(?,?,?,?)",
                (u["organisation_id"],name,(data.get("address") or "").strip(),now()))
    audit("Created","site",sid,name)
    return jsonify(ok=True,id=sid)

@app.post("/api/site/select")
@login_required
def select_site():
    u=user(); sid=int((request.get_json() or {}).get("site_id",0))
    s=q("SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1",(sid,u["organisation_id"]),True)
    if not s:return jsonify(error="Site not found"),404
    session["site_id"]=sid
    return jsonify(ok=True)

@app.get("/api/sites")
@login_required
def sites():
    u=user()
    return jsonify(sites=[dict(x) for x in q("SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY name",(u["organisation_id"],))])

@app.get("/api/dashboard")
@login_required
def dashboard():
    f=finance_summary()
    u=user(); s=current_site()
    pending=float(q("""SELECT COALESCE(SUM(gross),0) x FROM invoices WHERE organisation_id=? AND site_id=? AND status IN ('Awaiting approval','Approved')""",(u["organisation_id"],s["id"]),True)["x"])
    stock_low=q("""SELECT COUNT(*) x FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 AND on_hand<par_level""",(u["organisation_id"],s["id"]),True)["x"]
    payroll=q("""SELECT COALESCE(SUM(net_pay),0) x FROM payroll_runs WHERE organisation_id=? AND site_id=? AND status IN ('Draft','Approved')""",(u["organisation_id"],s["id"]),True)["x"]
    return jsonify(**f,outstanding_invoices=pending,low_stock=stock_low,payroll_due=float(payroll or 0))

@app.get("/api/finance")
@login_required
def api_finance():
    return jsonify(finance_summary(request.args.get("month")))

@app.get("/api/sales")
@login_required
def get_sales():
    u=user();s=current_site()
    rows=q("SELECT * FROM sales WHERE organisation_id=? AND site_id=? ORDER BY sale_date DESC,id DESC LIMIT 200",(u["organisation_id"],s["id"]))
    return jsonify(sales=[dict(r) for r in rows])

@app.post("/api/sales")
@login_required
@manager_required
def add_sale():
    u=user();s=current_site();d=request.get_json() or {}
    try: gross=float(d.get("gross",0)); vat=float(d.get("vat",0))
    except: return jsonify(error="Invalid amount"),400
    if gross<=0:return jsonify(error="Gross sales must be greater than zero"),400
    sid=execute("""INSERT INTO sales(organisation_id,site_id,sale_date,category,net,vat,gross,source)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],d.get("sale_date") or date.today().isoformat(),
                 d.get("category") or "Other",gross-vat,vat,gross,d.get("source") or "Manual"))
    audit("Created","sale",sid)
    return jsonify(ok=True,id=sid)

@app.get("/api/expenses")
@login_required
def get_expenses():
    u=user();s=current_site()
    rows=q("SELECT * FROM expenses WHERE organisation_id=? AND site_id=? ORDER BY expense_date DESC,id DESC LIMIT 200",(u["organisation_id"],s["id"]))
    return jsonify(expenses=[dict(r) for r in rows])

@app.post("/api/expenses")
@login_required
@manager_required
def add_expense():
    u=user();s=current_site();d=request.get_json() or {}
    try: gross=float(d.get("gross",0));vat=float(d.get("vat",0))
    except:return jsonify(error="Invalid amount"),400
    if gross<=0:return jsonify(error="Amount must be greater than zero"),400
    eid=execute("""INSERT INTO expenses(organisation_id,site_id,expense_date,category,description,supplier,net,vat,gross)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],d.get("expense_date") or date.today().isoformat(),
                 d.get("category") or "Other",d.get("description") or "Expense",d.get("supplier") or "",gross-vat,vat,gross))
    audit("Created","expense",eid)
    return jsonify(ok=True,id=eid)

@app.get("/api/invoices")
@login_required
def get_invoices():
    u=user();s=current_site()
    rows=q("SELECT * FROM invoices WHERE organisation_id=? AND site_id=? ORDER BY due_date,id DESC",(u["organisation_id"],s["id"]))
    return jsonify(invoices=[dict(r) for r in rows])

@app.post("/api/invoices")
@login_required
@manager_required
def add_invoice():
    u=user();s=current_site();d=request.get_json() or {}
    try: net=float(d.get("net",0));vat=float(d.get("vat",0))
    except:return jsonify(error="Invalid amount"),400
    if not d.get("supplier") or not d.get("invoice_number") or net<=0:return jsonify(error="Supplier, invoice number and positive net amount are required"),400
    iid=execute("""INSERT INTO invoices(organisation_id,site_id,supplier,invoice_number,invoice_date,due_date,category,net,vat,gross,notes,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],d["supplier"],d["invoice_number"],d.get("invoice_date") or date.today().isoformat(),
                 d.get("due_date") or date.today().isoformat(),d.get("category") or "Other",net,vat,net+vat,d.get("notes") or "",now()))
    audit("Created","invoice",iid,d["invoice_number"])
    return jsonify(ok=True,id=iid)

@app.post("/api/invoices/<int:iid>/approve")
@login_required
@manager_required
def approve_invoice(iid):
    u=user();s=current_site()
    row=q("SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?",(iid,u["organisation_id"],s["id"]),True)
    if not row:return jsonify(error="Invoice not found"),404
    if row["status"]!="Awaiting approval":return jsonify(error="Invoice is not awaiting approval"),400
    execute("UPDATE invoices SET status='Approved',approved_by=?,approved_at=? WHERE id=?",(u["id"],now(),iid))
    pid=execute("""INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],"Supplier",row["supplier"],row["invoice_number"],row["gross"],row["due_date"],
                 "Scheduled","Bank transfer",iid,u["id"],now()))
    audit("Approved","invoice",iid,"Payment "+str(pid)+" scheduled")
    return jsonify(ok=True)

@app.post("/api/invoices/<int:iid>/pay")
@login_required
@manager_required
def pay_invoice(iid):
    u=user();s=current_site()
    row=q("SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?",(iid,u["organisation_id"],s["id"]),True)
    if not row or row["status"]!="Approved":return jsonify(error="Invoice must be approved first"),400
    execute("UPDATE invoices SET status='Paid',paid_at=? WHERE id=?",(now(),iid))
    execute("UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Supplier'",(now(),iid))
    audit("Recorded paid","invoice",iid)
    return jsonify(ok=True)

@app.get("/api/payments")
@login_required
def get_payments():
    u=user();s=current_site()
    rows=q("SELECT * FROM payments WHERE organisation_id=? AND site_id=? ORDER BY payment_date,id DESC",(u["organisation_id"],s["id"]))
    return jsonify(payments=[dict(r) for r in rows])

@app.get("/api/employees")
@login_required
def get_employees():
    u=user();s=current_site()
    return jsonify(employees=[dict(x) for x in q("SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",(u["organisation_id"],s["id"]))])

@app.post("/api/employees")
@login_required
@manager_required
def add_employee():
    u=user();s=current_site();d=request.get_json() or {}
    try: rate=float(d.get("pay_rate",0))
    except:return jsonify(error="Invalid pay rate"),400
    if not d.get("name"):return jsonify(error="Name required"),400
    eid=execute("""INSERT INTO employees(organisation_id,site_id,name,email,department,job_title,pay_type,pay_rate,ni_rate,pension_rate,holiday_allowance)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],d["name"],d.get("email") or "",d.get("department") or "FOH",
                 d.get("job_title") or "",d.get("pay_type") or "Hourly",rate,float(d.get("ni_rate",0) or 0),
                 float(d.get("pension_rate",0) or 0),float(d.get("holiday_allowance",28) or 28)))
    audit("Created","employee",eid,d["name"])
    return jsonify(ok=True,id=eid)

@app.get("/api/shifts")
@login_required
def get_shifts():
    u=user();s=current_site()
    rows=q("""SELECT sh.*,e.name employee_name,e.pay_rate FROM shifts sh JOIN employees e ON e.id=sh.employee_id
              WHERE sh.organisation_id=? AND sh.site_id=? ORDER BY shift_date,start_time""",(u["organisation_id"],s["id"]))
    return jsonify(shifts=[dict(x) for x in rows])

@app.post("/api/shifts")
@login_required
@manager_required
def add_shift():
    u=user();s=current_site();d=request.get_json() or {}
    try: eid=int(d.get("employee_id")); breakm=int(d.get("break_minutes",0))
    except:return jsonify(error="Invalid employee"),400
    if not d.get("shift_date") or not d.get("start_time") or not d.get("end_time"):return jsonify(error="Complete shift details"),400
    sid=execute("""INSERT INTO shifts(organisation_id,site_id,employee_id,shift_date,start_time,end_time,break_minutes)
                   VALUES(?,?,?,?,?,?,?)""",(u["organisation_id"],s["id"],eid,d["shift_date"],d["start_time"],d["end_time"],breakm))
    return jsonify(ok=True,id=sid)

@app.get("/api/payroll")
@login_required
def get_payroll():
    u=user();s=current_site()
    runs=q("SELECT * FROM payroll_runs WHERE organisation_id=? AND site_id=? ORDER BY period_end DESC",(u["organisation_id"],s["id"]))
    emps=q("SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",(u["organisation_id"],s["id"]))
    return jsonify(runs=[dict(x) for x in runs],employees=[dict(x) for x in emps])

@app.post("/api/payroll")
@login_required
@manager_required
def add_payroll():
    u=user();s=current_site();d=request.get_json() or {}
    try:
        gross=float(d.get("gross_pay",0)); employer=float(d.get("employer_costs",0)); deductions=float(d.get("deductions",0))
    except:return jsonify(error="Invalid payroll values"),400
    if gross<=0 or deductions<0 or deductions>gross:return jsonify(error="Invalid payroll values"),400
    rid=execute("""INSERT INTO payroll_runs(organisation_id,site_id,period_start,period_end,gross_pay,employer_costs,deductions,net_pay,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],d["period_start"],d["period_end"],gross,employer,deductions,gross-deductions,now()))
    audit("Created","payroll",rid)
    return jsonify(ok=True,id=rid)

@app.post("/api/payroll/<int:rid>/approve")
@login_required
@manager_required
def approve_payroll(rid):
    u=user();s=current_site()
    row=q("SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?",(rid,u["organisation_id"],s["id"]),True)
    if not row:return jsonify(error="Payroll run not found"),404
    execute("UPDATE payroll_runs SET status='Approved',approved_by=?,approved_at=? WHERE id=?",(u["id"],now(),rid))
    execute("""INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u["organisation_id"],s["id"],"Payroll","Staff payroll",f"PAY-{rid}",row["net_pay"],date.today().isoformat(),"Scheduled","Bank transfer",rid,u["id"],now()))
    audit("Approved","payroll",rid)
    return jsonify(ok=True)

@app.post("/api/payroll/<int:rid>/pay")
@login_required
@manager_required
def pay_payroll(rid):
    u=user();s=current_site()
    row=q("SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?",(rid,u["organisation_id"],s["id"]),True)
    if not row or row["status"]!="Approved":return jsonify(error="Payroll must be approved first"),400
    execute("UPDATE payroll_runs SET status='Paid',paid_at=? WHERE id=?",(now(),rid))
    execute("UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Payroll'",(now(),rid))
    audit("Recorded paid","payroll",rid)
    return jsonify(ok=True)

@app.get("/api/stock")
@login_required
def get_stock():
    u=user();s=current_site()
    rows=q("SELECT * FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",(u["organisation_id"],s["id"]))
    return jsonify(stock=[dict(x) for x in rows])

@app.post("/api/stock")
@login_required
@manager_required
def add_stock():
    u=user();s=current_site();d=request.get_json() or {}
    try:onhand=float(d.get("on_hand",0));par=float(d.get("par_level",0));cost=float(d.get("unit_cost",0))
    except:return jsonify(error="Invalid stock values"),400
    iid=execute("""INSERT INTO stock_items(organisation_id,site_id,name,category,unit,on_hand,par_level,unit_cost,supplier)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (u["organisation_id"],s["id"],d.get("name"),d.get("category") or "Food",d.get("unit") or "unit",onhand,par,cost,d.get("supplier") or ""))
    return jsonify(ok=True,id=iid)

@app.post("/api/stock/<int:iid>/movement")
@login_required
@manager_required
def stock_move(iid):
    u=user();s=current_site();d=request.get_json() or {}
    try:qty=float(d.get("quantity",0))
    except:return jsonify(error="Invalid quantity"),400
    row=q("SELECT * FROM stock_items WHERE id=? AND organisation_id=? AND site_id=?",(iid,u["organisation_id"],s["id"]),True)
    if not row:return jsonify(error="Stock item not found"),404
    new=max(0,row["on_hand"]+qty)
    execute("UPDATE stock_items SET on_hand=? WHERE id=?",(new,iid))
    execute("""INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at)
               VALUES(?,?,?,?,?,?,?)""",(u["organisation_id"],s["id"],iid,qty,d.get("movement_type") or "Adjustment",d.get("note") or "",now()))
    return jsonify(ok=True,on_hand=new)

@app.get("/api/suppliers")
@login_required
def get_suppliers():
    u=user()
    return jsonify(suppliers=[dict(x) for x in q("SELECT * FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name",(u["organisation_id"],))])

@app.post("/api/suppliers")
@login_required
@manager_required
def add_supplier():
    u=user();d=request.get_json() or {}
    if not d.get("name"):return jsonify(error="Supplier name required"),400
    sid=execute("INSERT INTO suppliers(organisation_id,name,contact,email,phone,payment_terms) VALUES(?,?,?,?,?,?)",
                (u["organisation_id"],d["name"],d.get("contact") or "",d.get("email") or "",d.get("phone") or "",int(d.get("payment_terms",30))))
    return jsonify(ok=True,id=sid)

@app.get("/api/menu")
@login_required
def get_menu():
    u=user();s=current_site()
    return jsonify(menu=[dict(x) for x in q("SELECT * FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY category,name",(u["organisation_id"],s["id"]))])

@app.post("/api/menu")
@login_required
@manager_required
def add_menu():
    u=user();s=current_site();d=request.get_json() or {}
    try:p=float(d.get("selling_price",0));c=float(d.get("recipe_cost",0))
    except:return jsonify(error="Invalid prices"),400
    if not d.get("name") or p<=0:return jsonify(error="Name and positive selling price required"),400
    mid=execute("""INSERT INTO menu_items(organisation_id,site_id,name,category,selling_price,recipe_cost)
                   VALUES(?,?,?,?,?,?)""",(u["organisation_id"],s["id"],d["name"],d.get("category") or "Main",p,c))
    return jsonify(ok=True,id=mid)

@app.get("/api/budget")
@login_required
def get_budget():
    u=user();s=current_site();month=request.args.get("month") or date.today().strftime("%Y-%m")
    b=q("SELECT * FROM budgets WHERE organisation_id=? AND site_id=? AND month=?",(u["organisation_id"],s["id"],month),True)
    return jsonify(budget=dict(b) if b else None,month=month)

@app.post("/api/budget")
@login_required
@manager_required
def save_budget():
    u=user();s=current_site();d=request.get_json() or {};month=d.get("month") or date.today().strftime("%Y-%m")
    vals=[float(d.get(k,0) or 0) for k in ("revenue","food_cost","drink_cost","labour","overheads")]
    c=conn()
    c.execute("""INSERT INTO budgets(organisation_id,site_id,month,revenue,food_cost,drink_cost,labour,overheads)
                 VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(site_id,month) DO UPDATE SET revenue=excluded.revenue,food_cost=excluded.food_cost,
                 drink_cost=excluded.drink_cost,labour=excluded.labour,overheads=excluded.overheads""",
              (u["organisation_id"],s["id"],month,*vals))
    c.commit();c.close()
    return jsonify(ok=True)

@app.get("/api/audit")
@login_required
@manager_required
def audit_log():
    u=user()
    return jsonify(log=[dict(x) for x in q("SELECT * FROM audit_log WHERE organisation_id=? ORDER BY id DESC LIMIT 100",(u["organisation_id"],))])

@app.get("/health")
def health(): return jsonify(status="ok",service="OrderFlow")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
