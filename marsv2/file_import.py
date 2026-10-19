"""Offline, idempotent importer for reviewed file-library material."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .db import connect
from .filed import initialize
from .curated_import import import_items


SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,63}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
CATALOG_STATUSES = {
    "historical_listing", "recovered_verified", "possible_match",
    "unavailable", "quarantined", "redistribution_not_approved",
}


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def regular_payload(root: Path, relative: str, expected_size: int,
                    expected_hash: str) -> bytes:
    parts = Path(relative).parts
    if (not relative or relative.startswith(("/", "\\")) or ".." in parts
            or any(part in ("", ".") for part in parts)):
        raise ValueError("unsafe payload path")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    file_fd = -1
    try:
        for component in parts[:-1]:
            child_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"payload is not a regular file: {relative}")
        if metadata.st_size != expected_size or metadata.st_size > 60_000:
            raise ValueError(f"payload size mismatch: {relative}")
        value = b""
        while len(value) <= 60_000:
            chunk = os.read(file_fd, min(65536, 60_001 - len(value)))
            if not chunk:
                break
            value += chunk
        if len(value) != expected_size:
            raise ValueError(f"payload changed while reading: {relative}")
    except OSError as exc:
        raise ValueError(f"unsafe or unreadable payload: {relative}") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)
    if sha256(value) != expected_hash:
        raise ValueError(f"payload checksum mismatch: {relative}")
    if any(byte in value for byte in (0, 0x1b)):
        raise ValueError(f"active terminal or NUL byte rejected: {relative}")
    value.decode("ascii", "strict")
    return value


def preview(value: bytes) -> str:
    return value.decode("ascii").replace("\r\n", "\n").replace("\r", "\n").replace(
        "\f", "\n\n---------------- PAGE BREAK ----------------\n\n")


def validate_manifest(document: object) -> list[dict[str, object]]:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported approved-file manifest")
    rows = document.get("files")
    if not isinstance(rows, list):
        raise ValueError("invalid approved-file manifest")
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid approved-file record")
        area, name = row.get("area"), row.get("name")
        if not isinstance(area, str) or not SAFE_LABEL.fullmatch(area):
            raise ValueError("unsafe file area")
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            raise ValueError("unsafe filename")
        key = (area, name)
        if key in seen:
            raise ValueError("duplicate approved file")
        seen.add(key)
        result.append(row)
    return result


def import_all(db_path: Path, catalog_path: Path, manifest_path: Path,
               payload_root: Path, dry_run: bool) -> dict[str, int]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("records"), list):
        raise ValueError("unsupported catalog")
    approved = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    payloads: list[tuple[dict[str, object], str]] = []
    for row in approved:
        raw = regular_payload(payload_root, str(row["source_path"]), int(row["size"]),
                              str(row["sha256"]))
        payloads.append((row, preview(raw)))
    if dry_run:
        return {"catalog": len(catalog["records"]), "reviewed": len(payloads),
                "published": sum(bool(row.get("publish", True)) for row, _ in payloads)}

    db = connect(str(db_path))
    initialize(db)
    with db:
        # Links are rebuilt solely from the current reviewed manifest. This
        # prevents a removed approval from leaving a stale view/download path.
        db.execute("UPDATE historical_catalog SET published_file_id=NULL")
        for record in catalog["records"]:
            if record.get("status") not in CATALOG_STATUSES:
                raise ValueError("invalid catalog status")
            listed_date = (f"{record['listed_month']} {record['listed_day']} "
                           f"{record['listed_year_or_time']}")
            db.execute(
                "INSERT INTO historical_catalog(category,filename,listed_size,listed_date,"
                "mode,listing_line,recovery_status,sha256,source_url,license_status,notes) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(category,filename) DO UPDATE SET "
                "listed_size=excluded.listed_size,listed_date=excluded.listed_date,"
                "mode=excluded.mode,listing_line=excluded.listing_line,"
                "recovery_status=excluded.recovery_status,sha256=excluded.sha256,"
                "source_url=excluded.source_url,license_status=excluded.license_status,"
                "notes=excluded.notes",
                (record["category"], record["filename"], record["listed_size"],
                 listed_date, record["mode"], record["listing_line"], record["status"],
                 record["sha256"], record["source_url"], record["license_status"],
                 record["notes"]))
        for record, content in payloads:
            if not bool(record.get("publish", True)):
                # Remove only a byte-identical item previously managed by this
                # importer. Never delete an unrelated operator/user file that
                # happens to reuse its display name.
                db.execute(
                    "DELETE FROM files WHERE area=? AND name=? AND source_url=? AND sha256=?",
                    (record["area"], record["name"], record["source_url"],
                     record["sha256"]))
                continue
            cursor = db.execute(
                "INSERT INTO files(area,name,content,description,source_url,sha256,"
                "original_date,license_status,historical,encoding) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(area,name) DO UPDATE SET content=excluded.content,"
                "description=excluded.description,source_url=excluded.source_url,"
                "sha256=excluded.sha256,original_date=excluded.original_date,"
                "license_status=excluded.license_status,historical=excluded.historical,"
                "encoding=excluded.encoding RETURNING id",
                (record["area"], record["name"], content, record["description"],
                 record["source_url"], record["sha256"], record["original_date"],
                 record["license_status"], int(bool(record["historical"])), "ASCII"))
            file_id = int(cursor.fetchone()[0])
            category = record.get("catalog_category")
            filename = record.get("catalog_filename")
            if category is not None or filename is not None:
                linked = db.execute(
                    "UPDATE historical_catalog SET published_file_id=? "
                    "WHERE category=? AND filename=?",
                    (file_id, category, filename))
                if linked.rowcount != 1:
                    raise ValueError("approved historical file has no catalog record")
    return {"catalog": len(catalog["records"]), "reviewed": len(payloads),
            "published": sum(bool(row.get("publish", True)) for row, _ in payloads)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--payload-root", type=Path, required=True)
    parser.add_argument("--curated-fetch-results", type=Path)
    parser.add_argument("--curated-approvals", type=Path)
    parser.add_argument("--curated-staging-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    curated = (args.curated_fetch_results, args.curated_approvals,
               args.curated_staging_root)
    if any(curated) and not all(curated):
        parser.error("all curated import paths must be supplied together")
    result: dict[str, object] = {
        "baseline": import_all(args.db, args.catalog, args.manifest,
                               args.payload_root, args.dry_run)
    }
    if all(curated):
        curated_result = import_items(
            args.db, args.curated_fetch_results, args.curated_approvals,
            args.curated_staging_root, args.dry_run)
        # The detailed item ledger is retained in the manifest and database.
        # Startup logs should report counts without emitting hundreds of rows.
        files = curated_result.pop("files", [])
        curated_result["file_records"] = len(files)
        result["curated"] = curated_result
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
