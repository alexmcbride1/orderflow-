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
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
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
    if one:
        return rows[0] if rows else None
    return rows


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
    if not u:
        return None
    return q("SELECT * FROM organisations WHERE id=?", (u["organisation_id"],), True)


def current_site():
    u = user()
    if not u:
        return None
    sid = session.get("site_id")
    if sid:
        s = q("SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1", (sid, u["organisation_id"]), True)
        if s:
            return s
    s = q("SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1", (u["organisation_id"],), True)
    if s:
        session["site_id"] = s["id"]
    return s


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
        if not u:
            return jsonify(error="Not authenticated"), 401
        allowed = ("Owner", "Admin", "Finance", "General Manager", "Manager")
        if u["role"] not in allowed:
            return jsonify(error="Manager permission required"), 403
        return fn(*args, **kwargs)
    return wrapped


def audit(action, entity, entity_id=None, detail=""):
    u = user()
    execute(
        """INSERT INTO audit_log(organisation_id,user_id,action,entity,entity_id,detail,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (u["organisation_id"] if u else None, u["id"] if u else None, action, entity, entity_id, detail, now())
    )


def month_bounds(month):
    try:
        y, m = map(int, month.split("-"))
    except Exception:
        y = date.today().year
        m = date.today().month
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y + 1:04d}-01-01" if m == 12 else f"{y:04d}-{m + 1:02d}-01"
    return start, end


def finance_summary(month=None):
    u = user()
    s = current_site()
    month = month or date.today().strftime("%Y-%m")
    if not u or not s:
        return {"month": month, "revenue": 0, "food": 0, "drink": 0, "cogs": 0, "gross_profit": 0, "labour": 0, "overheads": 0, "ebitda": 0, "gross_margin": 0, "ebitda_margin": 0}

    start, end = month_bounds(month)
    revenue_row = q("SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<?", (u["organisation_id"], s["id"], start, end), True)
    food_row = q("SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<? AND category='Food'", (u["organisation_id"], s["id"], start, end), True)
    drink_row = q("SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<? AND category='Drink'", (u["organisation_id"], s["id"], start, end), True)

    revenue = float(revenue_row["x"] or 0)
    food = float(food_row["x"] or 0)
    drink = float(drink_row["x"] or 0)

    cogs_row = q("""SELECT COALESCE(SUM(ABS(sm.quantity) * si.unit_cost),0) x
                     FROM stock_movements sm JOIN stock_items si ON si.id=sm.stock_item_id
                     WHERE sm.organisation_id=? AND sm.site_id=? AND sm.created_at>=? AND sm.created_at<?
                     AND LOWER(sm.movement_type) IN ('usage','waste','sale','cogs')""",
                  (u["organisation_id"], s["id"], start, end), True)
    cogs = float(cogs_row["x"] or 0)

    labour_row = q("""SELECT COALESCE(SUM(CASE WHEN sh.status!='Cancelled' THEN
                     MAX(0,(((CAST(substr(sh.end_time,1,2) AS INTEGER)*60+CAST(substr(sh.end_time,4,2) AS INTEGER))-
                     (CAST(substr(sh.start_time,1,2) AS INTEGER)*60+CAST(substr(sh.start_time,4,2) AS INTEGER))) / 60.0)-sh.break_minutes/60.0) * e.pay_rate ELSE 0 END),0) x
                     FROM shifts sh JOIN employees e ON e.id=sh.employee_id
                     WHERE sh.organisation_id=? AND sh.site_id=? AND sh.shift_date>=? AND sh.shift_date<?""",
                   (u["organisation_id"], s["id"], start, end), True)
    labour = float(labour_row["x"] or 0)

    overhead_row = q("""SELECT COALESCE(SUM(gross),0) x FROM expenses
                        WHERE organisation_id=? AND site_id=? AND expense_date>=? AND expense_date<?
                        AND category NOT IN ('Food','Drink','Labour')""",
                      (u["organisation_id"], s["id"], start, end), True)
    overheads = float(overhead_row["x"] or 0)

    gross_profit = revenue - cogs
    ebitda = gross_profit - labour - overheads
    gross_margin = gross_profit / revenue * 100 if revenue else 0
    ebitda_margin = ebitda / revenue * 100 if revenue else 0

    return {"month": month, "revenue": revenue, "food": food, "drink": drink, "cogs": cogs,
            "gross_profit": gross_profit, "labour": labour, "overheads": overheads,
            "ebitda": ebitda, "gross_margin": gross_margin, "ebitda_margin": ebitda_margin}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        u = q("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,), True)
        if u and check_password_hash(u["password_hash"], password):
            session.clear()
            session["user_id"] = u["id"]
            s = q("SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1", (u["organisation_id"],), True)
            if s:
                session["site_id"] = s["id"]
            return redirect(url_for("home"))
        return render_template("login.html", error="Incorrect email or password.")
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    if user():
        return redirect(url_for("home"))
    if request.method == "POST":
        business = (request.form.get("business_name") or "").strip()
        business_type = (request.form.get("business_type") or "Restaurant").strip()
        site_name = (request.form.get("site_name") or "").strip()
        address = (request.form.get("address") or "").strip()
        owner_name = (request.form.get("owner_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not business or not site_name or not owner_name or not email or len(password) < 8:
            return render_template("onboarding.html", error="Complete all fields and use a password of at least 8 characters.")

        c = conn()
        try:
            cur = c.execute("INSERT INTO organisations(name,business_type,created_at) VALUES(?,?,?)", (business, business_type, now()))
            organisation_id = cur.lastrowid
            cur = c.execute("INSERT INTO sites(organisation_id,name,address,created_at) VALUES(?,?,?,?)", (organisation_id, site_name, address, now()))
            site_id = cur.lastrowid
            cur = c.execute("INSERT INTO users(organisation_id,name,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)", (organisation_id, owner_name, email, generate_password_hash(password), "Owner", now()))
            user_id = cur.lastrowid
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            c.close()
            return render_template("onboarding.html", error="That email is already registered for this business.")
        c.close()
        session["user_id"] = user_id
        session["site_id"] = site_id
        return redirect(url_for("home"))
    return render_template("onboarding.html")


@app.get("/")
@login_required
def home():
    return render_template("app.html", user=user(), organisation=org(), site=current_site())


@app.get("/app")
@login_required
def app_home():
    return redirect(url_for("home"))


@app.get("/api/me")
@login_required
def api_me():
    u, o, s = user(), org(), current_site()
    return jsonify(user=dict(u), organisation=dict(o), site=dict(s) if s else None)


@app.post("/api/site")
@login_required
@manager_required
def add_site():
    u = user()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    address = (d.get("address") or "").strip()
    if not name:
        return jsonify(error="Site name is required"), 400
    site_id = execute("INSERT INTO sites(organisation_id,name,address,created_at) VALUES(?,?,?,?)", (u["organisation_id"], name, address, now()))
    audit("Created", "site", site_id, name)
    return jsonify(ok=True, id=site_id)


@app.post("/api/site/select")
@login_required
def select_site():
    u = user()
    d = request.get_json() or {}
    try:
        site_id = int(d.get("site_id", 0))
    except Exception:
        return jsonify(error="Invalid site"), 400
    s = q("SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1", (site_id, u["organisation_id"]), True)
    if not s:
        return jsonify(error="Site not found"), 404
    session["site_id"] = site_id
    return jsonify(ok=True)


@app.get("/api/sites")
@login_required
def sites():
    u = user()
    result = q("SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY name", (u["organisation_id"],))
    return jsonify(sites=[dict(x) for x in result])


@app.get("/api/dashboard")
@login_required
def dashboard():
    f = finance_summary()
    u, s = user(), current_site()
    pending = q("SELECT COALESCE(SUM(gross),0) x FROM invoices WHERE organisation_id=? AND site_id=? AND status IN ('Awaiting approval','Approved')", (u["organisation_id"], s["id"]), True)
    stock_low = q("SELECT COUNT(*) x FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 AND on_hand<par_level", (u["organisation_id"], s["id"]), True)
    payroll = q("SELECT COALESCE(SUM(net_pay),0) x FROM payroll_runs WHERE organisation_id=? AND site_id=? AND status IN ('Draft','Approved')", (u["organisation_id"], s["id"]), True)
    return jsonify(**f, outstanding_invoices=float(pending["x"] or 0), low_stock=int(stock_low["x"] or 0), payroll_due=float(payroll["x"] or 0))


@app.get("/api/finance")
@login_required
def api_finance():
    return jsonify(finance_summary(request.args.get("month")))


@app.get("/api/sales")
@login_required
def get_sales():
    u, s = user(), current_site()
    result = q("SELECT * FROM sales WHERE organisation_id=? AND site_id=? ORDER BY sale_date DESC,id DESC LIMIT 200", (u["organisation_id"], s["id"]))
    return jsonify(sales=[dict(r) for r in result])


@app.post("/api/sales")
@login_required
@manager_required
def add_sale():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        gross = float(d.get("gross", 0))
        vat = float(d.get("vat", 0))
    except Exception:
        return jsonify(error="Invalid amount"), 400
    if gross <= 0:
        return jsonify(error="Gross sales must be greater than zero"), 400
    if vat < 0 or vat > gross:
        return jsonify(error="Invalid VAT amount"), 400
    sale_id = execute("""INSERT INTO sales(organisation_id,site_id,sale_date,category,net,vat,gross,source)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (u["organisation_id"], s["id"], d.get("sale_date") or date.today().isoformat(), d.get("category") or "Other", gross-vat, vat, gross, d.get("source") or "Manual"))
    audit("Created", "sale", sale_id)
    return jsonify(ok=True, id=sale_id)


@app.get("/api/expenses")
@login_required
def get_expenses():
    u, s = user(), current_site()
    result = q("SELECT * FROM expenses WHERE organisation_id=? AND site_id=? ORDER BY expense_date DESC,id DESC LIMIT 200", (u["organisation_id"], s["id"]))
    return jsonify(expenses=[dict(r) for r in result])


@app.post("/api/expenses")
@login_required
@manager_required
def add_expense():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        gross = float(d.get("gross", d.get("amount", 0)))
        vat = float(d.get("vat", 0))
    except Exception:
        return jsonify(error="Invalid amount"), 400
    if gross <= 0:
        return jsonify(error="Amount must be greater than zero"), 400
    if vat < 0 or vat > gross:
        return jsonify(error="Invalid VAT amount"), 400
    expense_id = execute("""INSERT INTO expenses(organisation_id,site_id,expense_date,category,description,supplier,net,vat,gross)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                         (u["organisation_id"], s["id"], d.get("expense_date") or date.today().isoformat(), d.get("category") or "Other", d.get("description") or "Expense", d.get("supplier") or "", gross-vat, vat, gross))
    audit("Created", "expense", expense_id)
    return jsonify(ok=True, id=expense_id)


@app.get("/api/invoices")
@login_required
def get_invoices():
    u, s = user(), current_site()
    result = q("SELECT * FROM invoices WHERE organisation_id=? AND site_id=? ORDER BY due_date,id DESC", (u["organisation_id"], s["id"]))
    return jsonify(invoices=[dict(r) for r in result])


@app.post("/api/invoices")
@login_required
@manager_required
def add_invoice():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        net = float(d.get("net", d.get("net_amount", 0)))
        vat = float(d.get("vat", 0))
    except Exception:
        return jsonify(error="Invalid amount"), 400
    if not d.get("supplier") or not d.get("invoice_number") or net <= 0:
        return jsonify(error="Supplier, invoice number and positive net amount are required"), 400
    if vat < 0:
        return jsonify(error="VAT cannot be negative"), 400
    invoice_id = execute("""INSERT INTO invoices(organisation_id,site_id,supplier,invoice_number,invoice_date,due_date,category,net,vat,gross,notes,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (u["organisation_id"], s["id"], d["supplier"], d["invoice_number"], d.get("invoice_date") or date.today().isoformat(), d.get("due_date") or date.today().isoformat(), d.get("category") or "Other", net, vat, net+vat, d.get("notes") or "", now()))
    audit("Created", "invoice", invoice_id, d["invoice_number"])
    return jsonify(ok=True, id=invoice_id)


@app.post("/api/invoices/<int:iid>/approve")
@login_required
@manager_required
def approve_invoice(iid):
    u, s = user(), current_site()
    invoice = q("SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?", (iid, u["organisation_id"], s["id"]), True)
    if not invoice:
        return jsonify(error="Invoice not found"), 404
    if invoice["status"] != "Awaiting approval":
        return jsonify(error="Invoice is not awaiting approval"), 400
    execute("UPDATE invoices SET status='Approved',approved_by=?,approved_at=? WHERE id=?", (u["id"], now(), iid))
    payment_id = execute("""INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (u["organisation_id"], s["id"], "Supplier", invoice["supplier"], invoice["invoice_number"], invoice["gross"], invoice["due_date"], "Scheduled", "Bank transfer", iid, u["id"], now()))
    audit("Approved", "invoice", iid, "Payment " + str(payment_id) + " scheduled")
    return jsonify(ok=True)


@app.post("/api/invoices/<int:iid>/pay")
@login_required
@manager_required
def pay_invoice(iid):
    u, s = user(), current_site()
    invoice = q("SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?", (iid, u["organisation_id"], s["id"]), True)
    if not invoice or invoice["status"] != "Approved":
        return jsonify(error="Invoice must be approved first"), 400
    payment_time = now()
    execute("UPDATE invoices SET status='Paid',paid_at=? WHERE id=?", (payment_time, iid))
    execute("UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Supplier'", (payment_time, iid))
    audit("Recorded paid", "invoice", iid)
    return jsonify(ok=True)


@app.get("/api/payments")
@login_required
def get_payments():
    u, s = user(), current_site()
    result = q("SELECT * FROM payments WHERE organisation_id=? AND site_id=? ORDER BY payment_date,id DESC", (u["organisation_id"], s["id"]))
    return jsonify(payments=[dict(r) for r in result])


@app.get("/api/employees")
@login_required
def get_employees():
    u, s = user(), current_site()
    result = q("SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name", (u["organisation_id"], s["id"]))
    return jsonify(employees=[dict(x) for x in result])


@app.post("/api/employees")
@login_required
@manager_required
def add_employee():
    u, s = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    try:
        rate = float(d.get("pay_rate", 0))
        ni_rate = float(d.get("ni_rate", 0))
        pension_rate = float(d.get("pension_rate", 0))
        holiday_allowance = float(d.get("holiday_allowance", 28))
    except Exception:
        return jsonify(error="Invalid employee values"), 400
    if rate < 0:
        return jsonify(error="Pay rate cannot be negative"), 400
    employee_id = execute("""INSERT INTO employees(organisation_id,site_id,name,email,department,job_title,pay_type,pay_rate,ni_rate,pension_rate,holiday_allowance)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (u["organisation_id"], s["id"], name, d.get("email") or "", d.get("department") or "FOH", d.get("job_title") or "", d.get("pay_type") or "Hourly", rate, ni_rate, pension_rate, holiday_allowance))
    audit("Created", "employee", employee_id, name)
    return jsonify(ok=True, id=employee_id)


@app.get("/api/shifts")
@login_required
def get_shifts():
    u, s = user(), current_site()
    result = q("""SELECT sh.*,e.name employee_name,e.pay_rate FROM shifts sh JOIN employees e ON e.id=sh.employee_id
                  WHERE sh.organisation_id=? AND sh.site_id=? ORDER BY shift_date,start_time""", (u["organisation_id"], s["id"]))
    return jsonify(shifts=[dict(x) for x in result])


@app.post("/api/shifts")
@login_required
@manager_required
def add_shift():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        employee_id = int(d.get("employee_id"))
        break_minutes = int(d.get("break_minutes", 0))
    except Exception:
        return jsonify(error="Invalid employee"), 400
    if not d.get("shift_date") or not d.get("start_time") or not d.get("end_time"):
        return jsonify(error="Shift date, start time and end time are required"), 400
    if break_minutes < 0:
        return jsonify(error="Break cannot be negative"), 400
    employee = q("SELECT * FROM employees WHERE id=? AND organisation_id=? AND site_id=? AND active=1", (employee_id, u["organisation_id"], s["id"]), True)
    if not employee:
        return jsonify(error="Employee not found"), 404
    shift_id = execute("INSERT INTO shifts(organisation_id,site_id,employee_id,shift_date,start_time,end_time,break_minutes) VALUES(?,?,?,?,?,?,?)", (u["organisation_id"], s["id"], employee_id, d["shift_date"], d["start_time"], d["end_time"], break_minutes))
    audit("Created", "shift", shift_id, employee["name"])
    return jsonify(ok=True, id=shift_id)


@app.get("/api/payroll")
@login_required
def get_payroll():
    u, s = user(), current_site()
    runs = q("SELECT * FROM payroll_runs WHERE organisation_id=? AND site_id=? ORDER BY period_end DESC", (u["organisation_id"], s["id"]))
    employees = q("SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name", (u["organisation_id"], s["id"]))
    return jsonify(runs=[dict(x) for x in runs], employees=[dict(x) for x in employees])


@app.post("/api/payroll")
@login_required
@manager_required
def add_payroll():
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        gross = float(d.get("gross_pay", 0))
        employer = float(d.get("employer_costs", 0))
        deductions = float(d.get("deductions", 0))
    except Exception:
        return jsonify(error="Invalid payroll values"), 400
    if gross <= 0:
        return jsonify(error="Gross pay must be greater than zero"), 400
    if employer < 0 or deductions < 0 or deductions > gross:
        return jsonify(error="Invalid payroll values"), 400
    if not d.get("period_start") or not d.get("period_end"):
        return jsonify(error="Payroll period is required"), 400
    run_id = execute("""INSERT INTO payroll_runs(organisation_id,site_id,period_start,period_end,gross_pay,employer_costs,deductions,net_pay,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                     (u["organisation_id"], s["id"], d["period_start"], d["period_end"], gross, employer, deductions, gross-deductions, now()))
    audit("Created", "payroll", run_id)
    return jsonify(ok=True, id=run_id)


@app.post("/api/payroll/<int:rid>/approve")
@login_required
@manager_required
def approve_payroll(rid):
    u, s = user(), current_site()
    payroll_run = q("SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?", (rid, u["organisation_id"], s["id"]), True)
    if not payroll_run:
        return jsonify(error="Payroll run not found"), 404
    if payroll_run["status"] != "Draft":
        return jsonify(error="Payroll run is not in draft status"), 400
    execute("UPDATE payroll_runs SET status='Approved',approved_by=?,approved_at=? WHERE id=?", (u["id"], now(), rid))
    execute("""INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u["organisation_id"], s["id"], "Payroll", "Staff payroll", f"PAY-{rid}", payroll_run["net_pay"], date.today().isoformat(), "Scheduled", "Bank transfer", rid, u["id"], now()))
    audit("Approved", "payroll", rid)
    return jsonify(ok=True)


@app.post("/api/payroll/<int:rid>/pay")
@login_required
@manager_required
def pay_payroll(rid):
    u, s = user(), current_site()
    payroll_run = q("SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?", (rid, u["organisation_id"], s["id"]), True)
    if not payroll_run or payroll_run["status"] != "Approved":
        return jsonify(error="Payroll must be approved first"), 400
    payment_time = now()
    execute("UPDATE payroll_runs SET status='Paid',paid_at=? WHERE id=?", (payment_time, rid))
    execute("UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Payroll'", (payment_time, rid))
    audit("Recorded paid", "payroll", rid)
    return jsonify(ok=True)


@app.get("/api/stock")
@login_required
def get_stock():
    u, s = user(), current_site()
    result = q("SELECT * FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name", (u["organisation_id"], s["id"]))
    return jsonify(stock=[dict(x) for x in result])


@app.post("/api/stock")
@login_required
@manager_required
def add_stock():
    u, s = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Stock item name required"), 400
    try:
        on_hand = float(d.get("on_hand", 0))
        par = float(d.get("par_level", 0))
        cost = float(d.get("unit_cost", 0))
    except Exception:
        return jsonify(error="Invalid stock values"), 400
    if on_hand < 0 or par < 0 or cost < 0:
        return jsonify(error="Stock values cannot be negative"), 400
    item_id = execute("""INSERT INTO stock_items(organisation_id,site_id,name,category,unit,on_hand,par_level,unit_cost,supplier)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (u["organisation_id"], s["id"], name, d.get("category") or "Food", d.get("unit") or "unit", on_hand, par, cost, d.get("supplier") or ""))
    audit("Created", "stock_item", item_id, name)
    return jsonify(ok=True, id=item_id)


@app.post("/api/stock/<int:iid>/movement")
@login_required
@manager_required
def stock_move(iid):
    u, s = user(), current_site()
    d = request.get_json() or {}
    try:
        quantity = float(d.get("quantity", 0))
    except Exception:
        return jsonify(error="Invalid quantity"), 400
    if quantity == 0:
        return jsonify(error="Quantity cannot be zero"), 400
    item = q("SELECT * FROM stock_items WHERE id=? AND organisation_id=? AND site_id=?", (iid, u["organisation_id"], s["id"]), True)
    if not item:
        return jsonify(error="Stock item not found"), 404
    new_quantity = max(0, item["on_hand"] + quantity)
    execute("UPDATE stock_items SET on_hand=? WHERE id=?", (new_quantity, iid))
    execute("""INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (u["organisation_id"], s["id"], iid, quantity, d.get("movement_type") or "Adjustment", d.get("note") or "", now()))
    audit("Updated", "stock_item", iid, f"Movement {quantity}")
    return jsonify(ok=True, on_hand=new_quantity)


@app.get("/api/suppliers")
@login_required
def get_suppliers():
    u = user()
    result = q("SELECT * FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name", (u["organisation_id"],))
    return jsonify(suppliers=[dict(x) for x in result])


@app.post("/api/suppliers")
@login_required
@manager_required
def add_supplier():
    u = user()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Supplier name required"), 400
    try:
        terms = int(d.get("payment_terms", 30))
    except Exception:
        return jsonify(error="Invalid payment terms"), 400
    if terms < 0:
        return jsonify(error="Payment terms cannot be negative"), 400
    try:
        supplier_id = execute("""INSERT INTO suppliers(organisation_id,name,contact,email,phone,payment_terms)
                                VALUES(?,?,?,?,?,?)""",
                             (u["organisation_id"], name, d.get("contact") or "", d.get("email") or "", d.get("phone") or "", terms))
    except sqlite3.IntegrityError:
        return jsonify(error="Supplier already exists"), 400
    audit("Created", "supplier", supplier_id, name)
    return jsonify(ok=True, id=supplier_id)


@app.get("/api/menu")
@login_required
def get_menu():
    u, s = user(), current_site()
    result = q("SELECT * FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY category,name", (u["organisation_id"], s["id"]))
    return jsonify(menu=[dict(x) for x in result])


@app.post("/api/menu")
@login_required
@manager_required
def add_menu():
    u, s = user(), current_site()
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Menu item name required"), 400
    try:
        price = float(d.get("selling_price", 0))
        cost = float(d.get("recipe_cost", 0))
    except Exception:
        return jsonify(error="Invalid prices"), 400
    if price <= 0 or cost < 0:
        return jsonify(error="Invalid menu prices"), 400
    menu_id = execute("""INSERT INTO menu_items(organisation_id,site_id,name,category,selling_price,recipe_cost)
                         VALUES(?,?,?,?,?,?)""",
                      (u["organisation_id"], s["id"], name, d.get("category") or "Main", price, cost))
    audit("Created", "menu_item", menu_id, name)
    return jsonify(ok=True, id=menu_id)


@app.get("/api/budget")
@login_required
def get_budget():
    u, s = user(), current_site()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    budget = q("SELECT * FROM budgets WHERE organisation_id=? AND site_id=? AND month=?", (u["organisation_id"], s["id"], month), True)
    return jsonify(budget=dict(budget) if budget else None, month=month)


@app.post("/api/budget")
@login_required
@manager_required
def save_budget():
    u, s = user(), current_site()
    d = request.get_json() or {}
    month = d.get("month") or date.today().strftime("%Y-%m")
    try:
        values = [float(d.get(k, 0)) for k in ("revenue", "food_cost", "drink_cost", "labour", "overheads")]
    except Exception:
        return jsonify(error="Invalid budget values"), 400
    if any(v < 0 for v in values):
        return jsonify(error="Budget values cannot be negative"), 400
    revenue, food_cost, drink_cost, labour, overheads = values
    c = conn()
    c.execute("""INSERT INTO budgets(organisation_id,site_id,month,revenue,food_cost,drink_cost,labour,overheads)
                 VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(site_id,month) DO UPDATE SET
                 revenue=excluded.revenue,food_cost=excluded.food_cost,drink_cost=excluded.drink_cost,
                 labour=excluded.labour,overheads=excluded.overheads""",
              (u["organisation_id"], s["id"], month, revenue, food_cost, drink_cost, labour, overheads))
    c.commit()
    c.close()
    audit("Updated", "budget", None, month)
    return jsonify(ok=True)


@app.get("/api/audit")
@login_required
@manager_required
def audit_log():
    u = user()
    result = q("SELECT * FROM audit_log WHERE organisation_id=? ORDER BY id DESC LIMIT 100", (u["organisation_id"],))
    return jsonify(log=[dict(x) for x in result])


@app.get("/api/health")
@app.get("/health")
def health():
    return jsonify(status="ok", service="OrderFlow")


@app.get("/api/weather")
@login_required
def weather():
    return jsonify(current={"description": "Weather unavailable", "temperature": 0, "code": 0}, daily=[])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
