import os
import sqlite3
import calendar
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================
# ORDERFLOW
# Hospitality Management Platform
# ============================================================

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "orderflow.db")

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_IN_RENDER"
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)


# ============================================================
# DATABASE HELPERS
# ============================================================

def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def rows(sql, args=()):
    connection = db()
    result = connection.execute(sql, args).fetchall()
    connection.close()
    return result


def row(sql, args=()):
    result = rows(sql, args)
    return result[0] if result else None


def run(sql, args=()):
    connection = db()
    cursor = connection.execute(sql, args)
    connection.commit()
    last_id = cursor.lastrowid
    connection.close()
    return last_id


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def today():
    return date.today().isoformat()


# ============================================================
# DATABASE INITIALISATION
# ============================================================

def init_db():

    connection = db()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS organisations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            business_type TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'GBP',
            vat_registered INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            address TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE (organisation_id, email),
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            sale_date TEXT NOT NULL,
            category TEXT NOT NULL,
            net REAL DEFAULT 0,
            vat REAL DEFAULT 0,
            gross REAL DEFAULT 0,
            source TEXT DEFAULT 'Manual',
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE,
            FOREIGN KEY (site_id)
                REFERENCES sites(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            expense_date TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            supplier TEXT DEFAULT '',
            net REAL DEFAULT 0,
            vat REAL DEFAULT 0,
            gross REAL DEFAULT 0,
            status TEXT DEFAULT 'Posted',
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE,
            FOREIGN KEY (site_id)
                REFERENCES sites(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS invoices (
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
            status TEXT DEFAULT 'Awaiting approval',
            approved_by INTEGER,
            approved_at TEXT,
            paid_at TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE,
            FOREIGN KEY (site_id)
                REFERENCES sites(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            payment_type TEXT NOT NULL,
            payee TEXT NOT NULL,
            reference TEXT DEFAULT '',
            amount REAL NOT NULL,
            payment_date TEXT NOT NULL,
            status TEXT DEFAULT 'Scheduled',
            method TEXT DEFAULT 'Bank transfer',
            source_id INTEGER,
            approved_by INTEGER,
            approved_at TEXT,
            paid_at TEXT,
            external_reference TEXT DEFAULT '',
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE,
            FOREIGN KEY (site_id)
                REFERENCES sites(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT DEFAULT '',
            department TEXT DEFAULT 'FOH',
            job_title TEXT DEFAULT '',
            pay_type TEXT DEFAULT 'Hourly',
            pay_rate REAL DEFAULT 0,
            ni_rate REAL DEFAULT 0,
            pension_rate REAL DEFAULT 0,
            holiday_allowance REAL DEFAULT 28,
            active INTEGER DEFAULT 1,
            FOREIGN KEY (organisation_id)
                REFERENCES organisations(id)
                ON DELETE CASCADE,
            FOREIGN KEY (site_id)
                REFERENCES sites(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            break_minutes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Scheduled',
            notes TEXT DEFAULT '',
            FOREIGN KEY (employee_id)
                REFERENCES employees(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payroll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            gross_pay REAL NOT NULL,
            employer_costs REAL DEFAULT 0,
            deductions REAL DEFAULT 0,
            net_pay REAL NOT NULL,
            status TEXT DEFAULT 'Draft',
            created_at TEXT NOT NULL,
            approved_by INTEGER,
            approved_at TEXT,
            paid_at TEXT
        );

        CREATE TABLE IF NOT EXISTS stock_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit TEXT NOT NULL,
            on_hand REAL DEFAULT 0,
            par_level REAL DEFAULT 0,
            unit_cost REAL DEFAULT 0,
            supplier TEXT DEFAULT '',
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            stock_item_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            movement_type TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (stock_item_id)
                REFERENCES stock_items(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            contact TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            payment_terms INTEGER DEFAULT 30,
            active INTEGER DEFAULT 1,
            UNIQUE (organisation_id, name)
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            supplier_id INTEGER NOT NULL,
            order_date TEXT NOT NULL,
            delivery_date TEXT,
            total REAL DEFAULT 0,
            status TEXT DEFAULT 'Review',
            items TEXT DEFAULT '',
            FOREIGN KEY (supplier_id)
                REFERENCES suppliers(id)
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            selling_price REAL NOT NULL,
            recipe_cost REAL DEFAULT 0,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            revenue REAL DEFAULT 0,
            food_cost REAL DEFAULT 0,
            drink_cost REAL DEFAULT 0,
            labour REAL DEFAULT 0,
            overheads REAL DEFAULT 0,
            UNIQUE (site_id, month)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            user_id INTEGER,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER,
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            event_date TEXT NOT NULL,
            event_time TEXT DEFAULT '',
            title TEXT NOT NULL,
            detail TEXT DEFAULT ''
        );
        """
    )

    connection.commit()
    connection.close()


init_db()


# ============================================================
# USER / SITE HELPERS
# ============================================================

def me():

    if not session.get("user_id"):
        return None

    return row(
        """
        SELECT *
        FROM users
        WHERE id = ?
        AND active = 1
        """,
        (session.get("user_id"),)
    )


def site():

    user = me()

    if not user:
        return None

    selected_site = session.get("site_id")

    if selected_site:

        current = row(
            """
            SELECT *
            FROM sites
            WHERE id = ?
            AND organisation_id = ?
            AND active = 1
            """,
            (
                selected_site,
                user["organisation_id"]
            )
        )

        if current:
            return current

    current = row(
        """
        SELECT *
        FROM sites
        WHERE organisation_id = ?
        AND active = 1
        ORDER BY id
        LIMIT 1
        """,
        (user["organisation_id"],)
    )

    if current:
        session["site_id"] = current["id"]

    return current


# ============================================================
# AUDIT
# ============================================================

def audit(action, entity, entity_id=None, detail=""):

    user = me()

    if not user:
        return

    run(
        """
        INSERT INTO audit_log
        (
            organisation_id,
            user_id,
            action,
            entity,
            entity_id,
            detail,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            user["id"],
            action,
            entity,
            entity_id,
            detail,
            now()
        )
    )


# ============================================================
# ACCESS CONTROL
# ============================================================

def auth(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not me():
            return redirect("/login")

        return function(*args, **kwargs)

    return wrapper


def manager(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        user = me()

        if not user:
            return jsonify(error="Not authenticated"), 401

        allowed = (
            "Manager",
            "Owner",
            "Admin",
            "Finance"
        )

        if user["role"] not in allowed:
            return jsonify(
                error="Manager permission required"
            ), 403

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# FINANCIAL HELPERS
# ============================================================

def money_sum(sql, args=()):

    result = row(sql, args)

    if not result:
        return 0

    return float(result["n"] or 0)


def month_bounds(month):

    year, month_number = map(
        int,
        month.split("-")
    )

    last_day = calendar.monthrange(
        year,
        month_number
    )[1]

    return (
        f"{month}-01",
        f"{month}-{last_day:02d}"
    )


def financial(month, user, current_site):

    start, end = month_bounds(month)

    args = (
        user["organisation_id"],
        current_site["id"],
        start,
        end
    )

    revenue = money_sum(
        """
        SELECT SUM(net) n
        FROM sales
        WHERE organisation_id = ?
        AND site_id = ?
        AND sale_date BETWEEN ? AND ?
        """,
        args
    )

    food = money_sum(
        """
        SELECT SUM(net) n
        FROM expenses
        WHERE organisation_id = ?
        AND site_id = ?
        AND expense_date BETWEEN ? AND ?
        AND category = 'Food'
        """,
        args
    )

    drink = money_sum(
        """
        SELECT SUM(net) n
        FROM expenses
        WHERE organisation_id = ?
        AND site_id = ?
        AND expense_date BETWEEN ? AND ?
        AND category = 'Drink'
        """,
        args
    )

    cogs = food + drink

    labour = money_sum(
        """
        SELECT
            SUM(
                (
                    julianday(
                        shift_date || ' ' || end_time
                    )
                    -
                    julianday(
                        shift_date || ' ' || start_time
                    )
                ) * 24 * e.pay_rate
            ) n
        FROM shifts sh
        JOIN employees e
            ON e.id = sh.employee_id
        WHERE sh.organisation_id = ?
        AND sh.site_id = ?
        AND sh.shift_date BETWEEN ? AND ?
        """,
        args
    )

    overheads = money_sum(
        """
        SELECT SUM(net) n
        FROM expenses
        WHERE organisation_id = ?
        AND site_id = ?
        AND expense_date BETWEEN ? AND ?
        AND category NOT IN ('Food', 'Drink')
        """,
        args
    )

    gross_profit = revenue - cogs

    ebitda = (
        gross_profit
        - labour
        - overheads
    )

    return {
        "revenue": round(revenue, 2),
        "food_cost": round(food, 2),
        "drink_cost": round(drink, 2),
        "cogs": round(cogs, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_margin": round(
            gross_profit / revenue * 100,
            1
        ) if revenue else 0,
        "labour": round(labour, 2),
        "labour_pct": round(
            labour / revenue * 100,
            1
        ) if revenue else 0,
        "overheads": round(overheads, 2),
        "ebitda": round(ebitda, 2),
        "ebitda_margin": round(
            ebitda / revenue * 100,
            1
        ) if revenue else 0
    }


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/")
def home():

    if me():
        return redirect("/app")

    return redirect("/login")


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        user = row(
            """
            SELECT *
            FROM users
            WHERE lower(email) = ?
            AND active = 1
            """,
            (email,)
        )

        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            session["user_id"] = user["id"]
            session["site_id"] = None

            return redirect("/app")

        return render_template(
            "login.html",
            error="Invalid email or password"
        )

    return render_template("login.html")


# ============================================================
# LOGOUT
# ============================================================

@app.post("/logout")
def logout():

    session.clear()

    return redirect("/login")


# ============================================================
# BUSINESS ONBOARDING
# ============================================================

@app.route(
    "/onboarding",
    methods=["GET", "POST"]
)
def onboarding():

    if request.method == "POST":

        data = request.form

        business_name = data.get(
            "business_name",
            ""
        ).strip()

        site_name = data.get(
            "site_name",
            ""
        ).strip()

        owner_name = (
            data.get("owner_name")
            or data.get("name", "")
        ).strip()

        email = data.get(
            "email",
            ""
        ).strip().lower()

        password = data.get(
            "password",
            ""
        )

        if not all(
            [
                business_name,
                site_name,
                owner_name,
                email,
                password
            ]
        ):

            return render_template(
                "onboarding.html",
                error="Please complete every field"
            )

        connection = db()

        try:

            cursor = connection.execute(
                """
                INSERT INTO organisations
                (
                    name,
                    business_type,
                    currency,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    business_name,
                    data.get(
                        "business_type"
                    ) or "Hospitality",
                    "GBP",
                    now()
                )
            )

            organisation_id = cursor.lastrowid

            cursor = connection.execute(
                """
                INSERT INTO sites
                (
                    organisation_id,
                    name,
                    address,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    organisation_id,
                    site_name,
                    data.get("address") or "",
                    now()
                )
            )

            site_id = cursor.lastrowid

            connection.execute(
                """
                INSERT INTO users
                (
                    organisation_id,
                    name,
                    email,
                    password_hash,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    organisation_id,
                    owner_name,
                    email,
                    generate_password_hash(
                        password
                    ),
                    "Manager",
                    now()
                )
            )

            connection.commit()

        except sqlite3.IntegrityError:

            connection.rollback()

            return render_template(
                "onboarding.html",
                error="That email is already registered"
            )

        finally:

            connection.close()

        return redirect("/login")

    return render_template(
        "onboarding.html"
    )


# ============================================================
# MAIN APPLICATION
# ============================================================

@app.get("/app")
@auth
def application():

    user = me()
    current_site = site()

    organisation = row(
        """
        SELECT *
        FROM organisations
        WHERE id = ?
        """,
        (user["organisation_id"],)
    )

    return render_template(
        "app.html",
        user=dict(user),
        site=dict(current_site),
        organisation=dict(organisation),
        venue=organisation["name"]
    )


# ============================================================
# SITE SELECTION
# ============================================================

@app.post("/api/site/select")
@auth
def select_site():

    user = me()

    data = request.get_json() or {}

    try:
        site_id = int(
            data.get("site_id", 0)
        )
    except:
        return jsonify(
            error="Invalid site"
        ), 400

    selected = row(
        """
        SELECT *
        FROM sites
        WHERE id = ?
        AND organisation_id = ?
        AND active = 1
        """,
        (
            site_id,
            user["organisation_id"]
        )
    )

    if not selected:
        return jsonify(
            error="Site not found"
        ), 404

    session["site_id"] = site_id

    return jsonify(ok=True)


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/api/dashboard")
@auth
def dashboard():

    user = me()
    current_site = site()

    month = date.today().strftime(
        "%Y-%m"
    )

    financials = financial(
        month,
        user,
        current_site
    )

    events = rows(
        """
        SELECT *
        FROM events
        WHERE organisation_id = ?
        AND site_id = ?
        ORDER BY event_date, event_time
        LIMIT 8
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    order_count = money_sum(
        """
        SELECT COUNT(*) n
        FROM purchase_orders
        WHERE organisation_id = ?
        AND site_id = ?
        AND status != 'Received'
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    return jsonify(
        sales=financials["revenue"],
        gross_margin=financials["gross_margin"],
        ebitda=financials["ebitda"],
        ebitda_margin=financials[
            "ebitda_margin"
        ],
        labour=financials["labour"],
        labour_pct=financials[
            "labour_pct"
        ],
        order_count=int(order_count),
        events=[
            dict(event)
            for event in events
        ]
    )


# ============================================================
# SALES
# ============================================================

@app.post("/api/sales")
@auth
@manager
def add_sale():

    user = me()
    current_site = site()
    data = request.get_json() or {}

    try:
        gross = float(
            data.get("gross", 0) or 0
        )

        vat = float(
            data.get("vat", 0) or 0
        )

        net = float(
            data.get(
                "net",
                gross - vat
            ) or 0
        )

    except:
        return jsonify(
            error="Invalid sales values"
        ), 400

    if gross <= 0:
        return jsonify(
            error="Sale must be greater than zero"
        ), 400

    sale_id = run(
        """
        INSERT INTO sales
        (
            organisation_id,
            site_id,
            sale_date,
            category,
            net,
            vat,
            gross,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            data.get(
                "sale_date"
            ) or today(),
            data.get(
                "category"
            ) or "Food & Drink",
            net,
            vat,
            gross,
            data.get(
                "source"
            ) or "Manual"
        )
    )

    audit(
        "Created",
        "sale",
        sale_id
    )

    return jsonify(
        ok=True,
        id=sale_id
    )


# ============================================================
# FINANCE
# ============================================================

@app.get("/api/finance")
@auth
def finance_api():

    user = me()
    current_site = site()

    month = request.args.get(
        "month"
    ) or date.today().strftime(
        "%Y-%m"
    )

    financials = financial(
        month,
        user,
        current_site
    )

    budget = row(
        """
        SELECT *
        FROM budgets
        WHERE organisation_id = ?
        AND site_id = ?
        AND month = ?
        """,
        (
            user["organisation_id"],
            current_site["id"],
            month
        )
    )

    breakdown = rows(
        """
        SELECT
            category,
            SUM(net) n
        FROM expenses
        WHERE organisation_id = ?
        AND site_id = ?
        AND expense_date LIKE ?
        GROUP BY category
        ORDER BY n DESC
        """,
        (
            user["organisation_id"],
            current_site["id"],
            month + "%"
        )
    )

    month_label = datetime.strptime(
        month + "-01",
        "%Y-%m-%d"
    ).strftime("%B %Y")

    return jsonify(
        **financials,
        month_label=month_label,
        expense_breakdown={
            item["category"]:
            item["n"]
            for item in breakdown
        },
        budget=dict(budget)
        if budget
        else {}
    )


# ============================================================
# PROFIT & LOSS
# ============================================================

@app.get("/api/finance/pnl")
@auth
def pnl():

    user = me()
    current_site = site()

    month = request.args.get(
        "month"
    ) or date.today().strftime(
        "%Y-%m"
    )

    financials = financial(
        month,
        user,
        current_site
    )

    budget = row(
        """
        SELECT *
        FROM budgets
        WHERE organisation_id = ?
        AND site_id = ?
        AND month = ?
        """,
        (
            user["organisation_id"],
            current_site["id"],
            month
        )
    )

    budget_data = (
        dict(budget)
        if budget
        else {}
    )

    budget_gp = (
        budget_data.get(
            "revenue",
            0
        )
        - budget_data.get(
            "food_cost",
            0
        )
        - budget_data.get(
            "drink_cost",
            0
        )
    )

    budget_ebitda = (
        budget_gp
        - budget_data.get(
            "labour",
            0
        )
        - budget_data.get(
            "overheads",
            0
        )
    )

    output = [

        [
            "Revenue",
            financials["revenue"],
            budget_data.get(
                "revenue",
                0
            )
        ],

        [
            "Food cost",
            financials["food_cost"],
            budget_data.get(
                "food_cost",
                0
            )
        ],

        [
            "Drink cost",
            financials["drink_cost"],
            budget_data.get(
                "drink_cost",
                0
            )
        ],

        [
            "Gross profit",
            financials["gross_profit"],
            budget_gp
        ],

        [
            "Labour",
            financials["labour"],
            budget_data.get(
                "labour",
                0
            )
        ],

        [
            "Overheads",
            financials["overheads"],
            budget_data.get(
                "overheads",
                0
            )
        ],

        [
            "EBITDA",
            financials["ebitda"],
            budget_ebitda
        ]
    ]

    return jsonify(
        rows=output
    )


# ============================================================
# CASH FLOW
# ============================================================

@app.get("/api/finance/cashflow")
@auth
def cashflow():

    user = me()
    current_site = site()

    opening = 0

    expected_inflow = money_sum(
        """
        SELECT SUM(gross) n
        FROM sales
        WHERE organisation_id = ?
        AND site_id = ?
        AND sale_date = ?
        """,
        (
            user["organisation_id"],
            current_site["id"],
            today()
        )
    )

    upcoming = money_sum(
        """
        SELECT SUM(amount) n
        FROM payments
        WHERE organisation_id = ?
        AND site_id = ?
        AND status = 'Scheduled'
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    projected = (
        opening
        + expected_inflow
        - upcoming
    )

    return jsonify(
        opening=opening,
        expected_inflow=expected_inflow,
        upcoming_payments=upcoming,
        projected_closing=projected,
        lines=[
            [
                "Expected receipts",
                expected_inflow
            ],
            [
                "Scheduled payments",
                -upcoming
            ],
            [
                "Projected closing",
                projected
            ]
        ]
    )


# ============================================================
# OUTGOING COSTS
# ============================================================

@app.post("/api/expenses")
@auth
@manager
def add_expense():

    user = me()
    current_site = site()
    data = request.get_json() or {}

    try:

        gross = float(
            data.get(
                "amount",
                0
            ) or 0
        )

        vat = float(
            data.get(
                "vat",
                0
            ) or 0
        )

    except:

        return jsonify(
            error="Invalid amount"
        ), 400

    net = gross - vat

    if gross <= 0:

        return jsonify(
            error="Amount must be greater than zero"
        ), 400

    expense_id = run(
        """
        INSERT INTO expenses
        (
            organisation_id,
            site_id,
            expense_date,
            category,
            description,
            supplier,
            net,
            vat,
            gross
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            data.get(
                "expense_date"
            ) or today(),
            data.get(
                "category"
            ) or "Other",
            data.get(
                "description"
            ) or "Outgoing cost",
            data.get(
                "supplier"
            ) or "",
            net,
            vat,
            gross
        )
    )

    audit(
        "Created",
        "expense",
        expense_id
    )

    return jsonify(
        ok=True,
        id=expense_id
    )


# ============================================================
# INVOICES
# ============================================================

@app.get("/api/invoices")
@auth
def invoices():

    user = me()
    current_site = site()

    result = rows(
        """
        SELECT *
        FROM invoices
        WHERE organisation_id = ?
        AND site_id = ?
        ORDER BY due_date DESC, id DESC
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    return jsonify(
        invoices=[
            dict(item)
            for item in result
        ]
    )


@app.post("/api/invoices")
@auth
@manager
def add_invoice():

    user = me()
    current_site = site()
    data = request.get_json() or {}

    try:

        net = float(
            data.get(
                "net_amount",
                0
            ) or 0
        )

        vat = float(
            data.get(
                "vat",
                0
            ) or 0
        )

    except:

        return jsonify(
            error="Invalid invoice values"
        ), 400

    if (
        not data.get("supplier")
        or not data.get("invoice_number")
        or net < 0
    ):

        return jsonify(
            error="Supplier, invoice number and valid net amount required"
        ), 400

    invoice_id = run(
        """
        INSERT INTO invoices
        (
            organisation_id,
            site_id,
            supplier,
            invoice_number,
            invoice_date,
            due_date,
            category,
            net,
            vat,
            gross,
            notes,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            data["supplier"],
            data["invoice_number"],
            data.get(
                "invoice_date"
            ) or today(),
            data.get(
                "due_date"
            ) or today(),
            data.get(
                "category"
            ) or "Other",
            net,
            vat,
            net + vat,
            data.get(
                "notes"
            ) or "",
            now()
        )
    )

    audit(
        "Created",
        "invoice",
        invoice_id
    )

    return jsonify(
        ok=True,
        id=invoice_id
    )


@app.post("/api/invoices/<int:invoice_id>/approve")
@auth
@manager
def approve_invoice(invoice_id):

    user = me()
    current_site = site()

    invoice = row(
        """
        SELECT *
        FROM invoices
        WHERE id = ?
        AND organisation_id = ?
        AND site_id = ?
        """,
        (
            invoice_id,
            user["organisation_id"],
            current_site["id"]
        )
    )

    if (
        not invoice
        or invoice["status"]
        != "Awaiting approval"
    ):

        return jsonify(
            error="Invoice cannot be approved"
        ), 400

    run(
        """
        UPDATE invoices
        SET
            status = 'Approved',
            approved_by = ?,
            approved_at = ?
        WHERE id = ?
        """,
        (
            user["id"],
            now(),
            invoice_id
        )
    )

    run(
        """
        INSERT INTO payments
        (
            organisation_id,
            site_id,
            payment_type,
            payee,
            reference,
            amount,
            payment_date,
            status,
            method,
            source_id,
            approved_by,
            approved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            "Supplier",
            invoice["supplier"],
            invoice["invoice_number"],
            invoice["gross"],
            invoice["due_date"],
            "Scheduled",
            "Bank transfer",
            invoice_id,
            user["id"],
            now()
        )
    )

    audit(
        "Approved",
        "invoice",
        invoice_id
    )

    return jsonify(ok=True)


@app.post("/api/invoices/<int:invoice_id>/pay")
@auth
@manager
def pay_invoice(invoice_id):

    user = me()
    current_site = site()

    invoice = row(
        """
        SELECT *
        FROM invoices
        WHERE id = ?
        AND organisation_id = ?
        AND site_id = ?
        """,
        (
            invoice_id,
            user["organisation_id"],
            current_site["id"]
        )
    )

    if (
        not invoice
        or invoice["status"] != "Approved"
    ):

        return jsonify(
            error="Invoice must be approved first"
        ), 400

    run(
        """
        UPDATE invoices
        SET
            status = 'Paid',
            paid_at = ?
        WHERE id = ?
        """,
        (
            now(),
            invoice_id
        )
    )

    run(
        """
        UPDATE payments
        SET
            status = 'Paid',
            paid_at = ?
        WHERE source_id = ?
        AND payment_type = 'Supplier'
        """,
        (
            now(),
            invoice_id
        )
    )

    audit(
        "Recorded paid",
        "invoice",
        invoice_id
    )

    return jsonify(ok=True)


# ============================================================
# PAYMENTS
# ============================================================

@app.get("/api/payments")
@auth
def payments():

    user = me()
    current_site = site()

    result = rows(
        """
        SELECT
            payment_type,
            payee,
            amount,
            payment_date scheduled_date,
            method payment_method,
            status
        FROM payments
        WHERE organisation_id = ?
        AND site_id = ?
        ORDER BY payment_date DESC, id DESC
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    return jsonify(
        payments=[
            dict(item)
            for item in result
        ]
    )


# ============================================================
# PAYROLL
# ============================================================

@app.get("/api/payroll")
@auth
@manager
def payroll():

    user = me()
    current_site = site()

    week = request.args.get(
        "week"
    ) or today()

    selected = datetime.strptime(
        week,
        "%Y-%m-%d"
    ).date()

    monday = selected - timedelta(
        days=selected.weekday()
    )

    sunday = monday + timedelta(
        days=6
    )

    employees = rows(
        """
        SELECT *
        FROM employees
        WHERE organisation_id = ?
        AND site_id = ?
        AND active = 1
        ORDER BY name
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    shifts = rows(
        """
        SELECT
            sh.*,
            e.name employee_name,
            e.department,
            e.pay_rate
        FROM shifts sh
        JOIN employees e
            ON e.id = sh.employee_id
        WHERE sh.organisation_id = ?
        AND sh.site_id = ?
        AND shift_date BETWEEN ? AND ?
        ORDER BY shift_date, start_time
        """,
        (
            user["organisation_id"],
            current_site["id"],
            monday.isoformat(),
            sunday.isoformat()
        )
    )

    total_hours = 0
    total_cost = 0

    shift_output = []

    for shift in shifts:

        try:

            start = datetime.strptime(
                shift["start_time"],
                "%H:%M"
            )

            end = datetime.strptime(
                shift["end_time"],
                "%H:%M"
            )

            hours = (
                end - start
            ).seconds / 3600

            hours -= (
                shift["break_minutes"]
                / 60
            )

            if hours < 0:
                hours = 0

        except:

            hours = 0

        cost = (
            hours
            * shift["pay_rate"]
        )

        total_hours += hours
        total_cost += cost

        item = dict(shift)
        item["cost"] = round(
            cost,
            2
        )

        shift_output.append(item)

    forecast_sales = money_sum(
        """
        SELECT SUM(net) n
        FROM sales
        WHERE organisation_id = ?
        AND site_id = ?
        AND sale_date BETWEEN ? AND ?
        """,
        (
            user["organisation_id"],
            current_site["id"],
            monday.isoformat(),
            sunday.isoformat()
        )
    )

    return jsonify(
        week_start=monday.isoformat(),
        week_end=sunday.isoformat(),
        employees=[
            dict(item)
            for item in employees
        ],
        shifts=shift_output,
        summary={
            "forecast_sales":
                forecast_sales,
            "labour_cost":
                round(
                    total_cost,
                    2
                ),
            "labour_pct":
                round(
                    total_cost
                    / forecast_sales
                    * 100,
                    1
                )
                if forecast_sales
                else 0,
            "allotted_hours":
                round(
                    total_hours,
                    1
                )
        }
    )


@app.post("/api/payroll/shifts")
@auth
@manager
def add_shift():

    user = me()
    current_site = site()
    data = request.get_json() or {}

    try:

        employee_id = int(
            data.get(
                "employee_id"
            )
        )

        break_minutes = int(
            data.get(
                "break_minutes",
                0
            ) or 0
        )

    except:

        return jsonify(
            error="Invalid shift"
        ), 400

    if not all(
        [
            data.get("shift_date"),
            data.get("start_time"),
            data.get("end_time")
        ]
    ):

        return jsonify(
            error="Complete shift details"
        ), 400

    shift_id = run(
        """
        INSERT INTO shifts
        (
            organisation_id,
            site_id,
            employee_id,
            shift_date,
            start_time,
            end_time,
            break_minutes,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            employee_id,
            data["shift_date"],
            data["start_time"],
            data["end_time"],
            break_minutes,
            data.get(
                "notes"
            ) or ""
        )
    )

    audit(
        "Created",
        "shift",
        shift_id
    )

    return jsonify(
        ok=True,
        id=shift_id
    )


# ============================================================
# PAYROLL RUNS
# ============================================================

@app.get("/api/payroll/runs")
@auth
@manager
def payroll_runs():

    user = me()
    current_site = site()

    result = rows(
        """
        SELECT *
        FROM payroll_runs
        WHERE organisation_id = ?
        AND site_id = ?
        ORDER BY period_end DESC, id DESC
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    return jsonify(
        runs=[
            dict(item)
            for item in result
        ]
    )


@app.post("/api/payroll/runs")
@auth
@manager
def add_payrun():

    user = me()
    current_site = site()
    data = request.get_json() or {}

    try:

        gross = float(
            data.get(
                "gross_pay",
                0
            ) or 0
        )

        deductions = float(
            data.get(
                "deductions",
                0
            ) or 0
        )

        employer_costs = float(
            data.get(
                "employer_costs",
                0
            ) or 0
        )

    except:

        return jsonify(
            error="Invalid payroll values"
        ), 400

    if (
        gross <= 0
        or deductions < 0
        or deductions > gross
    ):

        return jsonify(
            error="Invalid payroll values"
        ), 400

    payroll_id = run(
        """
        INSERT INTO payroll_runs
        (
            organisation_id,
            site_id,
            period_start,
            period_end,
            gross_pay,
            employer_costs,
            deductions,
            net_pay,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            data["period_start"],
            data["period_end"],
            gross,
            employer_costs,
            deductions,
            gross - deductions,
            now()
        )
    )

    audit(
        "Created",
        "payroll",
        payroll_id
    )

    return jsonify(
        ok=True,
        id=payroll_id
    )


@app.post("/api/payroll/runs/<int:run_id>/approve")
@auth
@manager
def approve_payrun(run_id):

    user = me()
    current_site = site()

    payroll_run = row(
        """
        SELECT *
        FROM payroll_runs
        WHERE id = ?
        AND organisation_id = ?
        AND site_id = ?
        """,
        (
            run_id,
            user["organisation_id"],
            current_site["id"]
        )
    )

    if (
        not payroll_run
        or payroll_run["status"]
        != "Draft"
    ):

        return jsonify(
            error="Payroll run cannot be approved"
        ), 400

    run(
        """
        UPDATE payroll_runs
        SET
            status = 'Approved',
            approved_by = ?,
            approved_at = ?
        WHERE id = ?
        """,
        (
            user["id"],
            now(),
            run_id
        )
    )

    run(
        """
        INSERT INTO payments
        (
            organisation_id,
            site_id,
            payment_type,
            payee,
            reference,
            amount,
            payment_date,
            status,
            method,
            source_id,
            approved_by,
            approved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            "Payroll",
            "Staff payroll",
            f"PAY-{run_id}",
            payroll_run["net_pay"],
            today(),
            "Scheduled",
            "Bank transfer",
            run_id,
            user["id"],
            now()
        )
    )

    audit(
        "Approved",
        "payroll",
        run_id
    )

    return jsonify(ok=True)


@app.post("/api/payroll/runs/<int:run_id>/pay")
@auth
@manager
def pay_payrun(run_id):

    user = me()
    current_site = site()

    payroll_run = row(
        """
        SELECT *
        FROM payroll_runs
        WHERE id = ?
        AND organisation_id = ?
        AND site_id = ?
        """,
        (
            run_id,
            user["organisation_id"],
            current_site["id"]
        )
    )

    if (
        not payroll_run
        or payroll_run["status"]
        != "Approved"
    ):

        return jsonify(
            error="Payroll must be approved first"
        ), 400

    run(
        """
        UPDATE payroll_runs
        SET
            status = 'Paid',
            paid_at = ?
        WHERE id = ?
        """,
        (
            now(),
            run_id
        )
    )

    run(
        """
        UPDATE payments
        SET
            status = 'Paid',
            paid_at = ?
        WHERE source_id = ?
        AND payment_type = 'Payroll'
        """,
        (
            now(),
            run_id
        )
    )

    audit(
        "Recorded paid",
        "payroll",
        run_id
    )

    return jsonify(ok=True)


# ============================================================
# STOCK
# ============================================================

@app.get("/api/stock")
@auth
def stock():

    user = me()
    current_site = site()

    items = rows(
        """
        SELECT *
        FROM stock_items
        WHERE organisation_id = ?
        AND site_id = ?
        AND active = 1
        ORDER BY name
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    output = []

    for item in items:

        data = dict(item)

        data["days_cover"] = round(
            item["on_hand"]
            / max(
                item["par_level"],
                1
            )
            * 7,
            1
        )

        output.append(data)

    return jsonify(
        stock=output
    )


# ============================================================
# WASTE
# ============================================================

@app.post("/api/waste")
@auth
@manager
def waste():

    user = me()
    current_site = site()
    data = request.get_json() or {}

    item = row(
        """
        SELECT *
        FROM stock_items
        WHERE organisation_id = ?
        AND site_id = ?
        AND lower(name) = lower(?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            data.get(
                "item",
                ""
            )
        )
    )

    try:

        quantity = float(
            data.get(
                "quantity",
                0
            ) or 0
        )

    except:

        return jsonify(
            error="Invalid quantity"
        ), 400

    if not item:

        return jsonify(
            error="Stock item not found"
        ), 404

    if quantity <= 0:

        return jsonify(
            error="Quantity must be positive"
        ), 400

    run(
        """
        UPDATE stock_items
        SET on_hand =
            MAX(
                0,
                on_hand - ?
            )
        WHERE id = ?
        """,
        (
            quantity,
            item["id"]
        )
    )

    run(
        """
        INSERT INTO stock_movements
        (
            organisation_id,
            site_id,
            stock_item_id,
            quantity,
            movement_type,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["organisation_id"],
            current_site["id"],
            item["id"],
            -quantity,
            "Waste",
            data.get(
                "reason"
            ) or "Waste",
            now()
        )
    )

    audit(
        "Recorded",
        "waste",
        item["id"],
        str(quantity)
    )

    return jsonify(ok=True)


# ============================================================
# ORDERS
# ============================================================

@app.get("/api/orders")
@auth
def orders():

    user = me()
    current_site = site()

    result = rows(
        """
        SELECT
            po.*,
            su.name supplier
        FROM purchase_orders po
        JOIN suppliers su
            ON su.id = po.supplier_id
        WHERE po.organisation_id = ?
        AND po.site_id = ?
        ORDER BY po.order_date DESC, id DESC
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    return jsonify(
        orders=[
            dict(item)
            for item in result
        ]
    )


@app.post("/api/orders/<int:order_id>/status")
@auth
@manager
def order_status(order_id):

    user = me()
    current_site = site()

    data = request.get_json() or {}

    status = data.get(
        "status"
    )

    if status not in (
        "Review",
        "Approved",
        "Received"
    ):

        return jsonify(
            error="Invalid status"
        ), 400

    run(
        """
        UPDATE purchase_orders
        SET status = ?
        WHERE id = ?
        AND organisation_id = ?
        AND site_id = ?
        """,
        (
            status,
            order_id,
            user["organisation_id"],
            current_site["id"]
        )
    )

    audit(
        "Updated",
        "purchase_order",
        order_id,
        status
    )

    return jsonify(ok=True)


# ============================================================
# SUPPLIERS
# ============================================================

@app.get("/api/suppliers")
@auth
def suppliers():

    user = me()

    result = rows(
        """
        SELECT *
        FROM suppliers
        WHERE organisation_id = ?
        AND active = 1
        ORDER BY name
        """,
        (
            user["organisation_id"],
        )
    )

    output = []

    for item in result:

        data = dict(item)

        data["spend"] = 0
        data["movement"] = 0
        data["type"] = "Supplier"
        data["lead"] = (
            f"{item['payment_terms']} days"
        )

        output.append(data)

    return jsonify(
        suppliers=output
    )


# ============================================================
# MARGINS
# ============================================================

@app.get("/api/margins")
@auth
def margins():

    user = me()
    current_site = site()

    month = date.today().strftime(
        "%Y-%m"
    )

    financials = financial(
        month,
        user,
        current_site
    )

    revenue = financials[
        "revenue"
    ]

    if revenue:

        wet = round(
            (
                revenue
                - financials["drink_cost"]
            )
            / revenue
            * 100,
            1
        )

        dry = round(
            (
                revenue
                - financials["food_cost"]
            )
            / revenue
            * 100,
            1
        )

        food = dry

    else:

        wet = 0
        dry = 0
        food = 0

    return jsonify(
        wet=wet,
        dry=dry,
        food=food,
        overall=financials[
            "gross_margin"
        ]
    )


# ============================================================
# MENU COSTING
# ============================================================

@app.get("/api/menu")
@auth
def menu():

    user = me()
    current_site = site()

    items = rows(
        """
        SELECT *
        FROM menu_items
        WHERE organisation_id = ?
        AND site_id = ?
        AND active = 1
        ORDER BY category, name
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    output = []

    for item in items:

        data = dict(item)

        data["sell"] = item[
            "selling_price"
        ]

        data["cost"] = item[
            "recipe_cost"
        ]

        data["margin"] = round(
            (
                item["selling_price"]
                - item["recipe_cost"]
            )
            / item["selling_price"]
            * 100,
            1
        ) if item["selling_price"] else 0

        output.append(data)

    return jsonify(
        menu=output
    )


# ============================================================
# CALENDAR / EVENTS
# ============================================================

@app.get("/api/events")
@auth
def events():

    user = me()
    current_site = site()

    result = rows(
        """
        SELECT *
        FROM events
        WHERE organisation_id = ?
        AND site_id = ?
        ORDER BY event_date, event_time
        """,
        (
            user["organisation_id"],
            current_site["id"]
        )
    )

    return jsonify(
        events=[
            dict(item)
            for item in result
        ]
    )


# ============================================================
# TEAM
# ============================================================

@app.get("/api/users")
@auth
@manager
def users():

    user = me()

    result = rows(
        """
        SELECT
            id,
            name,
            email username,
            role,
            active
        FROM users
        WHERE organisation_id = ?
        ORDER BY name
        """,
        (
            user["organisation_id"],
        )
    )

    return jsonify(
        users=[
            dict(item)
            for item in result
        ]
    )


@app.post("/api/users")
@auth
@manager
def add_user():

    user = me()
    data = request.get_json() or {}

    name = data.get(
        "name",
        ""
    ).strip()

    username = data.get(
        "username",
        ""
    ).strip().lower()

    password = data.get(
        "password",
        ""
    )

    role = (
        data.get("role")
        or "FOH"
    )

    if (
        not name
        or not username
        or len(password) < 8
    ):

        return jsonify(
            error="Name, username and an 8+ character password are required"
        ), 400

    try:

        user_id = run(
            """
            INSERT INTO users
            (
                organisation_id,
                name,
                email,
                password_hash,
                role,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user["organisation_id"],
                name,
                username,
                generate_password_hash(
                    password
                ),
                role,
                now()
            )
        )

    except sqlite3.IntegrityError:

        return jsonify(
            error="That username already exists"
        ), 400

    audit(
        "Created",
        "user",
        user_id,
        name
    )

    return jsonify(
        ok=True,
        id=user_id
    )


# ============================================================
# WEATHER
# ============================================================

@app.get("/api/weather")
@auth
def weather():

    return jsonify(
        current={
            "description":
                "Weather unavailable",
            "temperature":
                0,
            "code":
                0
        },
        daily=[]
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return jsonify(
        status="ok",
        service="OrderFlow"
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        )
    )