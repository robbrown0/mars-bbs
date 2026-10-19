"""Transactional importer for explicitly reviewed historical library items.

Fetch state is discovery evidence, not an approval list.  This importer only
accepts records named in a separate, hash-pinned review policy and revalidates
both immutable source bytes and terminal-safe previews before touching SQLite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .db import connect
from .filed import initialize


SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &+.,_/'()-]{0,63}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+,'()-]{0,79}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_ORIGINAL = 2_000_000
MAX_PREVIEW = 2_000_000


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _relative_stage_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/staging/"):
        raise ValueError("fetch result has an invalid staging path")
    relative = value.removeprefix("/staging/")
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("fetch result has an unsafe staging path")
    return relative


def read_regular(root: Path, relative: str, maximum: int) -> bytes:
    """Open every component without following links and read a bounded file."""
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("unsafe staging path")
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
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("staging payload is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(65536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError("staging payload exceeds its size limit")
        if total != metadata.st_size:
            raise ValueError("staging payload changed while being read")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"unsafe or unreadable staging payload: {relative}") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)


def terminal_safe_preview(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("preview is not UTF-8") from exc
    # Form feed is meaningful in period documents but can clear a live
    # terminal.  Represent it as a visible page boundary in the derived view;
    # immutable source_payloads still retains the original byte.
    decoded = decoded.replace("\f", "\n\n---------------- PAGE BREAK ----------------\n\n")
    for character in decoded:
        code = ord(character)
        if character not in ("\n", "\t") and (code < 32 or code == 127
                                                  or 0x80 <= code <= 0x9f):
            raise ValueError("preview contains an active or unsupported control byte")
    return decoded


def load_inputs(fetch_path: Path, approval_path: Path, staging_root: Path
                ) -> tuple[list[dict[str, object]], str]:
    fetched = json.loads(fetch_path.read_text(encoding="utf-8"))
    approvals = json.loads(approval_path.read_text(encoding="utf-8"))
    if fetched.get("schema_version") != 1 or not isinstance(fetched.get("results"), dict):
        raise ValueError("unsupported fetch-results document")
    if approvals.get("schema_version") != 1 or not isinstance(approvals.get("files"), list):
        raise ValueError("unsupported approval document")
    approval_hash = digest(canonical_json(approvals))
    prepared: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    for approval in approvals["files"]:
        if not isinstance(approval, dict):
            raise ValueError("invalid approval record")
        source_id = approval.get("id")
        if not isinstance(source_id, str) or source_id in seen_ids:
            raise ValueError("invalid or duplicate approval id")
        seen_ids.add(source_id)
        area, name = approval.get("area"), approval.get("name")
        if not isinstance(area, str) or not SAFE_LABEL.fullmatch(area):
            raise ValueError(f"unsafe area for {source_id}")
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            raise ValueError(f"unsafe display name for {source_id}")
        if (area, name) in seen_names:
            raise ValueError("duplicate destination name")
        seen_names.add((area, name))
        record = fetched["results"].get(source_id)
        if not isinstance(record, dict) or record.get("status") != "fetched":
            raise ValueError(f"approved item was not fetched: {source_id}")
        expected_hash = approval.get("sha256")
        if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
            raise ValueError(f"approval is not hash-pinned: {source_id}")
        if record.get("sha256") != expected_hash:
            raise ValueError(f"approval/fetch checksum conflict: {source_id}")
        original = read_regular(staging_root, _relative_stage_path(record.get("raw_path")),
                                MAX_ORIGINAL)
        if digest(original) != expected_hash or len(original) != int(record["actual_size"]):
            raise ValueError(f"original failed revalidation: {source_id}")
        preview_bytes = read_regular(
            staging_root, _relative_stage_path(record.get("preview_path")), MAX_PREVIEW)
        preview = terminal_safe_preview(preview_bytes)
        policy = approval.get("rendering_policy", "wrapped")
        if policy not in ("wrapped", "fixed"):
            raise ValueError(f"invalid rendering policy: {source_id}")
        if approval.get("rights_decision") != "eligible":
            raise ValueError(f"approval lacks an eligible rights decision: {source_id}")
        if approval.get("content_decision") != "eligible":
            raise ValueError(f"approval lacks an eligible content decision: {source_id}")
        prepared.append({"approval": approval, "fetch": record,
                         "original": original, "preview": preview})
    return prepared, approval_hash


def import_items(db_path: Path, fetch_path: Path, approval_path: Path,
                 staging_root: Path, dry_run: bool = False) -> dict[str, object]:
    items, manifest_hash = load_inputs(fetch_path, approval_path, staging_root)
    batch_id = "curated-" + manifest_hash[:20]
    result: dict[str, object] = {
        "batch_id": batch_id, "manifest_sha256": manifest_hash,
        "approved": len(items), "inserted": 0, "updated": 0, "files": [],
        "dry_run": dry_run,
    }
    if dry_run:
        result["files"] = [str(item["approval"]["id"]) for item in items]
        return result
    db = connect(str(db_path))
    initialize(db)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with db:
        db.execute(
            "INSERT INTO import_batches(id,manifest_sha256,created_at,item_count,state) "
            "VALUES(?,?,?,?, 'committed') ON CONFLICT(id) DO UPDATE SET "
            "state='committed',item_count=excluded.item_count",
            (batch_id, manifest_hash, now, len(items)))
        for item in items:
            approval = item["approval"]
            fetched = item["fetch"]
            source_id = str(approval["id"])
            sha = str(approval["sha256"])
            existing = db.execute(
                "SELECT id,area,name FROM files WHERE source_identity=? AND payload_sha256=?",
                (source_id, sha)).fetchone()
            collision = db.execute(
                "SELECT id,source_identity,payload_sha256 FROM files WHERE area=? AND name=?",
                (approval["area"], approval["name"])).fetchone()
            if collision is not None and (existing is None or collision["id"] != existing["id"]):
                raise ValueError(f"refusing to overwrite existing library item: "
                                 f"{approval['area']}/{approval['name']}")
            db.execute(
                "INSERT INTO source_payloads(sha256,original,size,source_url,retrieved_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET "
                "source_url=excluded.source_url,retrieved_at=excluded.retrieved_at",
                (sha, item["original"], len(item["original"]), fetched["final_url"],
                 fetched["retrieved_at"]))
            values = (
                approval["area"], approval["name"], item["preview"],
                approval["description"], fetched["final_url"], sha,
                approval.get("original_date", ""), approval["rights_summary"],
                int(fetched.get("historical_association") == "documented_mars_issue_or_record"),
                fetched["encoding"], source_id, sha, batch_id, len(item["original"]),
                approval["attribution"], fetched["retrieved_at"],
                approval["rights_evidence"], approval.get("rendering_policy", "wrapped"),
                approval["rights_decision"], approval["content_decision"],
                int(fetched.get("max_line_columns", 0)),
            )
            if existing is None:
                cursor = db.execute(
                    "INSERT INTO files(area,name,content,description,source_url,sha256,"
                    "original_date,license_status,historical,encoding,source_identity,"
                    "payload_sha256,batch_id,original_size,attribution,retrieved_at,"
                    "rights_evidence,rendering_policy,rights_decision,content_decision,"
                    "max_line_columns) VALUES(" + ",".join("?" for _ in values) + ")",
                    values)
                file_id = int(cursor.lastrowid)
                result["inserted"] = int(result["inserted"]) + 1
            else:
                file_id = int(existing["id"])
                db.execute(
                    "UPDATE files SET area=?,name=?,content=?,description=?,source_url=?,"
                    "sha256=?,original_date=?,license_status=?,historical=?,encoding=?,"
                    "source_identity=?,payload_sha256=?,batch_id=?,original_size=?,"
                    "attribution=?,retrieved_at=?,rights_evidence=?,rendering_policy=?,"
                    "rights_decision=?,content_decision=?,max_line_columns=? WHERE id=?",
                    values + (file_id,))
                result["updated"] = int(result["updated"]) + 1
            result["files"].append({"id": file_id, "source_identity": source_id,
                                    "area": approval["area"], "name": approval["name"],
                                    "sha256": sha})
    return result


def rollback(db_path: Path, batch_id: str) -> dict[str, object]:
    if not re.fullmatch(r"curated-[0-9a-f]{20}", batch_id):
        raise ValueError("invalid batch id")
    db = connect(str(db_path))
    initialize(db)
    row = db.execute("SELECT state FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise ValueError("unknown import batch")
    with db:
        removed = db.execute("DELETE FROM files WHERE batch_id=?", (batch_id,)).rowcount
        payloads = db.execute(
            "DELETE FROM source_payloads WHERE sha256 NOT IN "
            "(SELECT payload_sha256 FROM files WHERE payload_sha256<>'')").rowcount
        db.execute("UPDATE import_batches SET state='rolled_back' WHERE id=?", (batch_id,))
    return {"batch_id": batch_id, "removed_files": removed,
            "removed_unreferenced_payloads": payloads}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--fetch-results", type=Path)
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.rollback:
        result = rollback(args.db, args.rollback)
    else:
        if not all((args.fetch_results, args.approvals, args.staging_root)):
            parser.error("fetch-results, approvals, and staging-root are required")
        result = import_items(args.db, args.fetch_results, args.approvals,
                              args.staging_root, args.dry_run)
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
