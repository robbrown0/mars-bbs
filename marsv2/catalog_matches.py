"""Apply explicit issue-level recovery evidence to the Mars listing catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import connect
from .filed import initialize


def apply(db_path: Path, fetch_path: Path, rules_path: Path,
          dry_run: bool = False) -> dict[str, object]:
    fetched = json.loads(fetch_path.read_text(encoding="utf-8"))
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    if fetched.get("schema_version") != 1 or not isinstance(fetched.get("results"), dict):
        raise ValueError("unsupported fetch-results document")
    if rules.get("schema_version") != 1 or not isinstance(rules.get("rules"), list):
        raise ValueError("unsupported catalog-match rules")
    updates: list[dict[str, object]] = []
    for rule in rules["rules"]:
        if not isinstance(rule, dict):
            raise ValueError("invalid match rule")
        first, last = int(rule["first"]), int(rule["last"])
        if first < 1 or last < first or last - first > 100:
            raise ValueError("invalid match-rule range")
        for number in range(first, last + 1):
            source_id = str(rule["source_template"]).format(number=number)
            candidate = fetched["results"].get(source_id)
            if not isinstance(candidate, dict):
                raise ValueError(f"missing fetch result for {source_id}")
            fetched_ok = candidate.get("status") == "fetched"
            updates.append({
                "category": str(rule["category"]),
                "filename": str(rule["filename_template"]).format(number=number),
                "source_id": source_id,
                "status": "possible_match" if fetched_ok else "quarantined",
                "sha256": str(candidate.get("sha256", "")) if fetched_ok else "",
                "source_url": str(candidate.get("final_url", candidate.get("url", ""))),
                "license_status": "item-level publication review required",
                "notes": str(rule["relationship"]) + (
                    " Candidate fetched and held outside the public library."
                    if fetched_ok else
                    " Candidate failed text validation and remains quarantined/unpublished."),
            })
    result: dict[str, object] = {"matched": len(updates), "dry_run": dry_run,
                                "updates": updates}
    if dry_run:
        return result
    db = connect(str(db_path))
    initialize(db)
    with db:
        for item in updates:
            cursor = db.execute(
                "UPDATE historical_catalog SET recovery_status=?,sha256=?,source_url=?,"
                "license_status=?,notes=? WHERE category=? AND filename=?",
                (item["status"], item["sha256"], item["source_url"],
                 item["license_status"], item["notes"], item["category"],
                 item["filename"]))
            if cursor.rowcount != 1:
                raise ValueError("catalog rule did not match exactly one record: "
                                 f"{item['category']}/{item['filename']}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--fetch-results", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = apply(args.db, args.fetch_results, args.rules, args.dry_run)
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
