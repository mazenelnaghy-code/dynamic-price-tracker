"""Track prices on a shop that builds its pages with JavaScript, and report what changed since the last run.

Usage:
    python tracker.py                          # the demo shop in shop/, served locally
    python tracker.py --date 2026-10-01        # the demo shop as it was on that day
    python tracker.py --url https://shop.example.com/products

Each run opens the page in a real (headless) browser, waits for the products to appear, clicks "Load more"
until every product is on the page, and reads name, price and stock. It then compares with the previous run
and writes:
    data/latest.csv     the prices from this run
    data/history.csv    every run, one row per product per day
    data/changes.md     what changed: price drops and rises, new and removed products, stock changes

To track another shop, change URL and the CSS selectors in SELECTORS.
"""
import argparse
import csv
import os
import re
import sys
import threading
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
SELECTORS = {  # where each field lives on the page
    "product": ".product",
    "name": ".name",
    "category": ".category",
    "price": ".price",
    "stock": ".stock",
    "load_more": "#more",
}
FIELDS = ["date", "id", "name", "category", "price", "in_stock"]
BIG_DROP = 10  # % - drops at least this big are marked as deals in the report
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_price(text):
    """'EGP 1,450' -> 1450.0; the first number wins, so a crossed-out old price is ignored."""
    match = NUMBER.search(text or "")
    return float(match.group().replace(",", "")) if match else None


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve_demo_shop():
    """Serve shop/ on a free local port in the background; return the server and its URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(HERE / "shop")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/index.html"


def scrape(url, timeout_ms=20000):
    """Every product on the page, after JavaScript has drawn them and 'Load more' has run out."""
    s = SELECTORS
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_selector(s["product"], timeout=timeout_ms)
        more = page.locator(s["load_more"])
        for _ in range(100):  # a safety stop for a button that never goes away
            if not (more.is_visible() and more.is_enabled()):
                break
            before = page.locator(s["product"]).count()
            more.click()
            try:
                page.wait_for_function("([sel, n]) => document.querySelectorAll(sel).length > n",
                                       arg=[s["product"], before], timeout=timeout_ms)
            except PlaywrightTimeout:
                break  # the button is still there but loads nothing new: that was the last page
        rows = page.eval_on_selector_all(s["product"], """(cards, s) => cards.map(c => {
            const text = sel => (c.querySelector(sel)?.firstChild?.textContent || "").trim();
            return {id: c.dataset.id || text(s.name), name: text(s.name), category: text(s.category),
                    price: text(s.price), stock: (c.querySelector(s.stock)?.innerText || "").trim()};
        })""", s)
        browser.close()
    return [{"id": r["id"], "name": r["name"], "category": r["category"], "price": parse_price(r["price"]),
             "in_stock": not r["stock"].lower().startswith("out")} for r in rows]


def compare(old, new):
    """What changed between two runs, biggest price drops first."""
    old_by_id, new_by_id = {p["id"]: p for p in old}, {p["id"]: p for p in new}
    changes = []
    for pid, now in new_by_id.items():
        was = old_by_id.get(pid)
        if was is None:
            changes.append({"name": now["name"], "change": "New product", "before": None, "now": now["price"], "pct": None})
            continue
        if was["price"] and now["price"] and now["price"] != was["price"]:
            pct = (now["price"] - was["price"]) / was["price"] * 100
            kind = "Price drop" if pct < 0 else "Price rise"
            changes.append({"name": now["name"], "change": kind, "before": was["price"], "now": now["price"], "pct": pct})
        if was["in_stock"] != now["in_stock"]:
            changes.append({"name": now["name"], "change": "Back in stock" if now["in_stock"] else "Out of stock",
                            "before": None, "now": None, "pct": None})
    for pid, was in old_by_id.items():
        if pid not in new_by_id:
            changes.append({"name": was["name"], "change": "Removed", "before": was["price"], "now": None, "pct": None})
    order = {"Price drop": 0, "Back in stock": 1, "New product": 2, "Price rise": 3, "Out of stock": 4, "Removed": 5}
    return sorted(changes, key=lambda c: (order[c["change"]], c["pct"] or 0))


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{**r, "price": float(r["price"]) if r["price"] else None, "in_stock": r["in_stock"] == "True"}
                for r in csv.DictReader(f)]


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({k: r[k] for k in FIELDS} for r in rows)


def money(value):
    return f"{value:,.0f}" if value is not None else ""


def report(day, previous_day, products, changes):
    lines = [f"# Price check {day}", ""]
    if previous_day is None:
        lines.append(f"First run: {len(products)} products recorded. Changes are reported from the next run.")
        return "\n".join(lines) + "\n"
    drops = [c for c in changes if c["change"] == "Price drop"]
    lines.append(f"{len(products)} products checked, compared with {previous_day}: {len(changes)} changes, "
                 f"{len(drops)} price drops.")
    if changes:
        lines += ["", "| Product | Change | Before (EGP) | Now (EGP) | % |", "|---|---|---:|---:|---:|"]
        for c in changes:
            deal = " **deal**" if c["pct"] is not None and c["pct"] <= -BIG_DROP else ""
            pct = f"{c['pct']:+.1f}%" if c["pct"] is not None else ""
            lines.append(f"| {c['name']} | {c['change']}{deal} | {money(c['before'])} | {money(c['now'])} | {pct} |")
    return "\n".join(lines) + "\n"


def run(url, day, data_dir):
    """Scrape, compare with the last run, and update the files in data_dir. Returns the changes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    latest_path, history_path = data_dir / "latest.csv", data_dir / "history.csv"
    previous = read_csv(latest_path)
    previous_day = previous[0]["date"] if previous else None
    if previous_day == day:  # running twice on the same day compares with the day before, not with itself
        history = [r for r in read_csv(history_path) if r["date"] < day]
        last = max((r["date"] for r in history), default=None)
        previous, previous_day = [r for r in history if r["date"] == last], last

    products = [{"date": day, **p} for p in scrape(url)]
    if not products:
        sys.exit("No products found: the page layout may have changed. Check SELECTORS.")
    changes = compare(previous, products) if previous else []

    write_csv(latest_path, products)
    history = [r for r in read_csv(history_path) if r["date"] != day] + products
    write_csv(history_path, sorted(history, key=lambda r: (r["date"], r["id"])))
    (data_dir / "changes.md").write_text(report(day, previous_day, products, changes), encoding="utf-8")
    return products, changes, previous_day


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="the page to track (default: the demo shop, served locally)")
    parser.add_argument("--date", default=date.today().isoformat(), help="date to record the run under (YYYY-MM-DD)")
    parser.add_argument("--data", type=Path, default=HERE / "data")
    args = parser.parse_args(argv)

    server = None
    url = args.url
    if url is None:
        server, url = serve_demo_shop()
        url += f"?date={args.date}"
    try:
        products, changes, previous_day = run(url, args.date, args.data)
    except PlaywrightTimeout:
        sys.exit("The page didn't show any products in time. Is the URL right, and does it need a login?")
    finally:
        if server:
            server.shutdown()

    print(f"{args.date}: {len(products)} products checked", end="")
    print(f", {len(changes)} changes since {previous_day}" if previous_day else " (first run)")
    for c in changes:
        pct = f" ({c['pct']:+.1f}%)" if c["pct"] is not None else ""
        price = f"  {money(c['before'])} -> {money(c['now'])}" if c["before"] and c["now"] else ""
        print(f"  {c['change']:<14} {c['name']:<26}{price}{pct}")


if __name__ == "__main__":
    main()
