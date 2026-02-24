# run.py
# ebay-flipper-2: simple UK eBay "flipping" scanner using the eBay Finding API (AppID only)
# Outputs results.csv with profit + margin filters.

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

import requests

FINDING_ENDPOINT = "https://svcs.ebay.com/services/search/FindingService/v1"

DEFAULT_EBAY_FEE_RATE = 0.128
DEFAULT_PAYMENT_FEE_RATE = 0.029
DEFAULT_PAYMENT_FIXED_FEE = 0.30
DEFAULT_SHIPPING_OUT = 4.50

MIN_PROFIT_GBP = 25.0
MIN_MARGIN = 0.25


def _iso_utc(dt: datetime) -> str:
    # eBay Finding API wants ISO 8601; UTC is safest
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ebay_finding_call(
    app_id: str,
    operation: str,
    global_id: str,
    params: Dict[str, Any],
    timeout: int = 30,
) -> Dict[str, Any]:
    base_params = {
        "OPERATION-NAME": operation,
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "GLOBAL-ID": global_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "true",
    }
    merged = {**base_params, **params}

    r = requests.get(FINDING_ENDPOINT, params=merged, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    # Basic API error handling
    resp_key = f"{operation}Response"
    root = data.get(resp_key, [{}])[0]
    ack = (root.get("ack", [""])[0] or "").lower()
    if ack not in {"success", "warning"}:
        errs = root.get("errorMessage", [{}])[0].get("error", [])
        msg = "; ".join(
            f"{e.get('errorId', ['?'])[0]}: {e.get('message', [''])[0]}" for e in errs
        ) or "Unknown eBay API error"
        raise RuntimeError(msg)

    return root


def _get_price(obj: Dict[str, Any]) -> Optional[float]:
    # Many eBay Finding fields are lists-of-dicts-of-lists.
    try:
        return float(obj.get("__value__"))
    except Exception:
        return None


def parse_active_items(root: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = root.get("searchResult", [{}])[0].get("item", [])
    out: List[Dict[str, Any]] = []
    for it in items:
        title = (it.get("title", [""])[0] or "").strip()
        item_id = (it.get("itemId", [""])[0] or "").strip()
        url = (it.get("viewItemURL", [""])[0] or "").strip()

        selling = it.get("sellingStatus", [{}])[0]
        price_obj = selling.get("currentPrice", [{}])[0]
        price = _get_price(price_obj)

        shipping = 0.0
        ship_info = it.get("shippingInfo", [{}])[0]
        ship_obj = ship_info.get("shippingServiceCost", [{}])
        if isinstance(ship_obj, list) and ship_obj:
            ship_val = _get_price(ship_obj[0])
            if ship_val is not None:
                shipping = ship_val

        if price is None:
            continue

        out.append(
            {
                "active_title": title,
                "active_item_id": item_id,
                "active_url": url,
                "active_price_gbp": price,
                "active_shipping_gbp": shipping,
                "active_buy_price_gbp": price + shipping,
            }
        )
    return out


def parse_sold_totals(root: Dict[str, Any]) -> List[float]:
    items = root.get("searchResult", [{}])[0].get("item", [])
    totals: List[float] = []
    for it in items:
        selling = it.get("sellingStatus", [{}])[0]
        price_obj = selling.get("currentPrice", [{}])[0]
        price = _get_price(price_obj)

        shipping = 0.0
        ship_info = it.get("shippingInfo", [{}])[0]
        ship_obj = ship_info.get("shippingServiceCost", [{}])
        if isinstance(ship_obj, list) and ship_obj:
            ship_val = _get_price(ship_obj[0])
            if ship_val is not None:
                shipping = ship_val

        if price is None:
            continue

        totals.append(price + shipping)
    return totals


def find_active(
    app_id: str,
    keyword: str,
    active_limit: int,
    global_id: str,
) -> List[Dict[str, Any]]:
    root = ebay_finding_call(
        app_id=app_id,
        operation="findItemsByKeywords",
        global_id=global_id,
        params={
            "keywords": keyword,
            "paginationInput.entriesPerPage": str(active_limit),
            "sortOrder": "BestMatch",
            # UK focus via global_id EBAY-GB; you can also add categoryId if you want.
        },
    )
    return parse_active_items(root)


def find_sold_totals(
    app_id: str,
    keyword: str,
    sold_limit: int,
    global_id: str,
    days: int = 90,
) -> List[float]:
    end_from = _iso_utc(datetime.now(timezone.utc) - timedelta(days=days))

    # Completed items API:
    # itemFilter(0)=SoldItemsOnly true
    # itemFilter(1)=EndTimeFrom <iso>
    root = ebay_finding_call(
        app_id=app_id,
        operation="findCompletedItems",
        global_id=global_id,
        params={
            "keywords": keyword,
            "paginationInput.entriesPerPage": str(min(sold_limit, 100)),
            "sortOrder": "EndTimeSoonest",
            "itemFilter(0).name": "SoldItemsOnly",
            "itemFilter(0).value": "true",
            "itemFilter(1).name": "EndTimeFrom",
            "itemFilter(1).value": end_from,
        },
    )

    totals = parse_sold_totals(root)

    # If they asked for more than 100, page (Finding API max 100 per page).
    page = 1
    while len(totals) < sold_limit:
        total_pages = int(root.get("paginationOutput", [{}])[0].get("totalPages", ["1"])[0])
        page += 1
        if page > total_pages:
            break

        root = ebay_finding_call(
            app_id=app_id,
            operation="findCompletedItems",
            global_id=global_id,
            params={
                "keywords": keyword,
                "paginationInput.entriesPerPage": "100",
                "paginationInput.pageNumber": str(page),
                "sortOrder": "EndTimeSoonest",
                "itemFilter(0).name": "SoldItemsOnly",
                "itemFilter(0).value": "true",
                "itemFilter(1).name": "EndTimeFrom",
                "itemFilter(1).value": end_from,
            },
        )
        totals.extend(parse_sold_totals(root))

        # be polite to the API
        time.sleep(0.1)

    return totals[:sold_limit]


def compute_row(
    keyword: str,
    active: Dict[str, Any],
    sold_totals: List[float],
    ebay_fee_rate: float,
    payment_fee_rate: float,
    payment_fixed_fee: float,
    shipping_out: float,
) -> Optional[Dict[str, Any]]:
    if len(sold_totals) == 0:
        return None

    median_sold = float(median(sold_totals))
    ebay_fee = median_sold * ebay_fee_rate
    payment_fee = median_sold * payment_fee_rate + payment_fixed_fee

    active_buy = float(active["active_buy_price_gbp"])
    expected_profit = median_sold - ebay_fee - payment_fee - shipping_out - active_buy
    margin = expected_profit / active_buy if active_buy > 0 else -1.0

    if expected_profit < MIN_PROFIT_GBP:
        return None
    if margin < MIN_MARGIN:
        return None

    return {
        "keyword": keyword,
        "active_title": active["active_title"],
        "active_item_id": active["active_item_id"],
        "active_url": active["active_url"],
        "active_buy_price_gbp": round(active_buy, 2),
        "median_sold_price_gbp": round(median_sold, 2),
        "sold_sample_size": len(sold_totals),
        "ebay_fee_gbp": round(ebay_fee, 2),
        "payment_fee_gbp": round(payment_fee, 2),
        "shipping_out_gbp": round(shipping_out, 2),
        "expected_profit_gbp": round(expected_profit, 2),
        "margin_percent": round(margin * 100, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--keywords",
        required=True,
        help='Comma-separated list, e.g. "lego star wars,sony walkman,game boy"',
    )
    ap.add_argument("--active-limit", type=int, default=20)
    ap.add_argument("--sold-limit", type=int, default=120)
    ap.add_argument("--output", default="results.csv")
    ap.add_argument("--global-id", default="EBAY-GB", help="EBAY-GB for UK")
    ap.add_argument("--app-id", default=os.environ.get("EBAY_APP_ID", "").strip())

    ap.add_argument("--ebay-fee-rate", type=float, default=DEFAULT_EBAY_FEE_RATE)
    ap.add_argument("--payment-fee-rate", type=float, default=DEFAULT_PAYMENT_FEE_RATE)
    ap.add_argument("--payment-fixed-fee", type=float, default=DEFAULT_PAYMENT_FIXED_FEE)
    ap.add_argument("--shipping-out", type=float, default=DEFAULT_SHIPPING_OUT)

    args = ap.parse_args()

    if not args.app_id:
        print('Missing App ID. Set EBAY_APP_ID env var or pass --app-id "YOUR_APP_ID".', file=sys.stderr)
        return 2

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        print("No keywords provided.", file=sys.stderr)
        return 2

    rows: List[Dict[str, Any]] = []

    for kw in keywords:
        try:
            active_items = find_active(args.app_id, kw, args.active_limit, args.global_id)
            sold_totals = find_sold_totals(args.app_id, kw, args.sold_limit, args.global_id, days=90)
        except requests.HTTPError as e:
            print(f"[{kw}] HTTP error: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"[{kw}] Error: {e}", file=sys.stderr)
            continue

        for a in active_items:
            row = compute_row(
                keyword=kw,
                active=a,
                sold_totals=sold_totals,
                ebay_fee_rate=args.ebay_fee_rate,
                payment_fee_rate=args.payment_fee_rate,
                payment_fixed_fee=args.payment_fixed_fee,
                shipping_out=args.shipping_out,
            )
            if row:
                rows.append(row)

    # Write CSV
    fieldnames = [
        "keyword",
        "active_title",
        "active_item_id",
        "active_url",
        "active_buy_price_gbp",
        "median_sold_price_gbp",
        "sold_sample_size",
        "ebay_fee_gbp",
        "payment_fee_gbp",
        "shipping_out_gbp",
        "expected_profit_gbp",
        "margin_percent",
    ]

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
