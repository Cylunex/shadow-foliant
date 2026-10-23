"""Read stored selection cohorts without fetching data or updating outcomes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: E402,F401


def read_performance(store, *, start_date, end_date, as_of, horizon_days=1):
    from analysis.selection_performance import summarize_selection_performance
    # Validate before opening a database connection.
    window = summarize_selection_performance([], start_date=start_date, end_date=end_date,
                                             as_of=as_of, horizon_days=horizon_days)
    start_date, end_date, as_of = (window[key] for key in ('start_date', 'end_date', 'as_of'))
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT n.nomination_id,n.selection_run_id,n.symbol,n.strategy_id,
                   n.strategy_version,n.lane,r.data_as_of AS selection_date,
                   o.horizon_days,o.outcome_status,o.entry_date,o.exit_date,
                   o.return_pct,o.metric_version
            FROM selection_candidate_nominations n
            JOIN selection_strategy_runs r ON r.strategy_run_id=n.strategy_run_id
            LEFT JOIN selection_candidate_outcomes o
              ON o.nomination_id=n.nomination_id AND o.horizon_days=?
            WHERE r.data_as_of>=? AND r.data_as_of<=?
            ORDER BY r.data_as_of,n.nomination_id""", (horizon_days, start_date, end_date))
        columns = [c[0] for c in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()
    return summarize_selection_performance(rows, start_date=start_date, end_date=end_date,
                                           as_of=as_of, horizon_days=horizon_days)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--as-of', required=True, help='Inclusive outcome exit-date cutoff')
    parser.add_argument('--horizon', type=int, default=1)
    args = parser.parse_args()
    from data.research_store import ResearchStore
    result = read_performance(ResearchStore(ensure_schema=False), start_date=args.start,
                             end_date=args.end, as_of=args.as_of, horizon_days=args.horizon)
    # _bootstrap decorates print() with log timestamps; exports must remain JSON.
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
