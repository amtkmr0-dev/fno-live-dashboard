import sqlite3
import pandas as pd
import sys
from pathlib import Path
import datetime
import os

def export_timeseries():
    # Paths
    base_dir = Path(__file__).parent
    db_path = base_dir / "data" / "quantra_history.db"
    export_dir = base_dir / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    # Use today's date if not specified
    today_str = datetime.date.today().isoformat()
    # If passed as arg, use it
    if len(sys.argv) > 1:
        today_str = sys.argv[1]

    # Query
    query = f"""
        SELECT *
        FROM nifty_timeseries
        WHERE trading_date = '{today_str}'
        ORDER BY snap_ts ASC
    """

    print(f"Connecting to {db_path} to extract data for {today_str}...")
    try:
        conn = sqlite3.connect(str(db_path))
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            print(f"No Nifty timeseries data found for {today_str}.")
            return

        # Export as compressed CSV
        output_file = export_dir / f"nifty_timeseries_{today_str}.csv.gz"
        print(f"Exporting {len(df)} rows to {output_file}...")
        df.to_csv(output_file, index=False, compression='gzip')
        print("Export complete.")
        
    except Exception as e:
        print(f"Error during export: {e}")
        sys.exit(1)

if __name__ == "__main__":
    export_timeseries()
