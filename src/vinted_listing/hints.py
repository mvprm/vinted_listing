"""Parse the product hint: the folder name plus optional `key: value` lines from hint.txt."""

from __future__ import annotations

import re

from vinted_listing.models import Condition, ProductHint

TEXT_KEYS = {"brand", "model", "size", "composition", "colour", "category", "notes"}
_PRICE = re.compile(r"^(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur)?$", re.IGNORECASE)
_TRUE = {"yes", "true"}
_FALSE = {"no", "false"}


def parse_hint(folder_name: str, hint_text: str | None) -> ProductHint:
    fields: dict[str, object] = {"free_text": re.sub(r"[-_]+", " ", folder_name).strip()}
    warnings: list[str] = []

    for line in (hint_text or "").lstrip("﻿").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            warnings.append(f"hint line ignored (no 'key: value'): {line!r}")
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if not value:
            continue

        if key in TEXT_KEYS:
            fields[key] = value
        elif key == "condition":
            condition = _condition(value)
            if condition is None:
                warnings.append(f"unknown condition {value!r}; use one of: {', '.join(c.value for c in Condition)}")
            else:
                fields["condition"] = condition
        elif key in {"price", "retail_price"}:
            price = _price(value)
            if price is None:
                warnings.append(f"{key} {value!r} is not a number in EUR")
            else:
                fields["price_eur" if key == "price" else "retail_price_eur"] = price
        elif key == "research":
            if value.lower() in _TRUE:
                fields["research"] = True
            elif value.lower() in _FALSE:
                fields["research"] = False
            else:
                warnings.append(f"research must be yes or no, got {value!r}")
        elif key == "match":
            if value.lower() == "image":
                fields["force_image_match"] = True
            else:
                warnings.append(f"match only accepts 'image', got {value!r}")
        else:
            warnings.append(f"unknown hint key {key!r}")

    return ProductHint(**fields, warnings=warnings)


def _condition(value: str) -> Condition | None:
    for condition in Condition:
        if condition.value.lower() == value.lower():
            return condition
    return None


def _price(value: str) -> float | None:
    match = _PRICE.match(value.strip())
    return float(match.group(1).replace(",", ".")) if match else None
