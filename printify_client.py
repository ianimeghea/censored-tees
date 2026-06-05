import os
import requests

API_BASE = "https://api.printify.com/v1"


class PrintifyError(Exception):
    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class PrintifyClient:
    def __init__(self, token=None, shop_id=None, user_agent=None):
        self.token = token or os.environ.get("PRINTIFY_API_TOKEN")
        self.shop_id = shop_id or os.environ.get("PRINTIFY_SHOP_ID")
        self.user_agent = user_agent or os.environ.get("PRINTIFY_USER_AGENT", "PrintifyStorefront")

    @property
    def is_configured(self):
        return bool(self.token and self.shop_id)

    def _headers(self):
        if not self.token:
            raise PrintifyError("Missing PRINTIFY_API_TOKEN. Add it to your .env file.")
        return {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json;charset=utf-8",
        }

    def _request(self, method, path, **kwargs):
        url = f"{API_BASE}{path}"
        try:
            response = requests.request(method, url, headers=self._headers(), timeout=20, **kwargs)
        except requests.RequestException as exc:
            raise PrintifyError(f"Network error contacting Printify: {exc}") from exc

        if response.status_code == 204:
            return None

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if not response.ok:
            message = (payload or {}).get("message") if isinstance(payload, dict) else None
            raise PrintifyError(
                message or f"Printify API error ({response.status_code})",
                status_code=response.status_code,
                payload=payload,
            )

        return payload

    def list_shops(self):
        return self._request("GET", "/shops.json")

    def list_products(self, page=1, limit=50):
        return self._request(
            "GET",
            f"/shops/{self.shop_id}/products.json",
            params={"page": page, "limit": limit},
        )

    def get_product(self, product_id):
        return self._request("GET", f"/shops/{self.shop_id}/products/{product_id}.json")

    def calculate_shipping(self, line_items, address_to):
        body = {"line_items": line_items, "address_to": address_to}
        return self._request("POST", f"/shops/{self.shop_id}/orders/shipping.json", json=body)

    def submit_order(self, payload):
        return self._request("POST", f"/shops/{self.shop_id}/orders.json", json=payload)

    def publishing_failed(self, product_id, reason="Unpublished by store admin"):
        return self._request(
            "POST",
            f"/shops/{self.shop_id}/products/{product_id}/publishing_failed.json",
            json={"reason": reason},
        )

    def delete_product(self, product_id):
        return self._request(
            "DELETE",
            f"/shops/{self.shop_id}/products/{product_id}.json",
        )
