"""Inspect PCM CDB SQLite tables and look up riders from the command line."""

import argparse
import sqlite3
from pathlib import Path

from .api import describe_table, find_matching_tables, find_riders


def print_rider_rows(rows):
    print(f'Matching riders: {len(rows)}')
    if not rows:
        return

    print(
        'IDcyclist | First | Last | Display | FirstCycling ID | Team ID | Region ID | '
        'Birthdate | Size | Weight | FLA | MO | MM | HIL | DH | COB | TT | PRL | SPR | ACC | END | RES | REC | ATT'
    )
    print(
        '--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---'
    )
    for row in rows:
        print(
            ' | '.join(
                str(value if value is not None else '')
                for value in row
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sqlite_path', type=Path)
    parser.add_argument(
        '--table',
        default='DYN_team',
        help='Table name to look for; matching is case-insensitive and plural-tolerant.',
    )
    parser.add_argument(
        '--rider',
        action='append',
        default=[],
        help='Search DYN_cyclist by rider name. Can be supplied multiple times.',
    )
    parser.add_argument(
        '--rider-id',
        action='append',
        type=int,
        default=[],
        help='Search DYN_cyclist by IDcyclist. Can be supplied multiple times.',
    )
    parser.add_argument(
        '--firstcycling-id',
        action='append',
        type=int,
        default=[],
        help='Search DYN_cyclist by value_f_current_ability as an integer FirstCycling ID.',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=25,
        help='Maximum rider lookup rows to return.',
    )
    args = parser.parse_args()

    if not args.sqlite_path.exists():
        raise SystemExit(f'File not found: {args.sqlite_path}')

    sqlite_uri = args.sqlite_path.resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(sqlite_uri, uri=True)
    try:
        if args.rider or args.rider_id or args.firstcycling_id:
            rows = find_riders(
                conn,
                search_terms=args.rider,
                cdb_ids=args.rider_id,
                firstcycling_ids=args.firstcycling_id,
                limit=args.limit,
            )
            print_rider_rows(rows)
            return

        matches = find_matching_tables(conn, args.table)
        if not matches:
            raise SystemExit(f'No table matching {args.table!r} found.')

        print(f'Matching tables: {", ".join(matches)}')
        for table_name in matches:
            columns, sample, row_count = describe_table(conn, table_name)
            print()
            print(f'[{table_name}] rows={row_count}')
            print('cid | name | type | notnull | default | pk | sample')
            print('--- | --- | --- | --- | --- | --- | ---')
            for cid, name, column_type, notnull, default_value, pk in columns:
                sample_value = sample[cid] if sample is not None else None
                print(
                    f'{cid} | {name} | {column_type or ""} | {notnull} | '
                    f'{default_value if default_value is not None else ""} | {pk} | '
                    f'{sample_value if sample_value is not None else ""}'
                )
    finally:
        conn.close()


if __name__ == '__main__':
    main()
