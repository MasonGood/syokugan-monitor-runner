"""Validate public-only monitor state before importing it into a fresh history."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


FIELDS = {
    "initialized", "source_mode", "known_codes", "active_restock_codes",
    "monitored_sections", "last_snapshot",
}
PRODUCT_FIELDS = {"code", "title", "release_date", "url", "section", "image_url"}
CODE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
SECTIONS = {"hc1", "hc3", "hc7"}


def public_url(value, *, empty=False):
    if empty and value == "":
        return
    parts = urlsplit(value)
    if (
        parts.scheme != "https" or parts.hostname != "syokugan-ohkoku.com"
        or parts.username or parts.password or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise ValueError("Only public product-site URLs may be imported")


def validate_baseline(data):
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError("Unexpected baseline fields")
    if data["initialized"] is not True or data["source_mode"] != "full_lists_v1":
        raise ValueError("Expected an initialized full-list baseline")
    for name in ("known_codes", "active_restock_codes"):
        values = data[name]
        if not isinstance(values, list) or any(
            not isinstance(code, str) or not CODE.fullmatch(code) for code in values
        ):
            raise ValueError("Invalid product code list")
    if (
        not isinstance(data["monitored_sections"], list)
        or set(data["monitored_sections"]) != SECTIONS
    ):
        raise ValueError("Unexpected monitored sections")
    snapshots = data["last_snapshot"]
    if not isinstance(snapshots, dict) or not snapshots:
        raise ValueError("Expected nonempty public product snapshot")
    for key, product in snapshots.items():
        if not isinstance(product, dict) or set(product) != PRODUCT_FIELDS:
            raise ValueError("Unexpected snapshot fields")
        if any(not isinstance(value, str) for value in product.values()):
            raise ValueError("Invalid snapshot values")
        if (
            not CODE.fullmatch(product["code"]) or product["section"] not in SECTIONS
            or key != f'{product["section"]}:{product["code"]}'
        ):
            raise ValueError("Snapshot identity mismatch")
        if any(EMAIL.search(value) for value in product.values()):
            raise ValueError("Personal contact information is not allowed")
        public_url(product["url"])
        public_url(product["image_url"], empty=True)
        parts = urlsplit(product["url"])
        if parts.path != "/item.php" or parse_qs(parts.query) != {"code": [product["code"]]}:
            raise ValueError("Unexpected product detail URL")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = validate_baseline(json.load(sys.stdin))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(f"Imported {len(data['last_snapshot'])} public product records")


if __name__ == "__main__":
    main()
