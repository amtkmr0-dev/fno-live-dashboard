"""Export today's OI Thesis flags (10 bull + 10 bear) as TradingView watchlist
text files. Uses the same /api/oi-thesis endpoint and the same format as the
on-page export buttons.

Outputs (in data/exports/):
  oi_thesis_bull_<YYYYMMDD>.txt
  oi_thesis_bear_<YYYYMMDD>.txt
  oi_thesis_both_<YYYYMMDD>.txt
"""
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "exports"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    with urllib.request.urlopen("http://localhost:8081/api/oi-thesis?days=1") as r:
        d = json.load(r)

    flags = d.get("today_flags") or []
    flag_date = (d.get("latest_flag_date") or "").replace("-", "")
    if not flags or not flag_date:
        print("No flags. Run: ./venv/bin/python3 oi_thesis_tracker.py daily")
        return 1

    bulls = [f["symbol"] for f in flags if f["side"] == "bull"]
    bears = [f["symbol"] for f in flags if f["side"] == "bear"]

    # Same format as the page export — single comma-joined line for single-side,
    # two ###Section,SYM,SYM lines for combined.
    bull_path = OUT_DIR / f"oi_thesis_bull_{flag_date}.txt"
    bear_path = OUT_DIR / f"oi_thesis_bear_{flag_date}.txt"
    both_path = OUT_DIR / f"oi_thesis_both_{flag_date}.txt"

    bull_path.write_text(",".join(f"NSE:{s}" for s in bulls) + "\n")
    bear_path.write_text(",".join(f"NSE:{s}" for s in bears) + "\n")
    both_path.write_text(
        f"###Bull ({flag_date})," + ",".join(f"NSE:{s}" for s in bulls) + "\n"
        f"###Bear ({flag_date})," + ",".join(f"NSE:{s}" for s in bears) + "\n"
    )

    print(f"flag_date: {flag_date}")
    print(f"Bull ({len(bulls)}): {bull_path}")
    print(f"Bear ({len(bears)}): {bear_path}")
    print(f"Both ({len(bulls) + len(bears)}): {both_path}")
    print()
    print(both_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
