"""Audit downloaded public data; count actual rows without loading text into the LLM."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import duckdb

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "upstream" / "esci-data"
DATA = REPO / "shopping_queries_dataset"
EXPECTED = {
    "shopping_queries_dataset_examples.parquet": (51286808, "4a735b693b4a424a6fc67f5be6e4c811495c488bbf66d02a602d308b2744263a"),
    "shopping_queries_dataset_products.parquet": (1108857465, "25124442d064d64b26f74082d6fa09438d679efc0c183cf28d19064a2b65a265"),
}


def rows(db, query, params=None):
    cursor = db.execute(query, params or [])
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://github.com/amazon-science/esci-data",
        "license": "Apache-2.0 (repository LICENSE; retain NOTICE)",
        "duckdb_version": duckdb.__version__,
        "dataset_role": "Historical public product-query relevance data; no adoption or active-customer claim.",
        "files": [],
    }
    result["commit"] = subprocess.check_output([r"E:\Git\cmd\git.exe", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    verified = set()
    for name, (size, digest) in EXPECTED.items():
        path = DATA / name
        row = {"file": name, "exists": path.is_file(), "expected_bytes": size, "expected_sha256": digest}
        if path.is_file():
            row["actual_bytes"] = path.stat().st_size
            row["actual_sha256"] = sha256(path)
            row["integrity_passed"] = row["actual_bytes"] == size and row["actual_sha256"] == digest
            if row["integrity_passed"]:
                verified.add(name)
        result["files"].append(row)
    with duckdb.connect() as db:
        db.execute("SET memory_limit='6GB'")
        db.execute("SET threads=4")
        examples = "shopping_queries_dataset_examples.parquet"
        products = "shopping_queries_dataset_products.parquet"
        if examples in verified:
            db.read_parquet(str(DATA / examples)).create_view("examples")
            result["examples"] = rows(db, "SELECT COUNT(*) AS rows, COUNT(DISTINCT query_id) AS query_ids, "
                                      "COUNT(DISTINCT (product_locale,query)) AS locale_query_texts, "
                                      "COUNT(DISTINCT (product_locale,product_id)) AS locale_products, "
                                      "COUNT(*) FILTER (WHERE query IS NULL OR product_id IS NULL OR product_locale IS NULL) AS missing_keys "
                                      "FROM examples")[0]
            result["splits"] = rows(db, "SELECT split,product_locale,COUNT(*) AS rows,COUNT(DISTINCT query_id) AS queries "
                                    "FROM examples GROUP BY ALL ORDER BY ALL")
            result["labels"] = rows(db, "SELECT esci_label,COUNT(*) AS rows FROM examples GROUP BY ALL ORDER BY ALL")
            result["version_counts"] = rows(db, "SELECT small_version,large_version,COUNT(*) AS rows FROM examples GROUP BY ALL ORDER BY ALL")
            result["cross_split_query_id_count"] = rows(db, "SELECT COUNT(*) AS n FROM (SELECT query_id FROM examples GROUP BY query_id HAVING COUNT(DISTINCT split)>1)")[0]["n"]
            result["cross_split_literal_query_count"] = rows(db, "SELECT COUNT(*) AS n FROM (SELECT product_locale,query FROM examples GROUP BY ALL HAVING COUNT(DISTINCT split)>1)")[0]["n"]
            result["duplicate_example_id_count"] = rows(db, "SELECT COUNT(*)-COUNT(DISTINCT example_id) AS n FROM examples")[0]["n"]
        if products in verified:
            db.read_parquet(str(DATA / products)).create_view("products")
            result["products"] = rows(db, "SELECT COUNT(*) AS rows, COUNT(DISTINCT (product_locale,product_id)) AS unique_locale_products, "
                                      "COUNT(*) FILTER (WHERE product_title IS NULL OR TRIM(product_title)='') AS missing_titles "
                                      "FROM products")[0]
            result["product_locales"] = rows(db, "SELECT product_locale,COUNT(*) AS rows FROM products GROUP BY ALL ORDER BY ALL")
            if examples in verified:
                result["unmatched_example_rows"] = rows(db, "SELECT COUNT(*) AS n FROM examples e ANTI JOIN products p USING(product_locale,product_id)")[0]["n"]
    result["download_complete_and_verified"] = len(verified) == 2
    result["readme_count_caution"] = "Upstream prose says 2,621,738 large-version judgements; its table says 2,621,288. Use audited file counts and explicit version filters."
    output = ROOT / "evidence" / "esci_data_audit.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
