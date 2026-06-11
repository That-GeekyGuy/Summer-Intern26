"""CSV writer shared by ml_export.py and used by dump.py for append mode."""

import pandas as pd
from pathlib import Path
from datetime import datetime


def write(df: pd.DataFrame, out_dir: Path, prefix: str) -> Path:
    """Write df to a timestamped CSV in out_dir; return the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(path)
    print(f"[CSV] wrote {len(df)} rows → {path}")
    return path


def append(df: pd.DataFrame, path: Path) -> int:
    """Append df rows to path; write header only on first write."""
    write_header = not path.exists() or path.stat().st_size == 0
    df.to_csv(path, mode="a", header=write_header)
    return len(df)


def last_timestamp(path: Path):
    """Return the max index timestamp from an existing CSV, or None."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True, usecols=[0])
        return df.index.max() if not df.empty else None
    except Exception:
        return None
