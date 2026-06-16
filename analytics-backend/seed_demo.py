"""
Seed demo datasets into the Kapi analytics backend.
Run once to populate the DB with sample events, users, and feature_usage data
so the Dashboard, Analyst, Reports, and Eval tabs work immediately.

Usage:
    python seed_demo.py              # seed if DB is empty
    python seed_demo.py --force      # re-seed even if data exists
"""
import asyncio
import sys
import uuid
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.config import get_settings
from core.database import init_db, AsyncSessionLocal, Dataset
from services.rag.faiss_index import build_index
from sqlalchemy import select
import pandas as pd

settings = get_settings()
UPLOAD_DIR = settings.storage_dir / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SAMPLES_DIR = Path(__file__).parent / "data" / "samples"

# Files to seed: (filename, display_name, dataset_type)
SEED_FILES = [
    ("events.csv",         "Demo Events",        "events"),
    ("users.csv",          "Demo Users",          "users"),
    ("feature_usage.csv",  "Demo Feature Usage",  "feature_usage"),
]


def _detect_type_signals(columns: list[str]) -> str:
    col_set = {c.lower().strip() for c in columns}
    signals = {
        "events": ["event_name", "event_type", "action", "event"],
        "users": ["user_id", "email", "plan", "country", "created_at"],
        "feature_usage": ["feature", "feature_name", "feature_id"],
    }
    for dtype, sigs in signals.items():
        if any(sig in c for sig in sigs for c in col_set):
            return dtype
    return "unknown"


async def seed(force: bool = False):
    await init_db()

    async with AsyncSessionLocal() as db:
        # Check if already seeded
        result = await db.execute(select(Dataset))
        existing = result.scalars().all()

        if existing and not force:
            print(f"[Kapi Seed] Already have {len(existing)} dataset(s). Use --force to re-seed.")
            return

        if force and existing:
            # Delete existing demo datasets
            for ds in existing:
                if ds.name.startswith("Demo "):
                    Path(ds.filepath).unlink(missing_ok=True)
                    await db.delete(ds)
            await db.commit()
            print("[Kapi Seed] Cleared existing demo datasets.")

        seeded = 0
        for filename, display_name, dtype in SEED_FILES:
            src = SAMPLES_DIR / filename
            if not src.exists():
                print(f"[Kapi Seed] SKIP {filename} — file not found at {src}")
                continue

            dataset_id = str(uuid.uuid4())
            dest = UPLOAD_DIR / f"{dataset_id}.csv"
            shutil.copy2(src, dest)

            # Read and analyze
            df = pd.read_csv(dest)
            schema_info = {col: str(dt) for col, dt in df.dtypes.items()}

            tags = [dtype]
            if any("user" in c.lower() for c in df.columns):
                tags.append("users")
            if any("time" in c.lower() or "date" in c.lower() or "ts" in c.lower() for c in df.columns):
                tags.append("time-series")

            dataset = Dataset(
                id=dataset_id,
                name=display_name,
                filename=filename,
                filepath=str(dest),
                dataset_type=dtype,
                row_count=len(df),
                column_count=len(df.columns),
                schema_info=schema_info,
                tags=list(set(tags)),
                indexed=False,
            )
            db.add(dataset)
            await db.commit()
            await db.refresh(dataset)

            # Build FAISS index synchronously
            print(f"[Kapi Seed] Indexing {display_name} ({len(df)} rows)...")
            try:
                count = build_index(dataset_id, df, display_name)
                dataset.indexed = True
                await db.commit()
                print(f"[Kapi Seed]   -> {count} chunks indexed")
            except Exception as e:
                print(f"[Kapi Seed]   -> Index failed: {e} (analytics will still work)")

            seeded += 1
            print(f"[Kapi Seed] Loaded: {display_name} ({len(df)} rows, {len(df.columns)} cols, type={dtype})")

        print(f"\n[Kapi Seed] Done! {seeded} demo datasets loaded.")
        print("[Kapi Seed] Start the backend with: python main.py")


if __name__ == "__main__":
    force = "--force" in sys.argv
    asyncio.run(seed(force))
