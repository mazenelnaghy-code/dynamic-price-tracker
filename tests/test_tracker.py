import csv
import sys
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tracker  # noqa: E402


def product(pid, price, in_stock=True, name=None):
    return {"id": pid, "name": name or pid, "category": "Audio", "price": price, "in_stock": in_stock}


def test_parse_price():
    assert tracker.parse_price("EGP 1,450") == 1450.0
    assert tracker.parse_price("EGP 1,295EGP 1,450") == 1295.0  # sale price first, crossed-out price ignored
    assert tracker.parse_price("Sold out") is None


def test_compare_finds_every_kind_of_change():
    old = [product("A", 100), product("B", 200), product("C", 300, in_stock=False), product("D", 50)]
    new = [product("A", 80), product("B", 210), product("C", 300, in_stock=True), product("E", 70)]
    changes = {(c["name"], c["change"]): c for c in tracker.compare(old, new)}
    assert changes[("A", "Price drop")]["pct"] == pytest.approx(-20)
    assert changes[("B", "Price rise")]["pct"] == pytest.approx(5)
    assert ("C", "Back in stock") in changes
    assert ("E", "New product") in changes
    assert ("D", "Removed") in changes
    assert tracker.compare(old, old) == []


def test_biggest_drops_come_first():
    old = [product("A", 100), product("B", 100)]
    new = [product("A", 95), product("B", 70)]
    assert [c["name"] for c in tracker.compare(old, new)] == ["B", "A"]


def test_report_marks_deals():
    changes = tracker.compare([product("A", 100)], [product("A", 85)])
    text = tracker.report("2026-10-02", "2026-10-01", [product("A", 85)], changes)
    assert "| A | Price drop **deal** | 100 | 85 | -15.0% |" in text
    assert "First run" in tracker.report("2026-10-01", None, [product("A", 85)], [])


# ---- Against the demo shop, in a real browser --------------------------------------------------------

def browser_available():
    try:
        server, url = tracker.serve_demo_shop()
        try:
            tracker.scrape(url + "?date=2026-01-01", timeout_ms=15000)
        finally:
            server.shutdown()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not browser_available(), reason="no Chromium for Playwright on this machine")


@pytest.fixture(scope="module")
def shop():
    server, url = tracker.serve_demo_shop()
    yield url
    server.shutdown()


def test_plain_http_sees_no_products(shop):
    html = urllib.request.urlopen(shop).read().decode()
    assert 'class="product"' not in html  # the products only exist after JavaScript runs


@needs_browser
def test_scrape_reads_every_product_behind_load_more(shop):
    rows = tracker.scrape(shop + "?date=2026-10-01")
    assert len(rows) == 24 and len({r["id"] for r in rows}) == 24
    assert all(r["price"] > 0 and r["name"] for r in rows)
    assert rows == tracker.scrape(shop + "?date=2026-10-01")  # same day, same prices


@needs_browser
def test_runs_build_history_and_report(tmp_path, shop):
    tracker.run(shop + "?date=2026-10-01", "2026-10-01", tmp_path)
    tracker.run(shop + "?date=2026-10-02", "2026-10-02", tmp_path)
    tracker.run(shop + "?date=2026-10-02", "2026-10-02", tmp_path)  # a re-run replaces the day, never duplicates it
    with (tmp_path / "history.csv").open() as f:
        history = list(csv.DictReader(f))
    assert len(history) == 48
    assert {r["date"] for r in history} == {"2026-10-01", "2026-10-02"}
    assert "compared with 2026-10-01" in (tmp_path / "changes.md").read_text()


@needs_browser
def test_a_load_more_button_that_never_disappears(tmp_path):
    """Some shops keep the button after the last page. The tracker must stop, not hang or fail."""
    (tmp_path / "index.html").write_text("""<div id=list></div><button id=more>Load more</button><script>
      let n = 0; const add = k => { for (let i = 0; i < k; i++, n++) list.insertAdjacentHTML('beforeend',
        `<div class=product data-id=P${n}><a class=name>Item ${n}</a><span class=category>X</span>
         <span class=price>EGP ${100 + n}</span><span class=stock>In stock</span></div>`); };
      add(5); more.onclick = () => { if (n < 10) add(5); };
    </script>""")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(tracker.QuietHandler, directory=str(tmp_path)))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        rows = tracker.scrape(f"http://127.0.0.1:{server.server_port}/index.html", timeout_ms=1500)
    finally:
        server.shutdown()
    assert [r["price"] for r in rows] == [100.0 + i for i in range(10)]
