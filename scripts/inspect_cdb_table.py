#!/usr/bin/env python3
"""Inspect a PCM CDB SQLite table schema."""

import argparse
import sqlite3
from pathlib import Path


def normalize_name(value):
    return ''.join(char for char in value.lower() if char.isalnum())


def find_matching_tables(conn, requested_name):
    normalized_requested = normalize_name(requested_name)
    singular_requested = normalized_requested.rstrip('s')

    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

    exact_matches = []
    fuzzy_matches = []
    for (table_name,) in rows:
        normalized_table = normalize_name(table_name)
        singular_table = normalized_table.rstrip('s')
        if normalized_table in (normalized_requested, singular_requested):
            exact_matches.append(table_name)
        elif singular_table in (normalized_requested, singular_requested):
            exact_matches.append(table_name)
        elif normalized_requested in normalized_table:
            fuzzy_matches.append(table_name)

    return exact_matches or fuzzy_matches


def describe_table(conn, table_name):
    quoted_name = table_name.replace('"', '""')
    columns = conn.execute(f'PRAGMA table_info("{quoted_name}")').fetchall()
    sample = conn.execute(f'SELECT * FROM "{quoted_name}" LIMIT 1').fetchone()
    row_count = conn.execute(f'SELECT COUNT(*) FROM "{quoted_name}"').fetchone()[0]
    return columns, sample, row_count


def quote_identifier(value):
    return value.replace('"', '""')


def find_riders(conn, search_terms=None, cdb_ids=None, firstcycling_ids=None, limit=25):
    search_terms = [term.strip() for term in (search_terms or []) if term.strip()]
    cdb_ids = [int(value) for value in (cdb_ids or [])]
    firstcycling_ids = [int(value) for value in (firstcycling_ids or [])]

    where_parts = []
    params = {}

    for index, term in enumerate(search_terms):
        param_name = f'term_{index}'
        where_parts.append(
            '('
            f'LOWER(gene_sz_firstname) LIKE :{param_name} OR '
            f'LOWER(gene_sz_lastname) LIKE :{param_name} OR '
            f'LOWER(gene_sz_firstlastname) LIKE :{param_name} OR '
            f'LOWER(TRIM(COALESCE(gene_sz_firstname, "") || " " || COALESCE(gene_sz_lastname, ""))) LIKE :{param_name}'
            ')'
        )
        params[param_name] = f'%{term.lower()}%'

    if cdb_ids:
        placeholders = []
        for index, rider_id in enumerate(cdb_ids):
            param_name = f'cdb_id_{index}'
            placeholders.append(f':{param_name}')
            params[param_name] = rider_id
        where_parts.append(f'IDcyclist IN ({", ".join(placeholders)})')

    if firstcycling_ids:
        placeholders = []
        for index, rider_id in enumerate(firstcycling_ids):
            param_name = f'fc_id_{index}'
            placeholders.append(f':{param_name}')
            params[param_name] = rider_id
        where_parts.append(f'CAST(value_f_current_ability AS INTEGER) IN ({", ".join(placeholders)})')

    if not where_parts:
        raise SystemExit('Provide --rider, --rider-id, or --firstcycling-id for rider lookup.')

    params['limit'] = limit
    query = f"""
        SELECT
            IDcyclist,
            gene_sz_firstname,
            gene_sz_lastname,
            gene_sz_firstlastname,
            CAST(value_f_current_ability AS INTEGER) AS first_cycling_id,
            fkIDteam,
            fkIDregion,
            gene_i_birthdate,
            gene_i_size,
            gene_i_weight,
            charac_i_plain,
            charac_i_mountain,
            charac_i_medium_mountain,
            charac_i_hill,
            charac_i_downhilling,
            charac_i_cobble,
            charac_i_timetrial,
            charac_i_prologue,
            charac_i_sprint,
            charac_i_acceleration,
            charac_i_endurance,
            charac_i_resistance,
            charac_i_recuperation,
            charac_i_baroudeur
        FROM DYN_cyclist
        WHERE {' OR '.join(where_parts)}
        ORDER BY gene_sz_lastname ASC, gene_sz_firstname ASC, IDcyclist ASC
        LIMIT :limit
    """
    return conn.execute(query, params).fetchall()


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

    conn = sqlite3.connect(args.sqlite_path)
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
