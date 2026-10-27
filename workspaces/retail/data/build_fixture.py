"""Build a deliberately messy retail SQLite fixture for local development and tests.

Messiness included on purpose:
* no declared primary or foreign keys
* implicit joins with inconsistent formats (``C-00012`` vs ``12``, `` s03 `` vs ``S03``, ``sku-1001`` vs ``SKU-1001``)
* orphaned references and null keys
* decoy tables (backup copy, audit log, temp export) that compete with the real ones
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

FIRST = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jade", "Kofi", "Lena", "Mateo", "Nia", "Omar", "Priya"]
LAST = ["Garcia", "Smith", "Li", "Okafor", "Novak", "Haddad", "Kim", "Rossi", "Silva", "Tanaka", "Nguyen", "Brown"]
CITIES = ["Seattle", "Austin", "Denver", "Boston", "Chicago", "Miami", "Portland", "Atlanta"]
CATEGORIES = ["Outdoor", "Kitchen", "Electronics", "Toys", "Garden", "Apparel"]
REGIONS = ["West", "Central", "East", "South"]


def build(path: str | Path, customers: int = 200, orders: int = 1000, seed: int = 42) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE CUST_MASTER (cust_id INTEGER, full_nm TEXT, email_addr TEXT, city_txt TEXT, created_dt TEXT);
        CREATE TABLE cust_master_bak (cust_id INTEGER, full_nm TEXT, email_addr TEXT, city_txt TEXT, created_dt TEXT);
        CREATE TABLE stores_ref (store_code TEXT, store_name TEXT, region TEXT);
        CREATE TABLE prod_catalog (SKU TEXT, prod_name TEXT, category TEXT, list_price REAL);
        CREATE TABLE tbl_ord_hdr (order_no TEXT, cust_ref TEXT, store_cd TEXT, ord_dt TEXT, total_amt REAL, status_cd TEXT);
        CREATE TABLE ord_lines_v2 (order_no TEXT, line_no INTEGER, sku TEXT, qty INTEGER, unit_prc REAL);
        CREATE TABLE audit_log (log_id INTEGER, user_id INTEGER, action TEXT, ts TEXT);
        CREATE TABLE tmp_export_2019 (col1 TEXT, col2 TEXT, col3 REAL);
        """
    )

    customer_rows = []
    for cid in range(1, customers + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        customer_rows.append(
            (cid, f"{first} {last}", f"{first.lower()}.{last.lower()}{cid}@example.com", rng.choice(CITIES), f"2023-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}")
        )
    cur.executemany("INSERT INTO CUST_MASTER VALUES (?,?,?,?,?)", customer_rows)
    cur.executemany("INSERT INTO cust_master_bak VALUES (?,?,?,?,?)", customer_rows[: customers * 3 // 5])

    stores = [(f"S{i:02d}", f"Store {i:02d}", REGIONS[i % len(REGIONS)]) for i in range(1, 9)]
    cur.executemany("INSERT INTO stores_ref VALUES (?,?,?)", stores)

    products = [
        (f"SKU-{1000 + i}", f"Product {1000 + i}", CATEGORIES[i % len(CATEGORIES)], round(rng.uniform(3, 250), 2))
        for i in range(1, 61)
    ]
    cur.executemany("INSERT INTO prod_catalog VALUES (?,?,?,?)", products)

    order_rows, line_rows = [], []
    for oid in range(1, orders + 1):
        cust = rng.randint(1, customers)
        if rng.random() < 0.02:
            cust = rng.randint(customers + 500, customers + 900)  # orphaned reference
        store = rng.choice(stores)[0]
        store_cd = None if rng.random() < 0.01 else f" {store.lower()} " if rng.random() < 0.5 else store.lower()
        order_no = f"ORD-{oid:06d}"
        lines = rng.randint(1, 4)
        total = 0.0
        for line_no in range(1, lines + 1):
            product = rng.choice(products)
            qty = rng.randint(1, 5)
            total += qty * product[3]
            line_rows.append((order_no, line_no, product[0].lower(), qty, product[3]))
        order_rows.append(
            (order_no, f"C-{cust:05d}", store_cd, f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}", round(total, 2), rng.choice(["NEW", "SHIP", "DONE"]))
        )
    cur.executemany("INSERT INTO tbl_ord_hdr VALUES (?,?,?,?,?,?)", order_rows)
    cur.executemany("INSERT INTO ord_lines_v2 VALUES (?,?,?,?,?)", line_rows)

    cur.executemany(
        "INSERT INTO audit_log VALUES (?,?,?,?)",
        [(i, rng.randint(1, customers), rng.choice(["login", "export", "update"]), f"2024-01-{rng.randint(1, 28):02d}") for i in range(1, 301)],
    )
    cur.executemany(
        "INSERT INTO tmp_export_2019 VALUES (?,?,?)",
        [(f"X{i}", rng.choice(["a", "b", "c"]), rng.random()) for i in range(50)],
    )
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    print(build(Path(__file__).parent / "retail.sqlite"))
