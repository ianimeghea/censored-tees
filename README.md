# Censored Tees

Flask storefront with Stripe Checkout payments, Printify fulfillment, and an admin order log.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in values
python main.py
```

Open http://127.0.0.1:5000.

For Stripe webhook testing locally:
```bash
stripe listen --forward-to localhost:5000/stripe/webhook
# copy the whsec_... it prints into STRIPE_WEBHOOK_SECRET in .env, restart
```

## Deploy to Render

This repo includes `render.yaml`, a `Procfile`, and `runtime.txt` — a one-click Blueprint deploy works.

### Steps

1. **Push the repo to GitHub** (private is fine).
2. Go to https://dashboard.render.com → **New** → **Blueprint** → connect your repo.
3. Render reads `render.yaml` and provisions:
   - A web service running `gunicorn app:app`
   - A 1GB persistent disk mounted at `/var/data` (so SQLite survives restarts)
   - Auto-generated `FLASK_SECRET_KEY`
4. You'll be prompted to fill in the secret env vars listed below. Set them, then deploy.
5. After the first deploy succeeds, copy the Render URL Render gives you (e.g. `https://censored-tees.onrender.com`) and:
   - Set it as `APP_BASE_URL` in the env vars
   - Use it as the webhook URL in Stripe (see below)

### Required env vars (set in Render dashboard)

| Variable | Where to get it |
|---|---|
| `PRINTIFY_API_TOKEN` | https://printify.com/app/account/api |
| `PRINTIFY_SHOP_ID` | `curl -H "Authorization: Bearer $TOKEN" https://api.printify.com/v1/shops.json` |
| `STRIPE_SECRET_KEY` | https://dashboard.stripe.com/apikeys (use **live** key for production) |
| `STRIPE_PUBLISHABLE_KEY` | same dashboard, "Publishable key" |
| `STRIPE_WEBHOOK_SECRET` | Stripe Dashboard → Webhooks → Add endpoint → see below |
| `APP_BASE_URL` | Your Render URL, e.g. `https://censored-tees.onrender.com` |
| `ADMIN_USER` | Pick a username |
| `ADMIN_PASSWORD` | Pick a strong password (this protects `/admin`) |

### Setting up the Stripe webhook

1. After your Render service is live, go to https://dashboard.stripe.com/webhooks
2. **Add endpoint**:
   - Endpoint URL: `https://your-app.onrender.com/stripe/webhook`
   - Events to send: select **`checkout.session.completed`**
3. After creation, click into the endpoint → **Signing secret** → **Reveal** → copy the `whsec_...` value
4. Paste it into Render's env vars as `STRIPE_WEBHOOK_SECRET`
5. Render auto-redeploys on env var changes

## What's in this repo

- `app.py` — Flask routes (storefront, cart, checkout, success, webhook, admin)
- `printify_client.py` — REST wrapper for the Printify API
- `storage.py` — SQLite helpers for pending order tracking (atomic claim + retry)
- `templates/` — Jinja2 templates
- `static/style.css` — site styling
- `render.yaml` — Render Blueprint config
- `Procfile` — gunicorn launch command
- `runtime.txt` — Python version pin

## Order flow

1. Customer fills cart → enters address at checkout
2. App calculates Printify shipping, persists order payload to SQLite, redirects to Stripe Checkout
3. Customer pays on Stripe
4. Stripe sends `checkout.session.completed` webhook **and** redirects user to `/order/success`
5. Whichever fires first atomically claims the order from the DB and submits it to Printify
6. Status is tracked in `/admin` — failed orders can be retried with one click

## Notes

- Pricing: Stripe charges the customer the same price as your Printify variant cost. Mark up your variant prices in Printify to keep margin.
- Order approval: by default Printify auto-approves orders within 24h. Set your shop's order approval to **Manual** in the Printify dashboard if you want to review each one.
- Webhook retries: Stripe retries failed webhooks for up to 3 days, so transient errors recover automatically.

### Free plan trade-offs

This is configured for Render's free tier. Two things to know:

- **Cold starts**: service sleeps after 15 min idle. First request after sleep takes ~30s to wake. Stripe webhooks during a wake may time out — but Stripe retries for 3 days so orders still get submitted to Printify, just with a delay.
- **Ephemeral DB**: SQLite lives in `/tmp/store.db` and resets on every deploy/restart. The admin order log only shows recent orders. **Stripe Dashboard is your authoritative payment record** — go to https://dashboard.stripe.com/payments to see all-time orders.

To upgrade to always-on + persistent disk later: change `plan: free` → `plan: starter` in `render.yaml`, add the disk block back (see git history), and change `DB_PATH` to `/var/data/store.db`. Costs $7/mo.
