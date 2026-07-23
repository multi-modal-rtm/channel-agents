"""
One-time script to pull product catalog from amoCRM and print it.

Usage:
    python scripts/fetch_amocrm_catalog.py --token <bearer_token>

The catalog ID 13553 is taken from the amoCRM URL:
    auras.amocrm.ru/catalogs/13553/
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

DOMAIN = "auras.amocrm.ru"
CATALOG_ID = 13553


def fetch_all_elements(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://{DOMAIN}/api/v4/catalogs/{CATALOG_ID}/elements"
    params = {"limit": 250, "page": 1}
    results = []

    while True:
        resp = httpx.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 401:
            print("ERROR: Token is invalid or expired.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code == 204:
            break  # no content = no more items
        resp.raise_for_status()

        data = resp.json()
        items = data.get("_embedded", {}).get("elements", [])
        if not items:
            break

        results.extend(items)

        # Check if there's a next page
        next_link = data.get("_links", {}).get("next")
        if not next_link:
            break
        params["page"] += 1

    return results


def extract_field(fields: list[dict], field_name: str) -> str | None:
    """Pull a custom field value by name from amoCRM element fields."""
    for f in fields:
        if f.get("field_name", "").lower() == field_name.lower():
            values = f.get("values", [])
            if values:
                return str(values[0].get("value", ""))
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="amoCRM Bearer token")
    args = parser.parse_args()

    print(f"Fetching catalog {CATALOG_ID} from {DOMAIN}...")
    elements = fetch_all_elements(args.token)
    print(f"Found {len(elements)} products.\n")

    products = []
    for el in elements:
        fields = el.get("custom_fields_values") or []
        name = el.get("name", "")
        price = extract_field(fields, "Цена") or extract_field(fields, "Price") or ""
        sku = extract_field(fields, "Артикул") or extract_field(fields, "SKU") or ""
        description = extract_field(fields, "Описание") or extract_field(fields, "Description") or ""
        unit = extract_field(fields, "Единица измерения") or ""

        product = {
            "id": el.get("id"),
            "name": name,
            "sku": sku,
            "price_som": price,
            "unit": unit,
            "description": description,
        }
        products.append(product)
        print(f"  [{sku or '-'}] {name} — {price} so'm  {unit}")

    # Save full JSON for seeding
    out_path = "scripts/amocrm_products.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
