"""Export the top-4 bullish + top-4 bearish (live OI thesis) as a
TradingView-importable watchlist text file.

TradingView .txt watchlist format:
  ###Section Name
  EXCHANGE:SYMBOL
  EXCHANGE:SYMBOL

Output: data/exports/qt_top4_each_<YYYY-MM-DD>.txt
"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PICKS = Path("/tmp/qt_top8.json")
OUT_DIR = ROOT / "data" / "exports"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    if not PICKS.exists():
        print("ERROR: run scripts/_pick_top8.py first to populate /tmp/qt_top8.json")
        return 1

    with open(PICKS) as f:
        picks = json.load(f)

    today = datetime.now().strftime("%Y-%m-%d")
    out_path = OUT_DIR / f"qt_top4_each_{today}.txt"

    bull = [p["sym"] for p in picks.get("bull", [])]
    bear = [p["sym"] for p in picks.get("bear", [])]

    with open(out_path, "w") as f:
        f.write(f"###Quantra Top 4 Bull — {today}\n")
        for sym in bull:
            f.write(f"NSE:{sym}\n")
        f.write(f"\n###Quantra Top 4 Bear — {today}\n")
        for sym in bear:
            f.write(f"NSE:{sym}\n")
        # Combined section for one-shot scanning
        f.write(f"\n###Quantra Top 8 Combined — {today}\n")
        for sym in bull + bear:
            f.write(f"NSE:{sym}\n")

    print(f"Wrote {len(bull) + len(bear)} symbols → {out_path}")
    print()
    print(out_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
