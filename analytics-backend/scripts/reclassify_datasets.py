"""
One-shot migration: re-classify existing datasets using the patched
score-based `detect_dataset_type` from `api.routes.data`.

Why: when a user installs Kapi for the first time, every dataset they upload
goes through the new score-based classifier and lands in the right type. But
users who installed an earlier version have datasets stamped with the old
classifier — typically a Kaggle product catalog tagged as `unknown`, which
then leaks into the dashboard's Events / Users dropdowns (because the
frontend filter is permissive: `dataset_type === 'events' || === 'unknown'`).

This script walks every row in the `datasets` table, re-reads the underlying
file, runs the new classifier, and updates the row when the type changes.

Idempotent: re-running it after the first pass is a no-op because the rows
already carry the correct type.

Usage (from any dir; we resolve paths from the script's own location):
    python scripts/reclassify_datasets.py            # dry-run
    python scripts/reclassify_datasets.py --apply    # apply changes

When invoked from apply_patches.ps1, we always pass --apply.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Resolve the analytics-backend dir relative to this file so we can import
# the patched modules and read storage/kapi.db.
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent  # .../analytics-backend
sys.path.insert(0, str(BACKEND))

# These imports must succeed AFTER patches are applied — the patched data.py
# is what ships the new classifier.
try:
    from api.routes.data import detect_dataset_type, _read_file  # type: ignore
except Exception as exc:
    print(f"[reclassify] ERROR: could not import detect_dataset_type from api.routes.data: {exc}")
    print(f"[reclassify] Make sure patches are applied to {BACKEND}.")
    sys.exit(2)


DB_PATH = BACKEND / "storage" / "kapi.db"


def reclassify(apply: bool) -> int:
    """Walk all datasets, recompute dataset_type, optionally write changes.

    Returns the count of rows whose type changed (or would have changed in a
    dry-run).
    """
    if not DB_PATH.exists():
        print(f"[reclassify] DB not found at {DB_PATH} — nothing to do (probably a fresh install).")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, filename, filepath, dataset_type FROM datasets")
        rows = cur.fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[reclassify] datasets table not present yet: {exc} — nothing to do.")
        conn.close()
        return 0

    changes: list[tuple[str, str, str, str]] = []   # (id, name, old_type, new_type)
    skipped: list[tuple[str, str, str]] = []         # (id, name, reason)

    for row in rows:
        ds_id = row["id"]
        ds_name = row["name"]
        old_type = row["dataset_type"] or "unknown"
        path = Path(row["filepath"])
        if not path.exists():
            skipped.append((ds_id, ds_name, f"file missing: {path}"))
            continue
        try:
            df = _read_file(path, row["filename"])
        except Exception as exc:
            skipped.append((ds_id, ds_name, f"read failed: {exc}"))
            continue

        try:
            new_type = detect_dataset_type(df.columns.tolist(), df)
        except Exception as exc:
            skipped.append((ds_id, ds_name, f"classify failed: {exc}"))
            continue

        if new_type != old_type:
            changes.append((ds_id, ds_name, old_type, new_type))

    # Report.
    if not changes and not skipped:
        print(f"[reclassify] All {len(rows)} dataset(s) already correctly classified.")
        conn.close()
        return 0

    if changes:
        print(f"[reclassify] {len(changes)} dataset(s) need reclassification:")
        for ds_id, ds_name, old, new in changes:
            print(f"           - {ds_name}  ({ds_id[:8]}...)  {old} -> {new}")
    if skipped:
        print(f"[reclassify] Skipped {len(skipped)} dataset(s):")
        for ds_id, ds_name, reason in skipped:
            print(f"           - {ds_name}  ({ds_id[:8]}...)  {reason}")

    if apply and changes:
        cur = conn.cursor()
        for ds_id, _name, _old, new in changes:
            cur.execute(
                "UPDATE datasets SET dataset_type = ? WHERE id = ?",
                (new, ds_id),
            )
        conn.commit()
        print(f"[reclassify] Applied {len(changes)} update(s).")
    elif changes:
        print("[reclassify] Dry-run — pass --apply to write changes.")

    conn.close()
    return len(changes)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Write changes to the DB.")
    args = p.parse_args()
    # `reclassify` returns the count of changed rows, but for an exit code we
    # care only about success/failure: changes were applied (or not needed)
    # is success — return 0. Reserve non-zero for the import-failure path
    # at the top of this file.
    reclassify(args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
