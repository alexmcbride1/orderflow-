import os
from datetime import date, datetime, timedelta
from functools import wraps
import hashlib
import hmac
import json
import time

import psycopg
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash, check_password_hash


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. Add the PostgreSQL Internal Database URL "
        "to the Render environment variables."
    )


app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_IN_RENDER")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)


def conn():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )


def sql_placeholders(sql):
    return sql.replace("?", "%s")


def q(sql, args=(), one=False):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql_placeholders(sql), args)
            rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    statement = sql.strip()
    is_insert = statement.upper().startswith("INSERT")
    if is_insert and "RETURNING" not in statement.upper():
        statement = statement.rstrip().rstrip(";") + " RETURNING id"
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql_placeholders(statement), args)
            if is_insert:
                row = cur.fetchone()
                return row["id"] if row else None
    return None


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


# -----------------------------------------------------------------------------
# ORDERFLOW SaaS PRICING / STRIPE BILLING
# -----------------------------------------------------------------------------
BASE_PRICE_PER_SITE = 250.0
INCLUDED_USERS_PER_SITE = 3
EXTRA_USER_PRICE = 20.0
VAT_RATE = 0.20
CONTRACT_MONTHS = 12
PAYMENT_GRACE_DAYS = 7


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(str(value)[:10])
        except Exception:
            return None


def _contract_end(start_date):
    # Exactly 12 months for the current commercial model.
    try:
        return start_date.replace(year=start_date.year + 1)
    except ValueError:
        # 29 February -> 28 February in the following year.
        return start_date.replace(month=2, day=28, year=start_date.year + 1)


def subscription_for(organisation_id):
    return q("SELECT * FROM subscriptions WHERE organisation_id=?", (organisation_id,), True)


def pricing_for(organisation_id, preserve_commitment=True):
    sub = subscription_for(organisation_id)
    sites_row = q("SELECT COUNT(*) AS n FROM sites WHERE organisation_id=? AND active=1", (organisation_id,), True)
    users_row = q("SELECT COUNT(*) AS n FROM users WHERE organisation_id=? AND active=1", (organisation_id,), True)
    active_sites = max(1, int((sites_row or {}).get("n") or 0))
    active_users = max(1, int((users_row or {}).get("n") or 0))

    base = float((sub or {}).get("base_price") or BASE_PRICE_PER_SITE)
    included_per_site = int((sub or {}).get("included_users_per_site") or INCLUDED_USERS_PER_SITE)
    extra_price = float((sub or {}).get("extra_user_price") or EXTRA_USER_PRICE)
    vat_rate = float((sub or {}).get("vat_rate") if (sub or {}).get("vat_rate") is not None else VAT_RATE)

    committed_sites = int((sub or {}).get("contracted_sites") or active_sites)
    committed_users = int((sub or {}).get("contracted_users") or active_users)
    contract_end = _parse_iso_date((sub or {}).get("contract_end"))
    inside_term = bool(contract_end and date.today() < contract_end)

    if preserve_commitment and inside_term:
        billable_sites = max(active_sites, committed_sites)
        billable_users = max(active_users, committed_users)
    else:
        billable_sites = active_sites
        billable_users = active_users

    included_users = included_per_site * billable_sites
    extra_users = max(0, billable_users - included_users)
    net = round((base * billable_sites) + (extra_price * extra_users), 2)
    vat = round(net * vat_rate, 2)
    gross = round(net + vat, 2)
    return {
        "active_sites": active_sites,
        "active_users": active_users,
        "billable_sites": billable_sites,
        "billable_users": billable_users,
        "included_users": included_users,
        "extra_users": extra_users,
        "base_price": base,
        "extra_user_price": extra_price,
        "vat_rate": vat_rate,
        "net": net,
        "vat": vat,
        "gross": gross,
        "inside_term": inside_term,
    }


def stripe_configured():
    return bool(
        os.environ.get("STRIPE_SECRET_KEY")
        and os.environ.get("STRIPE_BASE_PRICE_ID")
        and os.environ.get("STRIPE_EXTRA_USER_PRICE_ID")
        and os.environ.get("STRIPE_VAT_TAX_RATE_ID")
    )


def stripe_request(method, path, data=None):
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("Stripe is not configured. Add STRIPE_SECRET_KEY in Render.")
    url = "https://api.stripe.com/v1" + path
    r = requests.request(method, url, auth=(key, ""), data=data or {}, timeout=20)
    try:
        payload = r.json()
    except Exception:
        payload = {"error": {"message": "Stripe returned an invalid response."}}
    if not r.ok:
        msg = ((payload.get("error") or {}).get("message") or "Stripe request failed")
        raise RuntimeError(msg)
    return payload


def verify_stripe_signature(payload, signature_header, secret, tolerance=300):
    if not payload or not signature_header or not secret:
        return False
    pieces = {}
    for part in signature_header.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            pieces.setdefault(k, []).append(v)
    try:
        timestamp = int((pieces.get("t") or ["0"])[0])
    except Exception:
        return False
    if abs(int(time.time()) - timestamp) > tolerance:
        return False
    signed = str(timestamp).encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in pieces.get("v1", []))


def record_stripe_payment(organisation_id, invoice):
    invoice_id = str(invoice.get("id") or "")
    if invoice_id and q("SELECT id FROM subscription_payments WHERE stripe_invoice_id=?", (invoice_id,), True):
        return
    total_pence = int(invoice.get("amount_paid") or invoice.get("total") or 0)
    gross = round(total_pence / 100.0, 2)
    # OrderFlow commercial pricing is VAT-exclusive. Stripe applies the configured
    # 20% tax rate, so derive the accounting split from the paid gross amount.
    net = round(gross / (1 + VAT_RATE), 2) if gross else 0
    vat = round(gross - net, 2)
    paid_at = date.today().isoformat()
    execute(
        """INSERT INTO subscription_payments(
               organisation_id,amount,payment_date,status,method,reference,notes,created_at,
               stripe_invoice_id,vat_amount,net_amount)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (organisation_id, gross, paid_at, "Paid", "Stripe", invoice_id, "Automatic Stripe subscription payment", now(), invoice_id, vat, net),
    )


def activate_contract_from_checkout(organisation_id, checkout):
    p = pricing_for(organisation_id, preserve_commitment=False)
    start = date.today()
    end = _contract_end(start)
    customer_id = str(checkout.get("customer") or "")
    subscription_id = str(checkout.get("subscription") or "")
    session_id = str(checkout.get("id") or "")
    base_price_id = os.environ.get("STRIPE_BASE_PRICE_ID", "")
    extra_price_id = os.environ.get("STRIPE_EXTRA_USER_PRICE_ID", "")
    execute(
        """UPDATE subscriptions SET plan=?,monthly_price=?,status='Active',contract_start=?,contract_end=?,
           contract_months=?,contracted_sites=?,contracted_users=?,stripe_customer_id=?,stripe_subscription_id=?,
           stripe_checkout_session_id=?,stripe_base_price_id=?,stripe_extra_price_id=?,past_due_since='',
           last_payment_status='Paid',last_payment_at=? WHERE organisation_id=?""",
        (
            "OrderFlow Restaurant", p["net"], start.isoformat(), end.isoformat(), CONTRACT_MONTHS,
            p["billable_sites"], p["billable_users"], customer_id, subscription_id, session_id,
            base_price_id, extra_price_id, now(), organisation_id,
        ),
    )
    company_event(
        "Contract activated",
        f"12-month minimum term Â· Â£{p['net']:.2f} + VAT/month Â· ends {end.isoformat()}",
        organisation_id,
    )


def subscription_blocks_access(sub):
    if not sub:
        return False
    status = str(sub.get("status") or "")
    if status in ("Suspended", "Cancelled"):
        return True
    if status == "Past due":
        since = _parse_iso_date(sub.get("past_due_since"))
        return bool(since and date.today() >= since + timedelta(days=PAYMENT_GRACE_DAYS))
    return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS organisations(
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'GBP',
    vat_registered INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    address TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS users(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
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
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    sale_date TEXT NOT NULL,
    category TEXT NOT NULL,
    net DOUBLE PRECISION NOT NULL DEFAULT 0,
    vat DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross DOUBLE PRECISION NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'Manual',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS expenses(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    expense_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    supplier TEXT DEFAULT '',
    net DOUBLE PRECISION NOT NULL DEFAULT 0,
    vat DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Posted',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS invoices(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    supplier TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    category TEXT NOT NULL,
    net DOUBLE PRECISION NOT NULL,
    vat DOUBLE PRECISION NOT NULL,
    gross DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'Awaiting approval',
    approved_by BIGINT,
    approved_at TEXT,
    paid_at TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS payments(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    payment_type TEXT NOT NULL,
    payee TEXT NOT NULL,
    reference TEXT DEFAULT '',
    amount DOUBLE PRECISION NOT NULL,
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending approval',
    method TEXT NOT NULL DEFAULT 'Bank transfer',
    source_id BIGINT,
    approved_by BIGINT,
    approved_at TEXT,
    paid_at TEXT,
    external_reference TEXT DEFAULT '',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS employees(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    department TEXT NOT NULL DEFAULT 'FOH',
    job_title TEXT DEFAULT '',
    pay_type TEXT NOT NULL DEFAULT 'Hourly',
    pay_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    ni_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    pension_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    holiday_allowance DOUBLE PRECISION NOT NULL DEFAULT 28,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shifts(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    employee_id BIGINT NOT NULL,
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
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    gross_pay DOUBLE PRECISION NOT NULL,
    employer_costs DOUBLE PRECISION NOT NULL DEFAULT 0,
    deductions DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_pay DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',
    created_at TEXT NOT NULL,
    approved_by BIGINT,
    approved_at TEXT,
    paid_at TEXT,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS stock_items(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    on_hand DOUBLE PRECISION NOT NULL DEFAULT 0,
    par_level DOUBLE PRECISION NOT NULL DEFAULT 0,
    unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    supplier TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stock_movements(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    stock_item_id BIGINT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    movement_type TEXT NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(stock_item_id) REFERENCES stock_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS suppliers(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    contact TEXT DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    payment_terms INTEGER NOT NULL DEFAULT 30,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS purchase_orders(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    supplier_id BIGINT NOT NULL,
    order_date TEXT NOT NULL,
    delivery_date TEXT,
    total DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Draft',
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS menu_items(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    selling_price DOUBLE PRECISION NOT NULL,
    recipe_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    start_time TEXT DEFAULT '',
    end_time TEXT DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'General',
    notes TEXT DEFAULT '',
    all_day INTEGER NOT NULL DEFAULT 0,
    created_by BIGINT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS budgets(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    month TEXT NOT NULL,
    revenue DOUBLE PRECISION NOT NULL DEFAULT 0,
    food_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    drink_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    labour DOUBLE PRECISION NOT NULL DEFAULT 0,
    overheads DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(site_id,month),
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS subscriptions(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'Starter',
    monthly_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Trial',
    trial_end TEXT DEFAULT '',
    next_billing_date TEXT DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_at TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS subscription_payments(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Paid',
    method TEXT DEFAULT '',
    reference TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS company_events(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT,
    event_type TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS eho_daily_checks(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    check_date TEXT NOT NULL,
    check_type TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '{}',
    problems TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    signed_by BIGINT NOT NULL,
    signed_name TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(signed_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS eho_temperature_checks(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    check_date TEXT NOT NULL,
    check_time TEXT NOT NULL,
    check_type TEXT NOT NULL,
    item TEXT NOT NULL,
    temperature DOUBLE PRECISION NOT NULL,
    target_min DOUBLE PRECISION,
    target_max DOUBLE PRECISION,
    result TEXT NOT NULL DEFAULT 'OK',
    method TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    recorded_by BIGINT NOT NULL,
    recorded_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(recorded_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS eho_records(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    record_date TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Complete',
    details TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    due_date TEXT DEFAULT '',
    reference TEXT DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by BIGINT NOT NULL,
    created_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_by BIGINT,
    verified_at TEXT DEFAULT '',
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES users(id),
    FOREIGN KEY(verified_by) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS eho_four_week_reviews(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT NOT NULL,
    site_id BIGINT NOT NULL,
    review_date TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    persistent_problems TEXT DEFAULT '',
    changes_made TEXT DEFAULT '',
    safe_methods_current INTEGER NOT NULL DEFAULT 1,
    allergen_info_current INTEGER NOT NULL DEFAULT 1,
    training_current INTEGER NOT NULL DEFAULT 1,
    signed_by BIGINT NOT NULL,
    signed_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organisation_id) REFERENCES organisations(id) ON DELETE CASCADE,
    FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY(signed_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS audit_log(
    id BIGSERIAL PRIMARY KEY,
    organisation_id BIGINT,
    user_id BIGINT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id BIGINT,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def init_db():
    with conn() as c:
        with c.cursor() as cur:
            for statement in [x.strip() for x in SCHEMA.split(";") if x.strip()]:
                cur.execute(statement)

            # SaaS billing / 12-month contract fields. ADD COLUMN IF NOT EXISTS
            # keeps existing Render databases safe during deployment.
            migrations = [
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS base_price DOUBLE PRECISION NOT NULL DEFAULT 250",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS included_users_per_site INTEGER NOT NULL DEFAULT 3",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS extra_user_price DOUBLE PRECISION NOT NULL DEFAULT 20",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS vat_rate DOUBLE PRECISION NOT NULL DEFAULT 0.20",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contract_months INTEGER NOT NULL DEFAULT 12",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contract_start TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contract_end TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contracted_sites INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS contracted_users INTEGER NOT NULL DEFAULT 3",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS billing_email TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_checkout_session_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_base_price_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_extra_price_id TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS past_due_since TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_payment_status TEXT DEFAULT ''",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_payment_at TEXT DEFAULT ''",
                "ALTER TABLE subscription_payments ADD COLUMN IF NOT EXISTS stripe_invoice_id TEXT DEFAULT ''",
                "ALTER TABLE subscription_payments ADD COLUMN IF NOT EXISTS vat_amount DOUBLE PRECISION NOT NULL DEFAULT 0",
                "ALTER TABLE subscription_payments ADD COLUMN IF NOT EXISTS net_amount DOUBLE PRECISION NOT NULL DEFAULT 0",
            ]
            for statement in migrations:
                cur.execute(statement)

            cur.execute("""
                INSERT INTO subscriptions(organisation_id, plan, monthly_price, status, started_at)
                SELECT o.id, 'Starter', 0, 'Trial', o.created_at
                FROM organisations o
                LEFT JOIN subscriptions s ON s.organisation_id=o.id
                WHERE s.id IS NULL
            """)


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
        s = q(
            "SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1",
            (sid, u["organisation_id"]),
            True,
        )
        if s:
            return s
    s = q(
        "SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1",
        (u["organisation_id"],),
        True,
    )
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


def account_admin_required(fn):
    """Only organisation owners/admins can manage paid OrderFlow login licences."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = user()
        if not u:
            return jsonify(error="Not authenticated"), 401
        if u["role"] not in ("Owner", "Admin"):
            return jsonify(error="Owner or Admin permission required"), 403
        return fn(*args, **kwargs)
    return wrapped


def licence_summary(organisation_id):
    sub = subscription_for(organisation_id) or {}
    p = pricing_for(organisation_id)
    contracted_sites = max(1, int(sub.get("contracted_sites") or p["billable_sites"] or 1))
    contracted_users = max(0, int(sub.get("contracted_users") or 0))
    included_capacity = int(sub.get("included_users_per_site") or INCLUDED_USERS_PER_SITE) * contracted_sites
    licensed_capacity = max(included_capacity, contracted_users)
    active_users = int(q("SELECT COUNT(*) AS n FROM users WHERE organisation_id=? AND active=1", (organisation_id,), True)["n"] or 0)
    available = max(0, licensed_capacity - active_users)
    return {
        **p,
        "licensed_capacity": licensed_capacity,
        "active_users": active_users,
        "available_licences": available,
        "contracted_users": contracted_users,
        "contracted_sites": contracted_sites,
        "contract_start": sub.get("contract_start") or "",
        "contract_end": sub.get("contract_end") or "",
        "status": sub.get("status") or "",
    }


def sync_stripe_subscription_quantities(organisation_id, contracted_sites, contracted_users):
    """Keep Stripe quantities aligned with the contract without changing unit prices."""
    sub = subscription_for(organisation_id) or {}
    subscription_id = (sub.get("stripe_subscription_id") or "").strip()
    if not subscription_id:
        return
    base_price_id = (sub.get("stripe_base_price_id") or os.environ.get("STRIPE_BASE_PRICE_ID", "")).strip()
    extra_price_id = (sub.get("stripe_extra_price_id") or os.environ.get("STRIPE_EXTRA_USER_PRICE_ID", "")).strip()
    vat_tax_rate_id = os.environ.get("STRIPE_VAT_TAX_RATE_ID", "").strip()
    included_per_site = int(sub.get("included_users_per_site") or INCLUDED_USERS_PER_SITE)
    included_capacity = included_per_site * int(contracted_sites)
    extra_quantity = max(0, int(contracted_users) - included_capacity)

    remote = stripe_request("GET", "/subscriptions/" + subscription_id)
    items = ((remote.get("items") or {}).get("data") or [])
    by_price = {}
    for item in items:
        price = item.get("price") or {}
        pid = price.get("id") if isinstance(price, dict) else price
        if pid:
            by_price[str(pid)] = item

    base_item = by_price.get(base_price_id)
    if base_item:
        stripe_request("POST", "/subscription_items/" + str(base_item["id"]), {
            "quantity": str(max(1, int(contracted_sites))),
            "proration_behavior": "create_prorations",
        })

    extra_item = by_price.get(extra_price_id)
    if extra_quantity > 0:
        if extra_item:
            stripe_request("POST", "/subscription_items/" + str(extra_item["id"]), {
                "quantity": str(extra_quantity),
                "proration_behavior": "create_prorations",
            })
        else:
            data = {
                "subscription": subscription_id,
                "price": extra_price_id,
                "quantity": str(extra_quantity),
                "proration_behavior": "create_prorations",
            }
            if vat_tax_rate_id:
                data["tax_rates[0]"] = vat_tax_rate_id
            stripe_request("POST", "/subscription_items", data)
    elif extra_item:
        stripe_request("DELETE", "/subscription_items/" + str(extra_item["id"]), {
            "proration_behavior": "create_prorations",
        })


def audit(action, entity, entity_id=None, detail=""):
    u = user()
    execute(
        """INSERT INTO audit_log(organisation_id,user_id,action,entity,entity_id,detail,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            u["organisation_id"] if u else None,
            u["id"] if u else None,
            action,
            entity,
            entity_id,
            detail,
            now(),
        ),
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
        return {
            "month": month,
            "revenue": 0,
            "food": 0,
            "drink": 0,
            "cogs": 0,
            "gross_profit": 0,
            "labour": 0,
            "overheads": 0,
            "ebitda": 0,
            "gross_margin": 0,
            "ebitda_margin": 0,
        }

    start, end = month_bounds(month)

    revenue_row = q(
        "SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<?",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    food_row = q(
        "SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<? AND category='Food'",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    drink_row = q(
        "SELECT COALESCE(SUM(gross),0) x FROM sales WHERE organisation_id=? AND site_id=? AND sale_date>=? AND sale_date<? AND category='Drink'",
        (u["organisation_id"], s["id"], start, end),
        True,
    )

    revenue = float(revenue_row["x"] or 0)
    food = float(food_row["x"] or 0)
    drink = float(drink_row["x"] or 0)

    cogs_row = q(
        """SELECT COALESCE(SUM(ABS(sm.quantity) * si.unit_cost),0) x
           FROM stock_movements sm JOIN stock_items si ON si.id=sm.stock_item_id
           WHERE sm.organisation_id=? AND sm.site_id=? AND sm.created_at>=? AND sm.created_at<?
           AND LOWER(sm.movement_type) IN ('usage','waste','sale','cogs')""",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    cogs = float(cogs_row["x"] or 0)

    labour_row = q(
        """SELECT COALESCE(SUM(CASE WHEN sh.status!='Cancelled' THEN
           GREATEST(0,(((CAST(substr(sh.end_time,1,2) AS INTEGER)*60+CAST(substr(sh.end_time,4,2) AS INTEGER))-
           (CAST(substr(sh.start_time,1,2) AS INTEGER)*60+CAST(substr(sh.start_time,4,2) AS INTEGER))) / 60.0)-sh.break_minutes/60.0) * e.pay_rate ELSE 0 END),0) x
           FROM shifts sh JOIN employees e ON e.id=sh.employee_id
           WHERE sh.organisation_id=? AND sh.site_id=? AND sh.shift_date>=? AND sh.shift_date<?""",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    labour = float(labour_row["x"] or 0)

    overhead_row = q(
        """SELECT COALESCE(SUM(gross),0) x FROM expenses
           WHERE organisation_id=? AND site_id=? AND expense_date>=? AND expense_date<?
           AND category NOT IN ('Food','Drink','Labour')""",
        (u["organisation_id"], s["id"], start, end),
        True,
    )
    overheads = float(overhead_row["x"] or 0)

    gross_profit = revenue - cogs
    ebitda = gross_profit - labour - overheads
    gross_margin = gross_profit / revenue * 100 if revenue else 0
    ebitda_margin = ebitda / revenue * 100 if revenue else 0

    return {
        "month": month,
        "revenue": revenue,
        "food": food,
        "drink": drink,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "labour": labour,
        "overheads": overheads,
        "ebitda": ebitda,
        "gross_margin": gross_margin,
        "ebitda_margin": ebitda_margin,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        u = q(
            "SELECT * FROM users WHERE lower(email)=? AND active=1 ORDER BY id LIMIT 1",
            (email,),
            True,
        )
        if u and check_password_hash(u["password_hash"], password):
            sub = subscription_for(u["organisation_id"])
            if subscription_blocks_access(sub):
                return render_template("login.html", error="This OrderFlow account is currently suspended because the subscription is not in good standing. Please contact OrderFlow support."), 403
            session.clear()
            session["user_id"] = u["id"]
            s = q(
                "SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY id LIMIT 1",
                (u["organisation_id"],),
                True,
            )
            if s:
                session["site_id"] = s["id"]
            if sub and sub.get("status") == "Payment required":
                return redirect(url_for("subscribe"))
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
            return render_template(
                "onboarding.html",
                error="Complete all fields and use a password of at least 8 characters.",
            )

        try:
            with conn() as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO organisations(name,business_type,created_at) VALUES(%s,%s,%s) RETURNING id",
                        (business, business_type, now()),
                    )
                    organisation_id = cur.fetchone()["id"]

                    cur.execute(
                        """INSERT INTO subscriptions(
                               organisation_id,plan,monthly_price,status,started_at,base_price,
                               included_users_per_site,extra_user_price,vat_rate,contract_months,
                               contracted_sites,contracted_users,billing_email)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (organisation_id) DO NOTHING""",
                        (
                            organisation_id, "OrderFlow Restaurant", BASE_PRICE_PER_SITE,
                            "Payment required", now(), BASE_PRICE_PER_SITE, INCLUDED_USERS_PER_SITE,
                            EXTRA_USER_PRICE, VAT_RATE, CONTRACT_MONTHS, 1, 3, email,
                        ),
                    )

                    cur.execute(
                        "INSERT INTO sites(organisation_id,name,address,created_at) VALUES(%s,%s,%s,%s) RETURNING id",
                        (organisation_id, site_name, address, now()),
                    )
                    site_id = cur.fetchone()["id"]

                    cur.execute(
                        """INSERT INTO users(organisation_id,name,email,password_hash,role,created_at)
                           VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (
                            organisation_id,
                            owner_name,
                            email,
                            generate_password_hash(password),
                            "Owner",
                            now(),
                        ),
                    )
                    user_id = cur.fetchone()["id"]

        except psycopg.IntegrityError:
            return render_template(
                "onboarding.html",
                error="That email is already registered for this business.",
            )

        session.clear()
        session["user_id"] = user_id
        session["site_id"] = site_id
        return redirect(url_for("subscribe"))

    return render_template("onboarding.html")


@app.route("/subscribe", methods=["GET", "POST"])
@login_required
def subscribe():
    u = user()
    organisation_id = u["organisation_id"]
    sub = subscription_for(organisation_id)
    pricing = pricing_for(organisation_id)
    if sub and sub.get("status") == "Active":
        return redirect(url_for("home"))

    error = None
    if request.method == "POST":
        action = (request.form.get("action") or "pay").strip()

        if action == "admin_preview":
            admin_email = (request.form.get("admin_email") or "").strip().lower()
            admin_password = request.form.get("admin_password") or ""
            expected_email = os.environ.get("COMPANY_ADMIN_EMAIL", "").strip().lower()
            expected_password = os.environ.get("COMPANY_ADMIN_PASSWORD", "")

            if (
                expected_email
                and expected_password
                and hmac.compare_digest(admin_email, expected_email)
                and hmac.compare_digest(admin_password, expected_password)
            ):
                session["billing_preview_bypass_org"] = int(organisation_id)
                session["billing_preview_admin"] = True
                return redirect(url_for("home"))

            error = "Incorrect company admin email or password."

        elif not stripe_configured():
            error = "Online billing is not fully configured yet. Please contact OrderFlow."
        elif request.form.get("accept_contract") != "yes":
            error = "You must accept the 12-month minimum-term agreement to continue."
        else:
            try:
                base_price_id = os.environ.get("STRIPE_BASE_PRICE_ID", "").strip()
                extra_price_id = os.environ.get("STRIPE_EXTRA_USER_PRICE_ID", "").strip()
                vat_tax_rate_id = os.environ.get("STRIPE_VAT_TAX_RATE_ID", "").strip()
                success_url = request.url_root.rstrip("/") + "/subscription/success?session_id={CHECKOUT_SESSION_ID}"
                cancel_url = request.url_root.rstrip("/") + "/subscribe"
                data = {
                    "mode": "subscription",
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                    "client_reference_id": str(organisation_id),
                    "customer_email": (sub or {}).get("billing_email") or u["email"],
                    "billing_address_collection": "required",
                    "tax_id_collection[enabled]": "true",
                    "payment_method_collection": "always",
                    "metadata[organisation_id]": str(organisation_id),
                    "subscription_data[metadata][organisation_id]": str(organisation_id),
                    "line_items[0][price]": base_price_id,
                    "line_items[0][quantity]": str(pricing["billable_sites"]),
                    "line_items[0][tax_rates][0]": vat_tax_rate_id,
                }
                if pricing["extra_users"] > 0:
                    data.update({
                        "line_items[1][price]": extra_price_id,
                        "line_items[1][quantity]": str(pricing["extra_users"]),
                        "line_items[1][tax_rates][0]": vat_tax_rate_id,
                    })
                checkout = stripe_request("POST", "/checkout/sessions", data)
                execute(
                    """UPDATE subscriptions SET stripe_checkout_session_id=?,stripe_base_price_id=?,
                       stripe_extra_price_id=?,monthly_price=?,contracted_sites=?,contracted_users=?
                       WHERE organisation_id=?""",
                    (
                        checkout.get("id", ""), base_price_id, extra_price_id, pricing["net"],
                        pricing["billable_sites"], pricing["billable_users"], organisation_id,
                    ),
                )
                return redirect(checkout["url"])
            except Exception as exc:
                error = str(exc)

    return render_template(
        "subscribe.html", user=u, organisation=org(), pricing=pricing, subscription=sub,
        error=error, contract_months=CONTRACT_MONTHS,
    )


@app.get("/subscription/success")
@login_required
def subscription_success():
    u = user()
    sid = (request.args.get("session_id") or "").strip()
    activated = False
    error = None
    if sid and os.environ.get("STRIPE_SECRET_KEY"):
        try:
            checkout = stripe_request("GET", "/checkout/sessions/" + sid)
            if str(checkout.get("client_reference_id") or "") == str(u["organisation_id"]):
                if checkout.get("status") == "complete" or checkout.get("payment_status") in ("paid", "no_payment_required"):
                    activate_contract_from_checkout(u["organisation_id"], checkout)
                    activated = True
        except Exception as exc:
            error = str(exc)
    sub = subscription_for(u["organisation_id"])
    activated = activated or bool(sub and sub.get("status") == "Active")
    return render_template("subscription_success.html", activated=activated, error=error, subscription=sub)


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data(cache=False)
    signature = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not verify_stripe_signature(payload, signature, secret):
        return jsonify(error="Invalid Stripe signature"), 400
    try:
        event = json.loads(payload.decode("utf-8"))
        event_type = event.get("type")
        obj = ((event.get("data") or {}).get("object") or {})

        if event_type == "checkout.session.completed":
            organisation_id = int((obj.get("metadata") or {}).get("organisation_id") or obj.get("client_reference_id") or 0)
            if organisation_id:
                activate_contract_from_checkout(organisation_id, obj)

        elif event_type == "invoice.paid":
            subscription_id = str(obj.get("subscription") or "")
            sub = q("SELECT * FROM subscriptions WHERE stripe_subscription_id=?", (subscription_id,), True)
            if sub:
                record_stripe_payment(sub["organisation_id"], obj)
                next_ts = obj.get("period_end")
                next_date = datetime.utcfromtimestamp(next_ts).date().isoformat() if next_ts else ""
                execute(
                    """UPDATE subscriptions SET status='Active',past_due_since='',last_payment_status='Paid',
                       last_payment_at=?,next_billing_date=? WHERE organisation_id=?""",
                    (now(), next_date, sub["organisation_id"]),
                )
                company_event("Stripe payment received", f"Invoice {obj.get('id','')}", sub["organisation_id"])

        elif event_type == "invoice.payment_failed":
            subscription_id = str(obj.get("subscription") or "")
            sub = q("SELECT * FROM subscriptions WHERE stripe_subscription_id=?", (subscription_id,), True)
            if sub:
                execute(
                    """UPDATE subscriptions SET status='Past due',past_due_since=CASE WHEN past_due_since='' THEN ? ELSE past_due_since END,
                       last_payment_status='Failed' WHERE organisation_id=?""",
                    (date.today().isoformat(), sub["organisation_id"]),
                )
                company_event("Payment failed", f"Stripe invoice {obj.get('id','')}", sub["organisation_id"])

        elif event_type == "customer.subscription.deleted":
            subscription_id = str(obj.get("id") or "")
            sub = q("SELECT * FROM subscriptions WHERE stripe_subscription_id=?", (subscription_id,), True)
            if sub:
                end = _parse_iso_date(sub.get("contract_end"))
                status = "Suspended" if end and date.today() < end else "Cancelled"
                execute("UPDATE subscriptions SET status=?,cancelled_at=? WHERE organisation_id=?", (status, now(), sub["organisation_id"]))
                company_event("Stripe subscription ended", f"Account status: {status}", sub["organisation_id"])

        return jsonify(received=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/")
@login_required
def home():
    u = user()
    sub = subscription_for(u["organisation_id"])
    if sub and sub.get("status") == "Payment required":
        preview_ok = (
            session.get("billing_preview_admin") is True
            and int(session.get("billing_preview_bypass_org") or 0) == int(u["organisation_id"])
        )
        if not preview_ok:
            return redirect(url_for("subscribe"))
    if subscription_blocks_access(sub):
        session.clear()
        return redirect(url_for("login"))
    return render_template("app.html", user=u, organisation=org(), site=current_site())


@app.get("/app")
@login_required
def app_home():
    return redirect(url_for("home"))


@app.get("/api/me")
@login_required
def api_me():
    u, o, s = user(), org(), current_site()
    return jsonify(user=dict(u), organisation=dict(o), site=dict(s) if s else None)


@app.get("/api/licences")
@login_required
def licences_overview():
    u = user()
    rows = q(
        "SELECT id,name,email,role,active,created_at FROM users WHERE organisation_id=? ORDER BY active DESC,id",
        (u["organisation_id"],),
    )
    return jsonify(licence=licence_summary(u["organisation_id"]), users=[dict(x) for x in rows])


@app.post("/api/licences/upgrade")
@login_required
@account_admin_required
def upgrade_licences():
    u = user()
    organisation_id = u["organisation_id"]
    sub = subscription_for(organisation_id)
    if not sub:
        return jsonify(error="Subscription record not found"), 404
    if sub.get("status") not in ("Active", "Past due"):
        return jsonify(error="Your subscription must be active before adding paid user licences."), 409
    try:
        add_count = int((request.get_json() or {}).get("additional_licences", 1))
    except Exception:
        return jsonify(error="Enter a valid number of licences"), 400
    if add_count < 1 or add_count > 100:
        return jsonify(error="You can add between 1 and 100 licences at a time"), 400

    summary = licence_summary(organisation_id)
    new_capacity = summary["licensed_capacity"] + add_count
    new_contracted_users = max(int(sub.get("contracted_users") or 0), new_capacity)
    sites = max(1, int(sub.get("contracted_sites") or summary["billable_sites"] or 1))
    included = int(sub.get("included_users_per_site") or INCLUDED_USERS_PER_SITE) * sites
    extra_users = max(0, new_contracted_users - included)
    base_price = float(sub.get("base_price") or BASE_PRICE_PER_SITE)
    extra_price = float(sub.get("extra_user_price") or EXTRA_USER_PRICE)
    vat_rate = float(sub.get("vat_rate") if sub.get("vat_rate") is not None else VAT_RATE)
    new_net = round(base_price * sites + extra_price * extra_users, 2)
    new_vat = round(new_net * vat_rate, 2)
    new_gross = round(new_net + new_vat, 2)

    try:
        if sub.get("stripe_subscription_id"):
            sync_stripe_subscription_quantities(organisation_id, sites, new_contracted_users)
    except Exception as exc:
        return jsonify(error=f"Stripe could not update the subscription: {exc}"), 502

    execute(
        """UPDATE subscriptions SET contracted_users=?,monthly_price=? WHERE organisation_id=?""",
        (new_contracted_users, new_net, organisation_id),
    )
    company_event(
        "User licences increased",
        f"{new_capacity} licences Â· Â£{new_net:.2f} + VAT/month",
        organisation_id,
    )
    return jsonify(
        ok=True,
        licensed_capacity=new_capacity,
        monthly_net=new_net,
        monthly_vat=new_vat,
        monthly_gross=new_gross,
        message=f"Licence limit increased to {new_capacity} users.",
    )


@app.post("/api/licences/users")
@login_required
@account_admin_required
def add_login_user():
    u = user()
    organisation_id = u["organisation_id"]
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()[:120]
    email = (d.get("email") or "").strip().lower()[:200]
    password = d.get("password") or ""
    role = (d.get("role") or "Manager").strip()
    allowed_roles = ("Admin", "Finance", "General Manager", "Manager", "User")
    if not name or not email or "@" not in email:
        return jsonify(error="Name and a valid email address are required"), 400
    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400
    if role not in allowed_roles:
        return jsonify(error="Invalid user role"), 400

    summary = licence_summary(organisation_id)
    if summary["active_users"] >= summary["licensed_capacity"]:
        next_capacity = summary["licensed_capacity"] + 1
        sub = subscription_for(organisation_id) or {}
        sites = max(1, int(sub.get("contracted_sites") or summary["billable_sites"] or 1))
        included = int(sub.get("included_users_per_site") or INCLUDED_USERS_PER_SITE) * sites
        next_extra = max(0, next_capacity - included)
        next_net = round(float(sub.get("base_price") or BASE_PRICE_PER_SITE) * sites + float(sub.get("extra_user_price") or EXTRA_USER_PRICE) * next_extra, 2)
        next_vat = round(next_net * float(sub.get("vat_rate") if sub.get("vat_rate") is not None else VAT_RATE), 2)
        return jsonify(
            error="All purchased user licences are in use.",
            upgrade_required=True,
            current_licences=summary["licensed_capacity"],
            proposed_licences=next_capacity,
            proposed_net=next_net,
            proposed_vat=next_vat,
            proposed_gross=round(next_net + next_vat, 2),
        ), 409

    existing = q("SELECT id,active FROM users WHERE organisation_id=? AND lower(email)=?", (organisation_id, email), True)
    if existing:
        return jsonify(error="That email already belongs to an OrderFlow user for this restaurant."), 409
    try:
        user_id = execute(
            """INSERT INTO users(organisation_id,name,email,password_hash,role,active,created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (organisation_id, name, email, generate_password_hash(password), role, now()),
        )
    except psycopg.IntegrityError:
        return jsonify(error="That email is already registered."), 409
    audit("Created", "login_user", user_id, f"{name} Â· {role}")
    company_event("OrderFlow user added", f"{name} Â· {email}", organisation_id)
    return jsonify(ok=True, id=user_id)


@app.post("/api/licences/users/<int:user_id>/deactivate")
@login_required
@account_admin_required
def deactivate_login_user(user_id):
    u = user()
    target = q("SELECT * FROM users WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]), True)
    if not target:
        return jsonify(error="User not found"), 404
    if int(target["id"]) == int(u["id"]):
        return jsonify(error="You cannot deactivate your own account."), 400
    if target.get("role") == "Owner":
        owners = q("SELECT COUNT(*) AS n FROM users WHERE organisation_id=? AND role='Owner' AND active=1", (u["organisation_id"],), True)
        if int(owners["n"] or 0) <= 1:
            return jsonify(error="The organisation must keep at least one active Owner."), 400
    execute("UPDATE users SET active=0 WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]))
    audit("Deactivated", "login_user", user_id, target["email"])
    company_event("OrderFlow user deactivated", target["email"], u["organisation_id"])
    return jsonify(ok=True, message="User deactivated. Contracted licence quantity is unchanged during the minimum term.")


@app.post("/api/licences/users/<int:user_id>/activate")
@login_required
@account_admin_required
def activate_login_user(user_id):
    u = user()
    target = q("SELECT * FROM users WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]), True)
    if not target:
        return jsonify(error="User not found"), 404
    if int(target.get("active") or 0) == 1:
        return jsonify(ok=True)
    summary = licence_summary(u["organisation_id"])
    if summary["active_users"] >= summary["licensed_capacity"]:
        return jsonify(error="All purchased user licences are in use.", upgrade_required=True), 409
    execute("UPDATE users SET active=1 WHERE id=? AND organisation_id=?", (user_id, u["organisation_id"]))
    audit("Activated", "login_user", user_id, target["email"])
    company_event("OrderFlow user reactivated", target["email"], u["organisation_id"])
    return jsonify(ok=True)


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
    site_id = execute(
        "INSERT INTO sites(organisation_id,name,address,created_at) VALUES(?,?,?,?)",
        (u["organisation_id"], name, address, now()),
    )
    audit("Created", "site", site_id, name)
    sub = subscription_for(u["organisation_id"])
    if sub and sub.get("status") in ("Active", "Past due"):
        p = pricing_for(u["organisation_id"])
        new_sites = p["billable_sites"]
        included_capacity = int(sub.get("included_users_per_site") or INCLUDED_USERS_PER_SITE) * new_sites
        new_contracted_users = max(int(sub.get("contracted_users") or 0), included_capacity, p["active_users"])
        extra_users = max(0, new_contracted_users - included_capacity)
        new_net = round(float(sub.get("base_price") or BASE_PRICE_PER_SITE) * new_sites + float(sub.get("extra_user_price") or EXTRA_USER_PRICE) * extra_users, 2)
        try:
            if sub.get("stripe_subscription_id"):
                sync_stripe_subscription_quantities(u["organisation_id"], new_sites, new_contracted_users)
        except Exception as exc:
            return jsonify(error=f"Restaurant created, but Stripe billing could not be updated: {exc}"), 502
        execute(
            """UPDATE subscriptions SET contracted_sites=?,contracted_users=?,monthly_price=?
               WHERE organisation_id=?""",
            (new_sites, new_contracted_users, new_net, u["organisation_id"]),
        )
        company_event("Restaurant added", f"Contract value now Â£{new_net:.2f} + VAT/month", u["organisation_id"])
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
    s = q(
        "SELECT * FROM sites WHERE id=? AND organisation_id=? AND active=1",
        (site_id, u["organisation_id"]),
        True,
    )
    if not s:
        return jsonify(error="Site not found"), 404
    session["site_id"] = site_id
    return jsonify(ok=True)


@app.get("/api/sites")
@login_required
def sites():
    u = user()
    result = q(
        "SELECT * FROM sites WHERE organisation_id=? AND active=1 ORDER BY name",
        (u["organisation_id"],),
    )
    return jsonify(sites=[dict(x) for x in result])


@app.get("/api/dashboard")
@login_required
def dashboard():
    f = finance_summary()
    u, s = user(), current_site()
    pending = q(
        "SELECT COALESCE(SUM(gross),0) x FROM invoices WHERE organisation_id=? AND site_id=? AND status IN ('Awaiting approval','Approved')",
        (u["organisation_id"], s["id"]),
        True,
    )
    stock_low = q(
        "SELECT COUNT(*) x FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 AND on_hand<par_level",
        (u["organisation_id"], s["id"]),
        True,
    )
    payroll = q(
        "SELECT COALESCE(SUM(net_pay),0) x FROM payroll_runs WHERE organisation_id=? AND site_id=? AND status IN ('Draft','Approved')",
        (u["organisation_id"], s["id"]),
        True,
    )
    return jsonify(
        **f,
        outstanding_invoices=float(pending["x"] or 0),
        low_stock=int(stock_low["x"] or 0),
        payroll_due=float(payroll["x"] or 0),
    )


@app.get("/api/finance")
@login_required
def api_finance():
    return jsonify(finance_summary(request.args.get("month")))


@app.get("/api/sales")
@login_required
def get_sales():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM sales WHERE organisation_id=? AND site_id=? ORDER BY sale_date DESC,id DESC LIMIT 200",
        (u["organisation_id"], s["id"]),
    )
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
    sale_id = execute(
        """INSERT INTO sales(organisation_id,site_id,sale_date,category,net,vat,gross,source)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d.get("sale_date") or date.today().isoformat(),
            d.get("category") or "Other",
            gross - vat,
            vat,
            gross,
            d.get("source") or "Manual",
        ),
    )
    audit("Created", "sale", sale_id)
    return jsonify(ok=True, id=sale_id)


@app.get("/api/expenses")
@login_required
def get_expenses():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM expenses WHERE organisation_id=? AND site_id=? ORDER BY expense_date DESC,id DESC LIMIT 200",
        (u["organisation_id"], s["id"]),
    )
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
    expense_id = execute(
        """INSERT INTO expenses(organisation_id,site_id,expense_date,category,description,supplier,net,vat,gross)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d.get("expense_date") or date.today().isoformat(),
            d.get("category") or "Other",
            d.get("description") or "Expense",
            d.get("supplier") or "",
            gross - vat,
            vat,
            gross,
        ),
    )
    audit("Created", "expense", expense_id)
    return jsonify(ok=True, id=expense_id)


@app.get("/api/invoices")
@login_required
def get_invoices():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM invoices WHERE organisation_id=? AND site_id=? ORDER BY due_date,id DESC",
        (u["organisation_id"], s["id"]),
    )
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
        return jsonify(
            error="Supplier, invoice number and positive net amount are required"
        ), 400
    if vat < 0:
        return jsonify(error="VAT cannot be negative"), 400
    invoice_id = execute(
        """INSERT INTO invoices(organisation_id,site_id,supplier,invoice_number,invoice_date,due_date,category,net,vat,gross,notes,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d["supplier"],
            d["invoice_number"],
            d.get("invoice_date") or date.today().isoformat(),
            d.get("due_date") or date.today().isoformat(),
            d.get("category") or "Other",
            net,
            vat,
            net + vat,
            d.get("notes") or "",
            now(),
        ),
    )
    audit("Created", "invoice", invoice_id, d["invoice_number"])
    return jsonify(ok=True, id=invoice_id)


@app.post("/api/invoices/<int:iid>/approve")
@login_required
@manager_required
def approve_invoice(iid):
    u, s = user(), current_site()
    invoice = q(
        "SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?",
        (iid, u["organisation_id"], s["id"]),
        True,
    )
    if not invoice:
        return jsonify(error="Invoice not found"), 404
    if invoice["status"] != "Awaiting approval":
        return jsonify(error="Invoice is not awaiting approval"), 400
    execute(
        "UPDATE invoices SET status='Approved',approved_by=?,approved_at=? WHERE id=?",
        (u["id"], now(), iid),
    )
    payment_id = execute(
        """INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            "Supplier",
            invoice["supplier"],
            invoice["invoice_number"],
            invoice["gross"],
            invoice["due_date"],
            "Scheduled",
            "Bank transfer",
            iid,
            u["id"],
            now(),
        ),
    )
    audit("Approved", "invoice", iid, "Payment " + str(payment_id) + " scheduled")
    return jsonify(ok=True)


@app.post("/api/invoices/<int:iid>/pay")
@login_required
@manager_required
def pay_invoice(iid):
    u, s = user(), current_site()
    invoice = q(
        "SELECT * FROM invoices WHERE id=? AND organisation_id=? AND site_id=?",
        (iid, u["organisation_id"], s["id"]),
        True,
    )
    if not invoice or invoice["status"] != "Approved":
        return jsonify(error="Invoice must be approved first"), 400
    payment_time = now()
    execute("UPDATE invoices SET status='Paid',paid_at=? WHERE id=?", (payment_time, iid))
    execute(
        "UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Supplier'",
        (payment_time, iid),
    )
    audit("Recorded paid", "invoice", iid)
    return jsonify(ok=True)


@app.get("/api/payments")
@login_required
def get_payments():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM payments WHERE organisation_id=? AND site_id=? ORDER BY payment_date,id DESC",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(payments=[dict(r) for r in result])


@app.get("/api/employees")
@login_required
def get_employees():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",
        (u["organisation_id"], s["id"]),
    )
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
    employee_id = execute(
        """INSERT INTO employees(organisation_id,site_id,name,email,department,job_title,pay_type,pay_rate,ni_rate,pension_rate,holiday_allowance)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            name,
            d.get("email") or "",
            d.get("department") or "FOH",
            d.get("job_title") or "",
            d.get("pay_type") or "Hourly",
            rate,
            ni_rate,
            pension_rate,
            holiday_allowance,
        ),
    )
    audit("Created", "employee", employee_id, name)
    return jsonify(ok=True, id=employee_id)


@app.get("/api/shifts")
@login_required
def get_shifts():
    u, s = user(), current_site()
    result = q(
        """SELECT sh.*,e.name employee_name,e.pay_rate FROM shifts sh JOIN employees e ON e.id=sh.employee_id
           WHERE sh.organisation_id=? AND sh.site_id=? ORDER BY shift_date,start_time""",
        (u["organisation_id"], s["id"]),
    )
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
    employee = q(
        "SELECT * FROM employees WHERE id=? AND organisation_id=? AND site_id=? AND active=1",
        (employee_id, u["organisation_id"], s["id"]),
        True,
    )
    if not employee:
        return jsonify(error="Employee not found"), 404
    shift_id = execute(
        "INSERT INTO shifts(organisation_id,site_id,employee_id,shift_date,start_time,end_time,break_minutes) VALUES(?,?,?,?,?,?,?)",
        (
            u["organisation_id"],
            s["id"],
            employee_id,
            d["shift_date"],
            d["start_time"],
            d["end_time"],
            break_minutes,
        ),
    )
    audit("Created", "shift", shift_id, employee["name"])
    return jsonify(ok=True, id=shift_id)


@app.get("/api/payroll")
@login_required
def get_payroll():
    u, s = user(), current_site()
    runs = q(
        "SELECT * FROM payroll_runs WHERE organisation_id=? AND site_id=? ORDER BY period_end DESC",
        (u["organisation_id"], s["id"]),
    )
    employees = q(
        "SELECT * FROM employees WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",
        (u["organisation_id"], s["id"]),
    )
    return jsonify(
        runs=[dict(x) for x in runs],
        employees=[dict(x) for x in employees],
    )


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
    run_id = execute(
        """INSERT INTO payroll_runs(organisation_id,site_id,period_start,period_end,gross_pay,employer_costs,deductions,net_pay,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            d["period_start"],
            d["period_end"],
            gross,
            employer,
            deductions,
            gross - deductions,
            now(),
        ),
    )
    audit("Created", "payroll", run_id)
    return jsonify(ok=True, id=run_id)


@app.post("/api/payroll/<int:rid>/approve")
@login_required
@manager_required
def approve_payroll(rid):
    u, s = user(), current_site()
    payroll_run = q(
        "SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?",
        (rid, u["organisation_id"], s["id"]),
        True,
    )
    if not payroll_run:
        return jsonify(error="Payroll run not found"), 404
    if payroll_run["status"] != "Draft":
        return jsonify(error="Payroll run is not in draft status"), 400
    execute(
        "UPDATE payroll_runs SET status='Approved',approved_by=?,approved_at=? WHERE id=?",
        (u["id"], now(), rid),
    )
    execute(
        """INSERT INTO payments(organisation_id,site_id,payment_type,payee,reference,amount,payment_date,status,method,source_id,approved_by,approved_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            "Payroll",
            "Staff payroll",
            f"PAY-{rid}",
            payroll_run["net_pay"],
            date.today().isoformat(),
            "Scheduled",
            "Bank transfer",
            rid,
            u["id"],
            now(),
        ),
    )
    audit("Approved", "payroll", rid)
    return jsonify(ok=True)


@app.post("/api/payroll/<int:rid>/pay")
@login_required
@manager_required
def pay_payroll(rid):
    u, s = user(), current_site()
    payroll_run = q(
        "SELECT * FROM payroll_runs WHERE id=? AND organisation_id=? AND site_id=?",
        (rid, u["organisation_id"], s["id"]),
        True,
    )
    if not payroll_run or payroll_run["status"] != "Approved":
        return jsonify(error="Payroll must be approved first"), 400
    payment_time = now()
    execute("UPDATE payroll_runs SET status='Paid',paid_at=? WHERE id=?", (payment_time, rid))
    execute(
        "UPDATE payments SET status='Paid',paid_at=? WHERE source_id=? AND payment_type='Payroll'",
        (payment_time, rid),
    )
    audit("Recorded paid", "payroll", rid)
    return jsonify(ok=True)


@app.get("/api/stock")
@login_required
def get_stock():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM stock_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY name",
        (u["organisation_id"], s["id"]),
    )
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
    item_id = execute(
        """INSERT INTO stock_items(organisation_id,site_id,name,category,unit,on_hand,par_level,unit_cost,supplier)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            name,
            d.get("category") or "Food",
            d.get("unit") or "unit",
            on_hand,
            par,
            cost,
            d.get("supplier") or "",
        ),
    )
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
    item = q(
        "SELECT * FROM stock_items WHERE id=? AND organisation_id=? AND site_id=?",
        (iid, u["organisation_id"], s["id"]),
        True,
    )
    if not item:
        return jsonify(error="Stock item not found"), 404
    new_quantity = max(0, item["on_hand"] + quantity)
    execute("UPDATE stock_items SET on_hand=? WHERE id=?", (new_quantity, iid))
    execute(
        """INSERT INTO stock_movements(organisation_id,site_id,stock_item_id,quantity,movement_type,note,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            iid,
            quantity,
            d.get("movement_type") or "Adjustment",
            d.get("note") or "",
            now(),
        ),
    )
    audit("Updated", "stock_item", iid, f"Movement {quantity}")
    return jsonify(ok=True, on_hand=new_quantity)


@app.get("/api/suppliers")
@login_required
def get_suppliers():
    u = user()
    result = q(
        "SELECT * FROM suppliers WHERE organisation_id=? AND active=1 ORDER BY name",
        (u["organisation_id"],),
    )
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
    supplier_id = execute(
        """INSERT INTO suppliers(organisation_id,name,contact,email,phone,payment_terms)
           VALUES(?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            name,
            d.get("contact") or "",
            d.get("email") or "",
            d.get("phone") or "",
            terms,
        ),
    )
    audit("Created", "supplier", supplier_id, name)
    return jsonify(ok=True, id=supplier_id)


@app.get("/api/menu")
@login_required
def get_menu():
    u, s = user(), current_site()
    result = q(
        "SELECT * FROM menu_items WHERE organisation_id=? AND site_id=? AND active=1 ORDER BY category,name",
        (u["organisation_id"], s["id"]),
    )
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
    menu_id = execute(
        """INSERT INTO menu_items(organisation_id,site_id,name,category,selling_price,recipe_cost)
           VALUES(?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            name,
            d.get("category") or "Main",
            price,
            cost,
        ),
    )
    audit("Created", "menu_item", menu_id, name)
    return jsonify(ok=True, id=menu_id)


@app.get("/api/budget")
@login_required
def get_budget():
    u, s = user(), current_site()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    budget = q(
        "SELECT * FROM budgets WHERE organisation_id=? AND site_id=? AND month=?",
        (u["organisation_id"], s["id"], month),
        True,
    )
    return jsonify(budget=dict(budget) if budget else None, month=month)


@app.post("/api/budget")
@login_required
@manager_required
def save_budget():
    u, s = user(), current_site()
    d = request.get_json() or {}
    month = d.get("month") or date.today().strftime("%Y-%m")
    try:
        values = [
            float(d.get(k, 0))
            for k in ("revenue", "food_cost", "drink_cost", "labour", "overheads")
        ]
    except Exception:
        return jsonify(error="Invalid budget values"), 400
    if any(v < 0 for v in values):
        return jsonify(error="Budget values cannot be negative"), 400
    revenue, food_cost, drink_cost, labour, overheads = values
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO budgets(organisation_id,site_id,month,revenue,food_cost,drink_cost,labour,overheads)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(site_id,month) DO UPDATE SET
                   revenue=EXCLUDED.revenue,food_cost=EXCLUDED.food_cost,drink_cost=EXCLUDED.drink_cost,
                   labour=EXCLUDED.labour,overheads=EXCLUDED.overheads""",
                (
                    u["organisation_id"],
                    s["id"],
                    month,
                    revenue,
                    food_cost,
                    drink_cost,
                    labour,
                    overheads,
                ),
            )
    audit("Updated", "budget", None, month)
    return jsonify(ok=True)


@app.get("/api/audit")
@login_required
@manager_required
def audit_log():
    u = user()
    result = q(
        "SELECT * FROM audit_log WHERE organisation_id=? ORDER BY id DESC LIMIT 100",
        (u["organisation_id"],),
    )
    return jsonify(log=[dict(x) for x in result])




# -----------------------------------------------------------------------------
# EHO / FOOD-SAFETY COMPLIANCE (ENGLAND - SFBB/HACCP SUPPORT)
# -----------------------------------------------------------------------------
OPENING_CHECKS = [
    "Fridges, chilled display equipment and freezers are working properly",
    "Other equipment needed for safe food preparation is working properly",
    "Staff are fit for work and wearing clean work clothes",
    "Food preparation areas, equipment and utensils are clean and disinfected",
    "Areas are free from evidence of pest activity",
    "Handwashing and cleaning materials are available",
    "Hot running water is available at sinks and hand wash basins",
    "Probe thermometer is working and cleaning/disinfection materials are available",
    "Allergen information is accurate for all food currently on sale",
    "Required cleaning has been completed according to the cleaning schedule",
]
CLOSING_CHECKS = [
    "Food is covered, labelled and stored correctly",
    "Food on its Use By date that has not been safely used or frozen has been disposed of",
    "Cleaning equipment has been cleaned or disposed of as appropriate",
    "Waste has been removed and bins relined",
    "Food preparation areas, equipment and utensils are clean and disinfected",
    "Washing up has been completed",
    "Floors are swept and clean",
    "Required Prove It / food-safety checks have been recorded",
    "Required cleaning has been completed according to the cleaning schedule",
]
EHO_CATEGORIES = (
    "Cleaning", "Pest control", "Delivery", "Allergen", "Training", "Probe calibration",
    "Maintenance", "HACCP/SFBB", "EHO inspection", "Document", "Waste", "Fit to work", "Other"
)
ALLERGENS_14 = [
    "Celery", "Cereals containing gluten", "Crustaceans", "Eggs", "Fish", "Lupin", "Milk",
    "Molluscs", "Mustard", "Nuts", "Peanuts", "Sesame", "Soya", "Sulphur dioxide and sulphites"
]
TEMP_PRESETS = {
    "Chilled storage": {"max": 8.0, "note": "Legal maximum for foods subject to chill holding requirements in England; OrderFlow recommends operating fridges at 5Â°C or below."},
    "Freezer": {"max": -18.0, "note": "FSA recommended operating target for frozen food; set your documented safe method if different."},
    "Hot holding": {"min": 63.0, "note": "Legal hot-holding minimum in England, subject to applicable exemptions/time controls."},
    "Cooking": {"min": 70.0, "note": "Default OrderFlow verification target only. Record the time/temperature combination required by your documented safe method."},
    "Reheating": {"min": 70.0, "note": "Default OrderFlow verification target only. Food must be reheated thoroughly; use the limit in your documented safe method."},
    "Delivery chilled": {"max": 8.0, "note": "Use supplier/product requirements where stricter; foods subject to chill holding requirements must remain at 8Â°C or below."},
    "Cooling": {"note": "No single universal statutory endpoint is imposed here; record the method and target in your HACCP/SFBB safe method."},
    "Other": {"note": "Use the limits defined in your site food-safety management system."},
}


def _eho_scope():
    u, s = user(), current_site()
    return u, s


def _json_load(value, fallback=None):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {} if fallback is None else fallback


@app.get("/api/eho/overview")
@login_required
def eho_overview():
    u, site = _eho_scope()
    today = date.today().isoformat()
    opening = q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND check_type='Opening' ORDER BY id DESC LIMIT 1", (u["organisation_id"], site["id"], today), True)
    closing = q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND check_type='Closing' ORDER BY id DESC LIMIT 1", (u["organisation_id"], site["id"], today), True)
    temp_today = q("SELECT COUNT(*) AS n FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date=?", (u["organisation_id"], site["id"], today), True)["n"]
    breaches = q("SELECT COUNT(*) AS n FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date=? AND result='Action required'", (u["organisation_id"], site["id"], today), True)["n"]
    open_actions = q("SELECT COUNT(*) AS n FROM eho_records WHERE organisation_id=? AND site_id=? AND status IN ('Action required','Open','Overdue')", (u["organisation_id"], site["id"]), True)["n"]
    last_review = q("SELECT * FROM eho_four_week_reviews WHERE organisation_id=? AND site_id=? ORDER BY review_date DESC,id DESC LIMIT 1", (u["organisation_id"], site["id"]), True)
    review_due = True
    if last_review:
        d = _parse_iso_date(last_review.get("review_date"))
        review_due = not d or date.today() >= d + timedelta(days=28)
    completed = int(bool(opening)) + int(bool(closing))
    daily_percent = int(round(completed / 2 * 100))
    recent = q("SELECT * FROM eho_records WHERE organisation_id=? AND site_id=? ORDER BY record_date DESC,id DESC LIMIT 12", (u["organisation_id"], site["id"]))
    return jsonify(
        date=today,
        opening_complete=bool(opening), closing_complete=bool(closing), daily_percent=daily_percent,
        temperature_checks=int(temp_today or 0), temperature_breaches=int(breaches or 0), open_actions=int(open_actions or 0),
        four_week_review_due=review_due, last_review=dict(last_review) if last_review else None,
        opening_items=OPENING_CHECKS, closing_items=CLOSING_CHECKS, allergens=ALLERGENS_14,
        temperature_presets=TEMP_PRESETS, categories=EHO_CATEGORIES,
        recent=[({**dict(x), "details": "Restricted manager record", "corrective_action": ""} if x.get("category") == "Fit to work" and u["role"] not in ("Owner","Admin","General Manager","Manager") else dict(x)) for x in recent],
    )


@app.post("/api/eho/daily-check")
@login_required
def save_eho_daily_check():
    u, site = _eho_scope()
    d = request.get_json() or {}
    check_type = str(d.get("check_type") or "").strip().title()
    if check_type not in ("Opening", "Closing"):
        return jsonify(error="Check type must be Opening or Closing"), 400
    expected = OPENING_CHECKS if check_type == "Opening" else CLOSING_CHECKS
    answers = d.get("answers") or {}
    if not isinstance(answers, dict):
        return jsonify(error="Invalid checklist answers"), 400
    missing = [item for item in expected if item not in answers]
    if missing:
        return jsonify(error="Every checklist item must be answered"), 400
    failed = [item for item in expected if answers.get(item) is not True]
    problems = str(d.get("problems") or "").strip()
    corrective = str(d.get("corrective_action") or "").strip()
    if failed and (not problems or not corrective):
        return jsonify(error="For any failed check, record the problem and corrective action before signing"), 400
    row_id = execute(
        """INSERT INTO eho_daily_checks(organisation_id,site_id,check_date,check_type,answers_json,problems,corrective_action,signed_by,signed_name,completed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"], site["id"], date.today().isoformat(), check_type, json.dumps(answers), problems, corrective, u["id"], u["name"], now()),
    )
    if failed:
        execute(
            """INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,created_by,created_name,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (u["organisation_id"], site["id"], "HACCP/SFBB", date.today().isoformat(), f"{check_type} check exception", "Action required", "; ".join(failed) + (f" | {problems}" if problems else ""), corrective, u["id"], u["name"], now()),
        )
    audit("Completed", "eho_daily_check", row_id, f"{check_type} checks signed by {u['name']}")
    return jsonify(ok=True, id=row_id, failed=failed)


@app.get("/api/eho/daily-checks")
@login_required
def get_eho_daily_checks():
    u, site = _eho_scope()
    start = request.args.get("start") or (date.today() - timedelta(days=28)).isoformat()
    end = request.args.get("end") or date.today().isoformat()
    rows = q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,id DESC", (u["organisation_id"], site["id"], start, end))
    out=[]
    for r in rows:
        x=dict(r); x["answers"]=_json_load(x.pop("answers_json", "{}")); out.append(x)
    return jsonify(checks=out)


@app.post("/api/eho/temperature")
@login_required
def save_eho_temperature():
    u, site = _eho_scope()
    d=request.get_json() or {}
    check_type=str(d.get("check_type") or "Other").strip()
    item=str(d.get("item") or "").strip()
    if not item:
        return jsonify(error="Enter the fridge, food, delivery or equipment checked"),400
    try:
        temp=float(d.get("temperature"))
        target_min=None if d.get("target_min") in (None,"") else float(d.get("target_min"))
        target_max=None if d.get("target_max") in (None,"") else float(d.get("target_max"))
    except Exception:
        return jsonify(error="Enter a valid temperature and limits"),400
    preset=TEMP_PRESETS.get(check_type,{})
    if target_min is None and preset.get("min") is not None: target_min=float(preset["min"])
    if target_max is None and preset.get("max") is not None: target_max=float(preset["max"])
    result="OK"
    if (target_min is not None and temp < target_min) or (target_max is not None and temp > target_max): result="Action required"
    corrective=str(d.get("corrective_action") or "").strip()
    if result=="Action required" and not corrective:
        return jsonify(error="Temperature is outside the recorded limit. Enter the corrective action taken."),400
    check_date=str(d.get("check_date") or date.today().isoformat())
    check_time=str(d.get("check_time") or datetime.now().strftime("%H:%M"))
    row_id=execute("""INSERT INTO eho_temperature_checks(organisation_id,site_id,check_date,check_time,check_type,item,temperature,target_min,target_max,result,method,corrective_action,recorded_by,recorded_name,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"],site["id"],check_date,check_time,check_type,item,temp,target_min,target_max,result,str(d.get("method") or "").strip(),corrective,u["id"],u["name"],now()))
    if result=="Action required":
        execute("""INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,created_by,created_name,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (u["organisation_id"],site["id"],"HACCP/SFBB",check_date,f"Temperature exception: {item}","Action required",f"{check_type}: {temp}Â°C",corrective,u["id"],u["name"],now()))
    audit("Recorded", "eho_temperature", row_id, f"{check_type} Â· {item} Â· {temp}Â°C Â· {result}")
    return jsonify(ok=True,id=row_id,result=result)


@app.get("/api/eho/temperatures")
@login_required
def get_eho_temperatures():
    u,site=_eho_scope(); start=request.args.get("start") or (date.today()-timedelta(days=28)).isoformat(); end=request.args.get("end") or date.today().isoformat()
    rows=q("SELECT * FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,check_time DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    return jsonify(temperatures=[dict(x) for x in rows])


@app.post("/api/eho/record")
@login_required
def save_eho_record():
    u,site=_eho_scope(); d=request.get_json() or {}
    category=str(d.get("category") or "Other").strip()
    if category not in EHO_CATEGORIES: return jsonify(error="Invalid compliance category"),400
    title=str(d.get("title") or "").strip()
    if not title: return jsonify(error="Enter a record title"),400
    status=str(d.get("status") or "Complete").strip()
    details=str(d.get("details") or "").strip(); corrective=str(d.get("corrective_action") or "").strip()
    if status in ("Action required","Open","Overdue") and not corrective:
        return jsonify(error="Open/action-required records need a corrective action or next step"),400
    metadata=d.get("metadata") or {}
    if not isinstance(metadata,dict): metadata={}
    row_id=execute("""INSERT INTO eho_records(organisation_id,site_id,category,record_date,title,status,details,corrective_action,due_date,reference,metadata_json,created_by,created_name,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"],site["id"],category,str(d.get("record_date") or date.today().isoformat()),title,status,details,corrective,str(d.get("due_date") or ""),str(d.get("reference") or ""),json.dumps(metadata),u["id"],u["name"],now()))
    audit("Recorded", "eho_record", row_id, f"{category}: {title} ({status})")
    return jsonify(ok=True,id=row_id)


@app.get("/api/eho/records")
@login_required
def get_eho_records():
    u,site=_eho_scope(); start=request.args.get("start") or (date.today()-timedelta(days=90)).isoformat(); end=request.args.get("end") or date.today().isoformat(); category=(request.args.get("category") or "").strip()
    args=[u["organisation_id"],site["id"],start,end]; sql="SELECT * FROM eho_records WHERE organisation_id=? AND site_id=? AND record_date BETWEEN ? AND ?"
    if category: sql+=" AND category=?"; args.append(category)
    sql+=" ORDER BY record_date DESC,id DESC"
    rows=q(sql,tuple(args)); out=[]
    for r in rows:
        x=dict(r); x["metadata"]=_json_load(x.pop("metadata_json","{}"))
        if x.get("category") == "Fit to work" and u["role"] not in ("Owner","Admin","General Manager","Manager"):
            x["details"] = "Restricted manager record"; x["corrective_action"] = ""; x["reference"] = ""
        out.append(x)
    return jsonify(records=out)


@app.post("/api/eho/record/<int:record_id>/resolve")
@login_required
@manager_required
def resolve_eho_record(record_id):
    u,site=_eho_scope(); d=request.get_json() or {}
    row=q("SELECT * FROM eho_records WHERE id=? AND organisation_id=? AND site_id=?",(record_id,u["organisation_id"],site["id"]),True)
    if not row:return jsonify(error="Compliance record not found"),404
    resolution=str(d.get("resolution") or "").strip()
    if not resolution:return jsonify(error="Record how the issue was resolved"),400
    # Preserve the original record and append the resolution to the action field.
    action=(str(row.get("corrective_action") or "").strip()+" | RESOLVED: "+resolution).strip(" |")
    execute("UPDATE eho_records SET status='Complete',corrective_action=?,verified_by=?,verified_at=? WHERE id=?",(action,u["id"],now(),record_id))
    audit("Resolved", "eho_record", record_id, resolution)
    return jsonify(ok=True)


@app.post("/api/eho/record/<int:record_id>/verify")
@login_required
@manager_required
def verify_eho_record(record_id):
    u,site=_eho_scope(); row=q("SELECT * FROM eho_records WHERE id=? AND organisation_id=? AND site_id=?",(record_id,u["organisation_id"],site["id"]),True)
    if not row:return jsonify(error="Compliance record not found"),404
    execute("UPDATE eho_records SET verified_by=?,verified_at=? WHERE id=?",(u["id"],now(),record_id))
    audit("Verified", "eho_record", record_id, f"Verified by {u['name']}")
    return jsonify(ok=True)


@app.post("/api/eho/four-week-review")
@login_required
@manager_required
def save_four_week_review():
    u,site=_eho_scope(); d=request.get_json() or {}; end=_parse_iso_date(d.get("period_end")) or date.today(); start=_parse_iso_date(d.get("period_start")) or (end-timedelta(days=27))
    row_id=execute("""INSERT INTO eho_four_week_reviews(organisation_id,site_id,review_date,period_start,period_end,persistent_problems,changes_made,safe_methods_current,allergen_info_current,training_current,signed_by,signed_name,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (u["organisation_id"],site["id"],date.today().isoformat(),start.isoformat(),end.isoformat(),str(d.get("persistent_problems") or ""),str(d.get("changes_made") or ""),1 if d.get("safe_methods_current",True) else 0,1 if d.get("allergen_info_current",True) else 0,1 if d.get("training_current",True) else 0,u["id"],u["name"],now()))
    audit("Completed", "eho_four_week_review", row_id, f"4-week review {start.isoformat()} to {end.isoformat()}")
    return jsonify(ok=True,id=row_id)


@app.get("/api/eho/audit-pack")
@login_required
@manager_required
def eho_audit_pack():
    u,site=_eho_scope(); start=request.args.get("start") or (date.today()-timedelta(days=28)).isoformat(); end=request.args.get("end") or date.today().isoformat()
    daily=q("SELECT * FROM eho_daily_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    temps=q("SELECT * FROM eho_temperature_checks WHERE organisation_id=? AND site_id=? AND check_date BETWEEN ? AND ? ORDER BY check_date DESC,check_time DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    records=q("SELECT * FROM eho_records WHERE organisation_id=? AND site_id=? AND record_date BETWEEN ? AND ? ORDER BY record_date DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    reviews=q("SELECT * FROM eho_four_week_reviews WHERE organisation_id=? AND site_id=? AND review_date BETWEEN ? AND ? ORDER BY review_date DESC,id DESC",(u["organisation_id"],site["id"],start,end))
    daily_out=[]
    for r in daily:
        x=dict(r); x["answers"]=_json_load(x.pop("answers_json","{}")); daily_out.append(x)
    rec_out=[]
    for r in records:
        x=dict(r); x["metadata"]=_json_load(x.pop("metadata_json","{}")); rec_out.append(x)
    return jsonify(site=dict(site),organisation=dict(org()),start=start,end=end,daily_checks=daily_out,temperatures=[dict(x) for x in temps],records=rec_out,reviews=[dict(x) for x in reviews])


@app.get("/api/health")
@app.get("/health")
def health():
    try:
        row = q("SELECT 1 AS ok", one=True)
        database_ok = bool(row and row["ok"] == 1)
    except Exception:
        database_ok = False
    return jsonify(
        status="ok" if database_ok else "database_error",
        service="OrderFlow",
        database="PostgreSQL",
        database_connected=database_ok,
    ), (200 if database_ok else 503)


WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail"
}


def weather_payload(latitude, longitude, location_label="Your location"):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset",
        "forecast_days": 7,
        "timezone": "auto"
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    d = r.json()
    current = d.get("current", {})
    daily = d.get("daily", {})
    days = []
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    rain_prob = daily.get("precipitation_probability_max", [])
    rain_sum = daily.get("precipitation_sum", [])
    sunrise = daily.get("sunrise", [])
    sunset = daily.get("sunset", [])
    for i, day_value in enumerate(dates):
        code = codes[i] if i < len(codes) else None
        days.append({
            "date": day_value,
            "code": code,
            "description": WEATHER_CODES.get(code, "Weather"),
            "high": highs[i] if i < len(highs) else None,
            "low": lows[i] if i < len(lows) else None,
            "rain_probability": rain_prob[i] if i < len(rain_prob) else None,
            "rain_mm": rain_sum[i] if i < len(rain_sum) else None,
            "sunrise": sunrise[i] if i < len(sunrise) else None,
            "sunset": sunset[i] if i < len(sunset) else None
        })
    return {
        "location": location_label,
        "timezone": d.get("timezone"),
        "latitude": latitude,
        "longitude": longitude,
        "current": {
            "temperature": current.get("temperature_2m"),
            "apparent_temperature": current.get("apparent_temperature"),
            "wind_speed": current.get("wind_speed_10m"),
            "precipitation": current.get("precipitation"),
            "code": current.get("weather_code"),
            "description": WEATHER_CODES.get(current.get("weather_code"), "Weather")
        },
        "daily": days,
        "source": "Open-Meteo"
    }


@app.get("/api/weather")
@login_required
def weather():
    s = current_site()
    try:
        lat = float(request.args.get("lat")) if request.args.get("lat") else None
        lon = float(request.args.get("lon")) if request.args.get("lon") else None
    except Exception:
        return jsonify(error="Invalid location coordinates"), 400

    location_label = "Your location"
    if lat is None or lon is None:
        address = (s["address"] if s else "") or (s["name"] if s else "")
        if not address:
            return jsonify(error="Location unavailable. Allow location access or add a site address."), 400
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        try:
            gr = requests.get(
                geo_url,
                params={"name": address, "count": 1, "language": "en", "format": "json"},
                timeout=10,
            )
            gr.raise_for_status()
            results = gr.json().get("results") or []
            if not results:
                return jsonify(
                    error="Could not find the site location. Allow browser location access or update the site address."
                ), 400
            place = results[0]
            lat = float(place["latitude"])
            lon = float(place["longitude"])
            location_label = ", ".join(
                x for x in [place.get("name"), place.get("admin1"), place.get("country")] if x
            )
        except Exception as exc:
            return jsonify(error=f"Weather service unavailable: {exc}"), 502

    try:
        return jsonify(weather_payload(lat, lon, location_label))
    except requests.RequestException:
        return jsonify(error="Weather service is temporarily unavailable."), 502
    except Exception:
        return jsonify(error="Unable to load weather right now."), 500


@app.get("/api/events")
@login_required
def get_events():
    u, s = user(), current_site()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    result = q(
        """SELECT e.*,u.name created_by_name FROM events e
           LEFT JOIN users u ON u.id=e.created_by
           WHERE e.organisation_id=? AND e.site_id=? AND e.event_date>=? AND e.event_date<?
           ORDER BY e.event_date,e.start_time,e.id""",
        (u["organisation_id"], s["id"], start, end),
    )
    return jsonify(events=[dict(x) for x in result], month=month)


@app.post("/api/events")
@login_required
@manager_required
def add_event():
    u, s = user(), current_site()
    d = request.get_json() or {}
    title = (d.get("title") or "").strip()
    event_date = (d.get("event_date") or "").strip()
    event_type = (d.get("event_type") or "General").strip()
    start_time = (d.get("start_time") or "").strip()
    end_time = (d.get("end_time") or "").strip()
    notes = (d.get("notes") or "").strip()
    all_day = 1 if d.get("all_day") else 0
    if not title or not event_date:
        return jsonify(error="Event title and date are required"), 400
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
        if start_time:
            datetime.strptime(start_time, "%H:%M")
        if end_time:
            datetime.strptime(end_time, "%H:%M")
    except ValueError:
        return jsonify(error="Use a valid date and time"), 400
    event_id = execute(
        """INSERT INTO events(organisation_id,site_id,title,event_date,start_time,end_time,event_type,notes,all_day,created_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            u["organisation_id"],
            s["id"],
            title,
            event_date,
            start_time,
            end_time,
            event_type,
            notes,
            all_day,
            u["id"],
            now(),
        ),
    )
    audit("Created", "event", event_id, title)
    return jsonify(ok=True, id=event_id)


@app.delete("/api/events/<int:event_id>")
@login_required
@manager_required
def delete_event(event_id):
    u, s = user(), current_site()
    event = q(
        "SELECT * FROM events WHERE id=? AND organisation_id=? AND site_id=?",
        (event_id, u["organisation_id"], s["id"]),
        True,
    )
    if not event:
        return jsonify(error="Event not found"), 404
    execute("DELETE FROM events WHERE id=?", (event_id,))
    audit("Deleted", "event", event_id, event["title"])
    return jsonify(ok=True)


# -----------------------------------------------------------------------------
# ORDERFLOW COMPANY ADMIN (HQ)
# -----------------------------------------------------------------------------

def company_admin_logged_in():
    return bool(session.get("company_admin"))


def company_admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not company_admin_logged_in():
            if request.path.startswith("/api/company-admin/"):
                return jsonify(error="Company admin authentication required"), 401
            return redirect(url_for("company_admin_login"))
        return fn(*args, **kwargs)
    return wrapped


def company_event(event_type, detail="", organisation_id=None):
    execute(
        "INSERT INTO company_events(organisation_id,event_type,detail,created_at) VALUES(?,?,?,?)",
        (organisation_id, event_type, detail, now()),
    )


@app.route("/company-admin/login", methods=["GET", "POST"])
def company_admin_login():
    if company_admin_logged_in():
        return redirect(url_for("company_admin_home"))
    error = None
    if request.method == "POST":
        import hmac
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        expected_email = os.environ.get("COMPANY_ADMIN_EMAIL", "").strip().lower()
        expected_password = os.environ.get("COMPANY_ADMIN_PASSWORD", "")
        if expected_email and expected_password and hmac.compare_digest(email, expected_email) and hmac.compare_digest(password, expected_password):
            session.clear()
            session["company_admin"] = True
            session["company_admin_email"] = email
            return redirect(url_for("company_admin_home"))
        error = "Incorrect company admin email or password."
    return render_template("company_admin_login.html", error=error)


@app.post("/company-admin/logout")
def company_admin_logout():
    session.clear()
    return redirect(url_for("company_admin_login"))


@app.get("/company-admin")
@company_admin_required
def company_admin_home():
    return render_template("company_admin.html", admin_email=session.get("company_admin_email", "OrderFlow Admin"))


@app.get("/api/company-admin/overview")
@company_admin_required
def company_admin_overview():
    orgs = q("SELECT COUNT(*) AS n FROM organisations", one=True)["n"]
    sites = q("SELECT COUNT(*) AS n FROM sites WHERE active=1", one=True)["n"]
    users_count = q("SELECT COUNT(*) AS n FROM users WHERE active=1", one=True)["n"]
    active = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Active'", one=True)["n"]
    trials = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Trial'", one=True)["n"]
    payment_required = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Payment required'", one=True)["n"]
    overdue = q("SELECT COUNT(*) AS n FROM subscriptions WHERE status='Past due'", one=True)["n"]
    mrr = float(q("SELECT COALESCE(SUM(monthly_price),0) AS x FROM subscriptions WHERE status='Active'", one=True)["x"] or 0)
    collected = float(q("SELECT COALESCE(SUM(amount),0) AS x FROM subscription_payments WHERE status='Paid'", one=True)["x"] or 0)
    recent = q("""SELECT ce.*, o.name organisation_name FROM company_events ce
                  LEFT JOIN organisations o ON o.id=ce.organisation_id
                  ORDER BY ce.id DESC LIMIT 12""")
    return jsonify(
        organisations=orgs, sites=sites, users=users_count, active=active, trials=trials,
        payment_required=payment_required, overdue=overdue, mrr=mrr, arr=mrr*12, collected=collected,
        recent_events=[dict(x) for x in recent],
    )


@app.get("/api/company-admin/customers")
@company_admin_required
def company_admin_customers():
    rows = q("""SELECT o.id,o.name,o.business_type,o.created_at,
                COALESCE(s.plan,'Starter') plan,COALESCE(s.monthly_price,0) monthly_price,
                COALESCE(s.status,'Trial') subscription_status,COALESCE(s.trial_end,'') trial_end,
                COALESCE(s.next_billing_date,'') next_billing_date,
                COALESCE(s.contract_start,'') contract_start,COALESCE(s.contract_end,'') contract_end,
                COALESCE(s.contracted_sites,1) contracted_sites,COALESCE(s.contracted_users,3) contracted_users,
                COALESCE(s.base_price,250) base_price,COALESCE(s.extra_user_price,20) extra_user_price,
                (SELECT COUNT(*) FROM sites st WHERE st.organisation_id=o.id AND st.active=1) site_count,
                (SELECT COUNT(*) FROM users u WHERE u.organisation_id=o.id AND u.active=1) user_count,
                (SELECT COALESCE(SUM(sp.amount),0) FROM subscription_payments sp WHERE sp.organisation_id=o.id AND sp.status='Paid') total_paid
                FROM organisations o LEFT JOIN subscriptions s ON s.organisation_id=o.id
                ORDER BY o.id DESC""")
    return jsonify(customers=[dict(x) for x in rows])


@app.get("/api/company-admin/customers/<int:organisation_id>")
@company_admin_required
def company_admin_customer(organisation_id):
    customer = q("""SELECT o.*,s.plan,s.monthly_price,s.status subscription_status,s.trial_end,s.next_billing_date,
                    s.notes subscription_notes,s.contract_start,s.contract_end,s.contract_months,s.contracted_sites,
                    s.contracted_users,s.base_price,s.included_users_per_site,s.extra_user_price,s.vat_rate,
                    s.last_payment_status,s.last_payment_at,s.past_due_since
                    FROM organisations o LEFT JOIN subscriptions s ON s.organisation_id=o.id WHERE o.id=?""", (organisation_id,), True)
    if not customer:
        return jsonify(error="Customer not found"), 404
    sites = q("SELECT id,name,address,active,created_at FROM sites WHERE organisation_id=? ORDER BY id", (organisation_id,))
    users_rows = q("SELECT id,name,email,role,active,created_at FROM users WHERE organisation_id=? ORDER BY id", (organisation_id,))
    payments = q("SELECT * FROM subscription_payments WHERE organisation_id=? ORDER BY payment_date DESC,id DESC", (organisation_id,))
    return jsonify(customer=dict(customer), pricing=pricing_for(organisation_id), sites=[dict(x) for x in sites], users=[dict(x) for x in users_rows], payments=[dict(x) for x in payments])


@app.post("/api/company-admin/subscriptions/<int:organisation_id>")
@company_admin_required
def company_admin_update_subscription(organisation_id):
    if not q("SELECT id FROM organisations WHERE id=?", (organisation_id,), True):
        return jsonify(error="Customer not found"), 404
    d = request.get_json() or {}
    plan = (d.get("plan") or "Starter").strip()[:50]
    status = (d.get("status") or "Trial").strip()
    if status not in ("Trial", "Payment required", "Active", "Past due", "Suspended", "Cancelled"):
        return jsonify(error="Invalid subscription status"), 400
    current = subscription_for(organisation_id)
    pricing = pricing_for(organisation_id)
    # During an active 12-month minimum term the agreed unit rates are locked.
    # Company admin can change status/notes, but not silently re-price the contract.
    if current and pricing["inside_term"]:
        monthly_price = pricing["net"]
    else:
        try:
            monthly_price = max(0, float(d.get("monthly_price", pricing["net"])))
        except Exception:
            return jsonify(error="Invalid monthly price"), 400
    trial_end = (d.get("trial_end") or "")[:20]
    next_billing_date = (d.get("next_billing_date") or "")[:20]
    notes = (d.get("notes") or "")[:1000]
    execute("""INSERT INTO subscriptions(organisation_id,plan,monthly_price,status,trial_end,next_billing_date,started_at,notes)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT (organisation_id) DO UPDATE SET
               plan=EXCLUDED.plan,monthly_price=EXCLUDED.monthly_price,status=EXCLUDED.status,
               trial_end=EXCLUDED.trial_end,next_billing_date=EXCLUDED.next_billing_date,notes=EXCLUDED.notes""",
            (organisation_id, plan, monthly_price, status, trial_end, next_billing_date, now(), notes))
    company_event("Subscription updated", f"{plan} Â· Â£{monthly_price:.2f}/month Â· {status}", organisation_id)
    return jsonify(ok=True)


@app.post("/api/company-admin/payments")
@company_admin_required
def company_admin_add_payment():
    d = request.get_json() or {}
    try:
        organisation_id = int(d.get("organisation_id"))
        amount = float(d.get("amount"))
    except Exception:
        return jsonify(error="Customer and valid payment amount are required"), 400
    if amount < 0 or not q("SELECT id FROM organisations WHERE id=?", (organisation_id,), True):
        return jsonify(error="Invalid payment"), 400
    payment_date = (d.get("payment_date") or date.today().isoformat())[:20]
    status = (d.get("status") or "Paid")[:30]
    method = (d.get("method") or "")[:80]
    reference = (d.get("reference") or "")[:120]
    notes = (d.get("notes") or "")[:500]
    pid = execute("""INSERT INTO subscription_payments(organisation_id,amount,payment_date,status,method,reference,notes,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""", (organisation_id, amount, payment_date, status, method, reference, notes, now()))
    company_event("Payment recorded", f"Â£{amount:.2f} Â· {status}", organisation_id)
    return jsonify(ok=True, id=pid)


@app.get("/api/company-admin/payments")
@company_admin_required
def company_admin_payments():
    rows = q("""SELECT sp.*,o.name organisation_name FROM subscription_payments sp
                JOIN organisations o ON o.id=sp.organisation_id ORDER BY sp.payment_date DESC,sp.id DESC LIMIT 500""")
    return jsonify(payments=[dict(x) for x in rows])


@app.get("/api/company-admin/users")
@company_admin_required
def company_admin_users():
    rows = q("""SELECT u.id,u.name,u.email,u.role,u.active,u.created_at,o.name organisation_name
                FROM users u JOIN organisations o ON o.id=u.organisation_id ORDER BY u.id DESC LIMIT 1000""")
    return jsonify(users=[dict(x) for x in rows])


@app.get("/api/company-admin/events")
@company_admin_required
def company_admin_events():
    rows = q("""SELECT ce.*,o.name organisation_name FROM company_events ce
                LEFT JOIN organisations o ON o.id=ce.organisation_id ORDER BY ce.id DESC LIMIT 500""")
    return jsonify(events=[dict(x) for x in rows])


MARKET_INSTRUMENTS = [
    {"name": "Wheat", "symbol": "W_1", "category": "Grain", "unit": "USD / contract"},
    {"name": "Corn", "symbol": "C_1", "category": "Grain", "unit": "USD / contract"},
    {"name": "Sugar", "symbol": "SB1", "category": "Sugar", "unit": "USD / contract"},
    {"name": "Coffee", "symbol": "KC1", "category": "Coffee", "unit": "USD / contract"},
    {"name": "Cocoa", "symbol": "CC1", "category": "Cocoa", "unit": "USD / contract"},
    {"name": "Orange Juice", "symbol": "OJ1", "category": "Fruit", "unit": "USD / contract"},
    {"name": "Brent Crude", "symbol": "BRENT/USD", "category": "Energy", "unit": "USD / barrel"}
]


@app.get("/api/market")
@login_required
def market():
    api_key = os.environ.get("MARKET_API_KEY", "").strip()
    if not api_key:
        return jsonify(
            configured=False,
            provider="Twelve Data",
            message="Add MARKET_API_KEY to enable live market prices.",
            instruments=MARKET_INSTRUMENTS,
        )

    rows = []
    errors = []

    for item in MARKET_INSTRUMENTS:
        try:
            r = requests.get(
                "https://api.twelvedata.com/price",
                params={"symbol": item["symbol"], "apikey": api_key},
                timeout=8,
            )
            data = r.json()
            if r.ok and data.get("price") is not None:
                rows.append({
                    **item,
                    "price": float(data["price"]),
                    "timestamp": data.get("timestamp"),
                    "status": "Live",
                })
            else:
                errors.append(item["name"])
                rows.append({**item, "price": None, "status": "Unavailable"})
        except Exception:
            errors.append(item["name"])
            rows.append({**item, "price": None, "status": "Unavailable"})

    return jsonify(
        configured=True,
        provider="Twelve Data",
        instruments=rows,
        errors=errors,
        updated_at=now(),
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
