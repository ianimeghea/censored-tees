import hmac
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from functools import wraps

import requests as http_requests
from dotenv import load_dotenv

load_dotenv()

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

import storage
from printify_client import PrintifyClient, PrintifyError
from translations import TRANSLATIONS

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-key-change-me")

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if os.environ.get("APP_BASE_URL", "").startswith("https"):
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

client = PrintifyClient()

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
PAYPAL_SANDBOX = os.environ.get("PAYPAL_SANDBOX", "true").lower() in ("1", "true", "yes")
PAYPAL_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_SANDBOX else "https://api-m.paypal.com"

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

def _parse_id_list(val):
    """Parse a comma-separated env var into a set of stripped, non-empty strings."""
    if not val:
        return set()
    return {pid.strip() for pid in val.split(",") if pid.strip()}

PRODUCTS_RO_ONLY = _parse_id_list(os.environ.get("PRODUCTS_RO_ONLY", ""))
PRODUCTS_INT_ONLY = _parse_id_list(os.environ.get("PRODUCTS_INT_ONLY", ""))

storage.init_db()


# ── PayPal helpers ──────────────────────────────────────────────────────────

def paypal_configured():
    return bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)


def _paypal_access_token():
    try:
        resp = http_requests.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        app.logger.error("PayPal auth failed: %s", exc)
        return None


def _paypal_headers():
    token = _paypal_access_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _paypal_create_order(purchase_units):
    headers = _paypal_headers()
    if not headers:
        return None
    resp = http_requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers=headers,
        json={"intent": "CAPTURE", "purchase_units": purchase_units},
        timeout=20,
    )
    if not resp.ok:
        app.logger.error("PayPal create order failed: %s %s", resp.status_code, resp.text)
        return None
    return resp.json()


def _paypal_capture_order(order_id):
    headers = _paypal_headers()
    if not headers:
        return None
    resp = http_requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
        headers=headers,
        json={},
        timeout=20,
    )
    if not resp.ok:
        app.logger.error("PayPal capture failed: %s %s", resp.status_code, resp.text)
        return None
    return resp.json()


def _cents_to_paypal(cents):
    """Convert integer cents (e.g. 1988) to PayPal dollar string (e.g. '19.88')."""
    return f"{Decimal(cents) / Decimal(100):.2f}"


# ── Auth & utils ────────────────────────────────────────────────────────────

def _check_admin_auth(auth):
    if not ADMIN_USER or not ADMIN_PASSWORD:
        return False
    if not auth:
        return False
    user_ok = hmac.compare_digest(auth.username or "", ADMIN_USER)
    pass_ok = hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)
    return user_ok and pass_ok


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not ADMIN_USER or not ADMIN_PASSWORD:
            return Response(
                "Admin login isn't configured. Set ADMIN_USER and ADMIN_PASSWORD in .env.",
                status=503,
            )
        if not _check_admin_auth(request.authorization):
            return Response(
                "Authentication required.",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Admin"'},
            )
        return view(*args, **kwargs)
    return wrapper


def cart():
    return session.setdefault("cart", [])


def save_cart(items):
    session["cart"] = items
    session.modified = True


def cents_to_str(cents):
    if cents is None:
        return "0.00"
    return f"{Decimal(cents) / Decimal(100):.2f}"


app.jinja_env.filters["money"] = cents_to_str


def get_lang():
    if "lang" in session:
        return session["lang"]
    return request.accept_languages.best_match(["ro", "en"], default="en")


def _(text):
    lang = get_lang()
    if lang == "en":
        return text
    return TRANSLATIONS.get(lang, {}).get(text, text)


app.jinja_env.globals["_"] = _
app.jinja_env.globals["get_lang"] = get_lang


def product_visible_for_region(product):
    """Check if a product should be shown for the current region.

    Configure via env vars (comma-separated Printify product IDs):
      PRODUCTS_RO_ONLY  -> only shown to Romanian visitors
      PRODUCTS_INT_ONLY -> only shown to international visitors
    Products in neither list are shown to everyone.
    """
    pid = str(product.get("id", ""))
    is_ro = get_lang() == "ro"
    if pid in PRODUCTS_RO_ONLY:
        return is_ro
    if pid in PRODUCTS_INT_ONLY:
        return not is_ro
    return True


def first_image(product):
    images = product.get("images") or []
    default = next((img for img in images if img.get("is_default")), None)
    return (default or (images[0] if images else {})).get("src")


def enabled_variants(product):
    return [v for v in product.get("variants", []) if v.get("is_enabled")]


def find_variant(product, variant_id):
    for variant in product.get("variants", []):
        if variant.get("id") == variant_id:
            return variant
    return None


def variant_image(product, variant_id):
    images = product.get("images") or []
    matches = [img for img in images if variant_id in (img.get("variant_ids") or [])]
    if matches:
        default = next((img for img in matches if img.get("is_default")), matches[0])
        return default.get("src")
    return first_image(product)


def cart_summary():
    items = cart()
    if not items:
        return [], 0

    detailed = []
    subtotal = 0
    dirty = False
    surviving = []

    for entry in items:
        try:
            product = client.get_product(entry["product_id"])
        except PrintifyError:
            dirty = True
            continue

        variant = find_variant(product, entry["variant_id"])
        if not variant or not variant.get("is_enabled"):
            dirty = True
            continue

        line_total = variant["price"] * entry["quantity"]
        subtotal += line_total
        detailed.append({
            "product": product,
            "variant": variant,
            "quantity": entry["quantity"],
            "line_total": line_total,
            "image": variant_image(product, variant["id"]),
            "key": entry["key"],
        })
        surviving.append(entry)

    if dirty:
        save_cart(surviving)

    return detailed, subtotal


@app.context_processor
def inject_globals():
    return {
        "cart_count": sum(item.get("quantity", 0) for item in cart()),
        "shop_configured": client.is_configured,
        "now_year": datetime.utcnow().year,
        "paypal_sandbox": PAYPAL_SANDBOX,
        "lang": get_lang(),
    }


@app.errorhandler(PrintifyError)
def handle_printify_error(error):
    return render_template("error.html", message=str(error)), 502


@app.route("/healthz")
def healthz():
    return ("ok", 200, {"Content-Type": "text/plain"})


@app.route("/lang/<lang>")
def set_language(lang):
    if lang in ("ro", "en"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("index"))


@app.route("/")
def index():
    if not client.is_configured:
        return render_template("setup.html")

    page = max(int(request.args.get("page", 1)), 1)
    data = client.list_products(page=page, limit=24)
    products = []
    for raw in data.get("data", []):
        if not raw.get("visible", True):
            continue
        if not product_visible_for_region(raw):
            continue
        variants = enabled_variants(raw)
        if not variants:
            continue
        prices = [v["price"] for v in variants]
        products.append({
            "id": raw["id"],
            "title": raw["title"],
            "image": first_image(raw),
            "price_from": min(prices),
            "price_to": max(prices),
        })

    return render_template(
        "index.html",
        products=products,
        page=data.get("current_page", page),
        last_page=data.get("last_page", page),
        total=data.get("total", len(products)),
    )


@app.route("/product/<product_id>")
def product_detail(product_id):
    product = client.get_product(product_id)
    if not product_visible_for_region(product):
        abort(404)
    variants = enabled_variants(product)
    if not variants:
        abort(404)

    return render_template(
        "product.html",
        product=product,
        variants=variants,
        default_variant=next((v for v in variants if v.get("is_default")), variants[0]),
        first_image=first_image(product),
    )


@app.route("/cart/add", methods=["POST"])
def cart_add():
    product_id = request.form.get("product_id")
    variant_id = request.form.get("variant_id")
    quantity = max(int(request.form.get("quantity", 1)), 1)

    if not product_id or not variant_id:
        flash("Please select a variant.", "error")
        return redirect(request.referrer or url_for("index"))

    variant_id = int(variant_id)

    items = cart()
    for entry in items:
        if entry["product_id"] == product_id and entry["variant_id"] == variant_id:
            entry["quantity"] += quantity
            break
    else:
        items.append({
            "key": uuid.uuid4().hex,
            "product_id": product_id,
            "variant_id": variant_id,
            "quantity": quantity,
        })

    save_cart(items)
    flash("Added to cart.", "success")
    return redirect(url_for("cart_view"))


@app.route("/cart/remove", methods=["POST"])
def cart_remove():
    key = request.form.get("key")
    items = [entry for entry in cart() if entry["key"] != key]
    save_cart(items)
    return redirect(url_for("cart_view"))


@app.route("/cart/update", methods=["POST"])
def cart_update():
    key = request.form.get("key")
    quantity = max(int(request.form.get("quantity", 1)), 1)
    for entry in cart():
        if entry["key"] == key:
            entry["quantity"] = quantity
            break
    save_cart(cart())
    return redirect(url_for("cart_view"))


@app.route("/cart")
def cart_view():
    items, subtotal = cart_summary()
    return render_template("cart.html", items=items, subtotal=subtotal)


def address_from_json(data):
    return {
        "first_name": (data.get("first_name") or "").strip(),
        "last_name": (data.get("last_name") or "").strip(),
        "email": (data.get("email") or "").strip(),
        "phone": (data.get("phone") or "").strip(),
        "country": (data.get("country") or "").strip().upper(),
        "region": (data.get("region") or "").strip(),
        "address1": (data.get("address1") or "").strip(),
        "address2": (data.get("address2") or "").strip(),
        "city": (data.get("city") or "").strip(),
        "zip": (data.get("zip") or "").strip(),
    }


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items, subtotal = cart_summary()
    if not items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("index"))

    if not paypal_configured():
        return render_template(
            "error.html",
            message="Payments aren't configured yet. Add your PayPal keys to .env and restart.",
        ), 503

    shipping_cost = None
    form = {}

    if request.method == "POST":
        form = request.form
        try:
            address = {
                "first_name": form["first_name"].strip(),
                "last_name": form["last_name"].strip(),
                "email": form["email"].strip(),
                "phone": form.get("phone", "").strip(),
                "country": form["country"].strip().upper(),
                "region": form.get("region", "").strip(),
                "address1": form["address1"].strip(),
                "address2": form.get("address2", "").strip(),
                "city": form["city"].strip(),
                "zip": form["zip"].strip(),
            }
        except KeyError as exc:
            flash(f"Missing field: {exc.args[0]}", "error")
            return redirect(url_for("checkout"))

        line_items = [
            {
                "product_id": entry["product"]["id"],
                "variant_id": entry["variant"]["id"],
                "quantity": entry["quantity"],
            }
            for entry in items
        ]

        try:
            shipping = client.calculate_shipping(line_items, address)
        except PrintifyError as exc:
            flash(f"Shipping calculation failed: {exc}", "error")
            return render_template(
                "checkout.html",
                items=items,
                subtotal=subtotal,
                form=form,
                shipping=None,
                paypal_client_id=PAYPAL_CLIENT_ID,
            )

        shipping_cost = shipping.get("standard", 0)
        session["_checkout_address"] = address
        session["_checkout_shipping"] = shipping_cost
        session.modified = True

    return render_template(
        "checkout.html",
        items=items,
        subtotal=subtotal,
        form=form,
        shipping=shipping_cost,
        paypal_client_id=PAYPAL_CLIENT_ID,
    )


@app.route("/api/paypal/create-order", methods=["POST"])
def paypal_create_order():
    try:
        return _do_create_order()
    except Exception as exc:
        app.logger.exception("create-order crashed")
        return jsonify({"error": str(exc)}), 500


def _do_create_order():
    if not paypal_configured():
        return jsonify({"error": "Payments not configured"}), 503

    address = session.get("_checkout_address")
    if not address:
        return jsonify({"error": "Please fill in your shipping address first."}), 400

    shipping_cost = session.get("_checkout_shipping")
    if shipping_cost is None:
        return jsonify({"error": "Shipping not calculated. Go back and submit your address."}), 400

    items, subtotal = cart_summary()
    if not items:
        return jsonify({"error": "Cart is empty"}), 400

    line_items = [
        {
            "product_id": entry["product"]["id"],
            "variant_id": entry["variant"]["id"],
            "quantity": entry["quantity"],
        }
        for entry in items
    ]

    paypal_items = []
    for entry in items:
        paypal_items.append({
            "name": entry["product"]["title"][:127],
            "description": entry["variant"]["title"][:127],
            "quantity": str(entry["quantity"]),
            "unit_amount": {
                "currency_code": "USD",
                "value": _cents_to_paypal(entry["variant"]["price"]),
            },
        })

    total_cents = subtotal + shipping_cost
    purchase_units = [{
        "amount": {
            "currency_code": "USD",
            "value": _cents_to_paypal(total_cents),
            "breakdown": {
                "item_total": {
                    "currency_code": "USD",
                    "value": _cents_to_paypal(subtotal),
                },
                "shipping": {
                    "currency_code": "USD",
                    "value": _cents_to_paypal(shipping_cost),
                },
            },
        },
        "items": paypal_items,
        "shipping": {
            "name": {"full_name": f"{address['first_name']} {address['last_name']}"},
            "address": {
                "address_line_1": address["address1"],
                "address_line_2": address.get("address2") or "",
                "admin_area_2": address["city"],
                "admin_area_1": address.get("region") or "",
                "postal_code": address["zip"],
                "country_code": address["country"],
            },
        },
    }]

    pp_order = _paypal_create_order(purchase_units)
    if not pp_order:
        return jsonify({"error": "Failed to create PayPal order. Check your PayPal credentials."}), 502

    paypal_order_id = pp_order["id"]

    order_payload = {
        "external_id": uuid.uuid4().hex,
        "label": f"WEB-{uuid.uuid4().hex[:8].upper()}",
        "line_items": line_items,
        "shipping_method": 1,
        "send_shipping_notification": True,
        "address_to": address,
    }

    storage.save_pending(paypal_order_id, order_payload)

    session["_order_amounts"] = {
        "subtotal": subtotal,
        "shipping": shipping_cost,
    }
    session.modified = True

    return jsonify({"id": paypal_order_id})


@app.route("/api/paypal/capture-order", methods=["POST"])
def paypal_capture_order_route():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"error": "Missing order_id"}), 400

    result = _paypal_capture_order(order_id)
    if not result:
        return jsonify({"error": "Payment capture failed"}), 502

    status = result.get("status")
    if status != "COMPLETED":
        return jsonify({"error": f"Payment not completed (status: {status})"}), 400

    printify_order, fulfillment_status = finalize_order(order_id)
    save_cart([])

    return jsonify({
        "status": "ok",
        "redirect": url_for("order_success", payment_id=order_id),
    })


def finalize_order(payment_id):
    """Submit the pending Printify order, idempotently.
    Returns (printify_order_dict_or_None, status_string)."""
    payload = storage.claim_order(payment_id)
    if payload is None:
        existing = storage.get_order(payment_id)
        if existing and existing["status"] == "submitted" and existing["printify_order_id"]:
            return ({"id": existing["printify_order_id"]}, "submitted")
        return (None, (existing or {}).get("status", "missing"))

    try:
        order = client.submit_order(payload)
    except PrintifyError as exc:
        storage.mark_failed(payment_id, str(exc))
        app.logger.error("Printify order submission failed for %s: %s", payment_id, exc)
        return (None, "failed")
    except Exception:
        storage.reset_to_pending(payment_id)
        app.logger.exception("Unexpected error submitting order %s", payment_id)
        raise

    printify_id = (order or {}).get("id") or "unknown"
    storage.mark_submitted(payment_id, printify_id)
    return (order, "submitted")


@app.route("/order/success")
def order_success():
    payment_id = request.args.get("payment_id")
    if not payment_id:
        return redirect(url_for("index"))

    order_row = storage.get_order(payment_id)
    if not order_row:
        flash("Couldn't verify your payment.", "error")
        return redirect(url_for("index"))

    amounts = session.pop("_order_amounts", {})
    subtotal = amounts.get("subtotal", 0)
    shipping_cost = amounts.get("shipping", 0)
    save_cart([])

    printify_order_id = order_row.get("printify_order_id")
    order_obj = {"id": printify_order_id} if printify_order_id else {"id": payment_id}

    return render_template(
        "order_confirmation.html",
        order=order_obj,
        subtotal=subtotal,
        shipping_cost=shipping_cost,
        fulfillment_status=order_row["status"],
    )


@app.route("/api/shipping", methods=["POST"])
def api_shipping():
    items, _ = cart_summary()
    if not items:
        return jsonify({"error": "Cart is empty"}), 400

    address = request.get_json(silent=True) or {}
    line_items = [
        {
            "product_id": entry["product"]["id"],
            "variant_id": entry["variant"]["id"],
            "quantity": entry["quantity"],
        }
        for entry in items
    ]
    try:
        shipping = client.calculate_shipping(line_items, address)
    except PrintifyError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(shipping)


@app.route("/admin")
@admin_required
def admin_orders():
    rows = storage.list_orders(limit=200)
    orders = []
    totals = {"submitted": 0, "pending": 0, "submitting": 0, "failed": 0}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        line_items = payload.get("line_items") or []
        addr = payload.get("address_to") or {}
        orders.append({
            "payment_id": row["payment_id"],
            "status": row["status"],
            "printify_order_id": row["printify_order_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "label": payload.get("label", ""),
            "external_id": payload.get("external_id", ""),
            "item_count": sum(li.get("quantity", 0) for li in line_items),
            "customer": " ".join(filter(None, [addr.get("first_name"), addr.get("last_name")])).strip(),
            "email": addr.get("email", ""),
            "country": addr.get("country", ""),
        })
        if row["status"] in totals:
            totals[row["status"]] += 1

    return render_template("admin_orders.html", orders=orders, totals=totals)


@app.route("/admin/orders/<payment_id>")
@admin_required
def admin_order_detail(payment_id):
    row = storage.get_order(payment_id)
    if not row:
        abort(404)
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return render_template(
        "admin_order_detail.html",
        row=row,
        payload=payload,
        payload_pretty=json.dumps(payload, indent=2),
    )


@app.route("/admin/orders/<payment_id>/retry", methods=["POST"])
@admin_required
def admin_order_retry(payment_id):
    row = storage.get_order(payment_id)
    if not row:
        abort(404)
    if row["status"] not in ("failed", "submitting"):
        flash("Only failed or stuck orders can be retried.", "error")
        return redirect(url_for("admin_order_detail", payment_id=payment_id))

    storage.force_reset_to_pending(payment_id)
    _, status = finalize_order(payment_id)
    flash(f"Retry result: {status}", "success" if status == "submitted" else "error")
    return redirect(url_for("admin_order_detail", payment_id=payment_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
