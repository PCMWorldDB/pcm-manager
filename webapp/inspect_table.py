#!/usr/bin/env python3
"""Print column names and information for SQLite database tables

Usage:
    python inspect_table.py <database_path> <table_name>
    
Example:
    python inspect_table.py /app/data/namespaces/wdb2026/cdb/import/WDB_2026.sqlite DYN_cyclist
"""
import sqlite3
import sys


def inspect_table(db_path, table_name):
    """Print column information for a specific table
    
    Args:
        db_path: Path to SQLite database
        table_name: Name of the table to inspect
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        if not cursor.fetchone():
            print(f"Error: Table '{table_name}' not found in database")
            print("\nAvailable tables:")
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            for row in cursor.fetchall():
                print(f"  - {row[0]}")
            conn.close()
            return 1
        
        # Get column information
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()
        
        # Get row count
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cursor.fetchone()[0]
        
        # Print header
        print(f"\n{'='*80}")
        print(f"Table: {table_name}")
        print(f"Rows: {row_count:,}")
        print(f"{'='*80}\n")
        
        # Print column information
        print(f"{'Column Name':<40} {'Type':<15} {'Flags'}")
        print(f"{'-'*80}")
        
        for col in columns:
            col_id, col_name, col_type, not_null, default_val, is_pk = col
            
            flags = []
            if is_pk:
                flags.append("PRIMARY KEY")
            if not_null:
                flags.append("NOT NULL")
            if default_val is not None:
                flags.append(f"DEFAULT {default_val}")
            
            flags_str = ", ".join(flags) if flags else ""
            print(f"{col_name:<40} {col_type:<15} {flags_str}")
        
        print()
        
        # Print column names as comma-separated list (useful for SQL queries)
        print("Column names (comma-separated):")
        print(", ".join([col[1] for col in columns]))
        print()
        
        # Print column names as YAML mapping template
        print("\nYAML field mapping template:")
        for col in columns:
            col_name = col[1]
            # Try to suggest internal field name
            internal_name = col_name.lower().replace('gene_sz_', '').replace('gene_i_', '').replace('gene_f_', '').replace('gene_b_', '').replace('fkid', '').replace('value_', '')
            print(f"      # {internal_name}: {col_name}")
        
        conn.close()
        return 0
        
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def main():
    """Main entry point"""
    if len(sys.argv) != 3:
        print(__doc__)
        print("\nError: Missing required arguments")
        return 1
    
    db_path = sys.argv[1]
    table_name = sys.argv[2]
    
    return inspect_table(db_path, table_name)


if __name__ == '__main__':
    sys.exit(main())
