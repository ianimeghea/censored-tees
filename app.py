import hmac
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from functools import wraps

import stripe
from dotenv import load_dotenv
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

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-key-change-me")

# Render terminates TLS at its load balancer and forwards via X-Forwarded-Proto.
# This makes url_for() generate https URLs and request.is_secure work correctly.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Tighten session cookies in production (when not in Flask debug mode).
if not app.debug and os.environ.get("FLASK_ENV") != "development":
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

client = PrintifyClient()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

storage.init_db()


def stripe_configured():
    return bool(stripe.api_key and stripe.api_key.startswith("sk_"))


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
    }


@app.errorhandler(PrintifyError)
def handle_printify_error(error):
    return render_template("error.html", message=str(error)), 502


@app.route("/healthz")
def healthz():
    return ("ok", 200, {"Content-Type": "text/plain"})


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


def address_from_form(form):
    return {
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


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items, subtotal = cart_summary()
    if not items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("index"))

    if not stripe_configured():
        return render_template(
            "error.html",
            message="Payments aren't configured yet. Add your Stripe keys to .env and restart.",
        ), 503

    if request.method == "POST":
        try:
            address = address_from_form(request.form)
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
                form=request.form,
                shipping=None,
            )

        shipping_cost = shipping.get("standard", 0)

        stripe_line_items = [
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": entry["variant"]["price"],
                    "product_data": {
                        "name": entry["product"]["title"],
                        "description": entry["variant"]["title"][:500],
                        "images": [entry["image"]] if entry["image"] else [],
                    },
                },
                "quantity": entry["quantity"],
            }
            for entry in items
        ]

        order_payload = {
            "external_id": uuid.uuid4().hex,
            "label": f"WEB-{uuid.uuid4().hex[:8].upper()}",
            "line_items": line_items,
            "shipping_method": 1,
            "send_shipping_notification": True,
            "address_to": address,
        }

        try:
            checkout_session = stripe.checkout.Session.create(
                mode="payment",
                line_items=stripe_line_items,
                customer_email=address["email"],
                shipping_options=[{
                    "shipping_rate_data": {
                        "type": "fixed_amount",
                        "fixed_amount": {"amount": shipping_cost, "currency": "usd"},
                        "display_name": "Standard shipping",
                    },
                }],
                success_url=f"{APP_BASE_URL}/order/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{APP_BASE_URL}/checkout",
                metadata={
                    "order_external_id": order_payload["external_id"],
                },
                payment_intent_data={
                    "metadata": {
                        "order_external_id": order_payload["external_id"],
                    },
                },
            )
        except stripe.error.StripeError as exc:
            flash(f"Payment setup failed: {exc.user_message or str(exc)}", "error")
            return render_template(
                "checkout.html",
                items=items,
                subtotal=subtotal,
                form=request.form,
                shipping=shipping_cost,
            )

        # Persist the Printify order so the success route OR the webhook can finalize it.
        # Whichever fires first claims it; the other call becomes a no-op.
        storage.save_pending(checkout_session.id, order_payload)
        save_cart([])
        return redirect(checkout_session.url, code=303)

    return render_template(
        "checkout.html",
        items=items,
        subtotal=subtotal,
        form={},
        shipping=None,
    )


def finalize_order(stripe_session_id):
    """Submit the pending Printify order for this Stripe session, idempotently.
    Returns (printify_order_dict_or_None, status_string)."""
    payload = storage.claim_order(stripe_session_id)
    if payload is None:
        # Either nothing pending (already submitted), or another worker claimed it.
        existing = storage.get_order(stripe_session_id)
        if existing and existing["status"] == "submitted" and existing["printify_order_id"]:
            return ({"id": existing["printify_order_id"]}, "submitted")
        return (None, (existing or {}).get("status", "missing"))

    try:
        order = client.submit_order(payload)
    except PrintifyError as exc:
        storage.mark_failed(stripe_session_id, str(exc))
        app.logger.error("Printify order submission failed for %s: %s", stripe_session_id, exc)
        return (None, "failed")
    except Exception as exc:
        # Don't leave it stuck in 'submitting' — let webhook retries pick it up.
        storage.reset_to_pending(stripe_session_id)
        app.logger.exception("Unexpected error submitting order %s", stripe_session_id)
        raise

    printify_id = (order or {}).get("id") or "unknown"
    storage.mark_submitted(stripe_session_id, printify_id)
    return (order, "submitted")


@app.route("/order/success")
def order_success():
    session_id = request.args.get("session_id")
    if not session_id:
        return redirect(url_for("index"))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError:
        flash("Couldn't verify your payment.", "error")
        return redirect(url_for("index"))

    if checkout_session.payment_status != "paid":
        flash("Payment is still processing. You'll get an email once it's confirmed.", "success")
        save_cart([])
        return redirect(url_for("index"))

    printify_order, status = finalize_order(session_id)
    save_cart([])

    return render_template(
        "order_confirmation.html",
        order=printify_order or {"id": session_id},
        subtotal=(checkout_session.amount_subtotal or 0),
        shipping_cost=(checkout_session.shipping_cost.amount_total if checkout_session.shipping_cost else 0),
        fulfillment_status=status,
    )


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        app.logger.warning("Webhook hit but STRIPE_WEBHOOK_SECRET not configured.")
        return ("Webhook secret not configured", 503)

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return ("Invalid signature", 400)

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        if session_obj.get("payment_status") == "paid":
            session_id = session_obj["id"]
            try:
                _, status = finalize_order(session_id)
                app.logger.info("Webhook finalized order %s: %s", session_id, status)
            except Exception:
                # Return 500 so Stripe will retry the webhook.
                return ("Order submission error", 500)

    return jsonify({"received": True})


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
            "stripe_session_id": row["stripe_session_id"],
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


@app.route("/admin/orders/<stripe_session_id>")
@admin_required
def admin_order_detail(stripe_session_id):
    row = storage.get_order(stripe_session_id)
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


@app.route("/admin/orders/<stripe_session_id>/retry", methods=["POST"])
@admin_required
def admin_order_retry(stripe_session_id):
    row = storage.get_order(stripe_session_id)
    if not row:
        abort(404)
    if row["status"] not in ("failed", "submitting"):
        flash("Only failed or stuck orders can be retried.", "error")
        return redirect(url_for("admin_order_detail", stripe_session_id=stripe_session_id))

    storage.force_reset_to_pending(stripe_session_id)
    _, status = finalize_order(stripe_session_id)
    flash(f"Retry result: {status}", "success" if status == "submitted" else "error")
    return redirect(url_for("admin_order_detail", stripe_session_id=stripe_session_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
