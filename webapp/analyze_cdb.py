"""Analyze SQLite CDB file and generate field mappings

This script connects to a PCM CDB (SQLite) file, analyzes its structure,
and helps generate YAML mappings for cdb_mappings.yaml
"""
import sqlite3
import sys
import os
from collections import defaultdict


def analyze_cdb_structure(sqlite_path):
    """Analyze the structure of a CDB SQLite file
    
    Args:
        sqlite_path: Path to the SQLite CDB file
        
    Returns:
        dict: Structure information about tables and columns
    """
    if not os.path.exists(sqlite_path):
        print(f"Error: File not found: {sqlite_path}")
        sys.exit(1)
    
    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()
    
    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    
    print(f"\n{'='*80}")
    print(f"CDB Analysis: {os.path.basename(sqlite_path)}")
    print(f"{'='*80}\n")
    print(f"Found {len(tables)} tables\n")
    
    # Analyze each table
    table_info = {}
    
    for table_name in tables:
        # Get column information
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()
        
        # Get row count
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cursor.fetchone()[0]
        
        table_info[table_name] = {
            'columns': columns,
            'row_count': row_count
        }
        
        print(f"Table: {table_name} ({row_count} rows)")
        print(f"{'-'*80}")
        for col in columns:
            col_id, col_name, col_type, not_null, default_val, is_pk = col
            pk_marker = " [PK]" if is_pk else ""
            null_marker = " NOT NULL" if not_null else ""
            print(f"  {col_name:<40} {col_type:<15}{pk_marker}{null_marker}")
        print()
    
    conn.close()
    return table_info


def generate_cyclist_mapping(table_info):
    """Generate YAML mapping for cyclist table
    
    Args:
        table_info: Table structure information
        
    Returns:
        str: YAML mapping snippet
    """
    # Look for cyclist table (could be DYN_cyclist or similar)
    # Prefer tables that are exactly "DYN_cyclist" or have "cyclist" without "precontract", "transfer", etc.
    cyclist_tables = [t for t in table_info.keys() if 'cyclist' in t.lower()]
    
    if not cyclist_tables:
        print("\nWarning: No cyclist table found!")
        return None
    
    # Prefer exact match or main cyclist table
    preferred = [t for t in cyclist_tables if t.lower() == 'dyn_cyclist']
    if preferred:
        cyclist_table = preferred[0]
    else:
        # Filter out auxiliary tables
        main_tables = [t for t in cyclist_tables if not any(x in t.lower() for x in ['precontract', 'transfer', 'backup', 'temp'])]
        cyclist_table = main_tables[0] if main_tables else cyclist_tables[0]
    columns = table_info[cyclist_table]['columns']
    
    print(f"\n{'='*80}")
    print(f"Suggested Mapping for {cyclist_table}")
    print(f"{'='*80}\n")
    
    yaml_output = []
    yaml_output.append(f"PCM????:  # Replace with actual version")
    yaml_output.append(f"  cyclist:")
    yaml_output.append(f"    table_name: {cyclist_table}")
    yaml_output.append(f"    fields:")
    
    # Our internal model field names (in order)
    internal_fields = [
        'id', 'first_name', 'last_name', 'nation',
        'birthdate', 'birth_year', 'birth_month', 'birth_day',
        'weight', 'height', 'rider_type', 'potential',
        'mountain', 'hill', 'time_trial', 'prologue', 'flat',
        'sprint', 'acceleration', 'endurance', 'resistance', 'recovery',
        'cobbles', 'attack', 'fighter', 'level', 'training_program',
        'team_id', 'year_pro', 'contract_year', 'wage', 'is_locked'
    ]
    
    # Map of common CDB field patterns to internal fields
    field_patterns = {
        'id': ['IDcyclist', 'id', 'cyclist_id'],
        'first_name': ['gene_sz_firstname', 'gene_sz_first_name', 'firstname', 'first_name'],
        'last_name': ['gene_sz_lastname', 'gene_sz_last_name', 'lastname', 'last_name'],
        'nation': ['gene_sz_nation', 'nationality', 'nation'],
        'birthdate': ['gene_i_birthdate', 'birthdate', 'birth_date'],
        'birth_year': ['gene_i_birth_year', 'birth_year', 'year_birth'],
        'birth_month': ['gene_i_birth_month', 'birth_month', 'month_birth'],
        'birth_day': ['gene_i_birth_day', 'birth_day', 'day_birth'],
        'weight': ['gene_i_weight', 'weight'],
        'height': ['gene_i_height', 'height'],
        'rider_type': ['gene_i_type', 'type', 'rider_type'],
        'potential': ['gene_i_potential', 'potential'],
        'mountain': ['gene_i_mountain', 'mountain', 'mo'],
        'hill': ['gene_i_hill', 'hill', 'hi'],
        'time_trial': ['gene_i_tt', 'tt', 'time_trial'],
        'prologue': ['gene_i_prologue', 'prologue', 'pro'],
        'flat': ['gene_i_flat', 'flat', 'fl'],
        'sprint': ['gene_i_sprint', 'sprint', 'sp'],
        'acceleration': ['gene_i_acceleration', 'acceleration', 'acc'],
        'endurance': ['gene_i_endurance', 'endurance', 'end'],
        'resistance': ['gene_i_resistance', 'resistance', 'res'],
        'recovery': ['gene_i_recovery', 'recovery', 'rec'],
        'cobbles': ['gene_i_cobbles', 'cobbles', 'cob'],
        'attack': ['gene_i_attack', 'attack', 'att'],
        'fighter': ['gene_i_fighter', 'fighter', 'fig'],
        'level': ['gene_i_level', 'level', 'lvl'],
        'training_program': ['gene_sz_program', 'program', 'training_program'],
        'team_id': ['gene_fkIDteam', 'fkIDteam', 'team_id', 'id_team'],
        'year_pro': ['gene_i_year_pro', 'year_pro', 'pro_year'],
        'contract_year': ['gene_i_contract_year', 'contract_year', 'contract'],
        'wage': ['gene_i_wage', 'wage', 'salary'],
        'is_locked': ['gene_b_locked', 'locked', 'is_locked']
    }
    
    # Get actual column names from CDB
    cdb_columns = {col[1].lower(): col[1] for col in columns}
    
    # Try to match internal fields to CDB columns
    for internal_field in internal_fields:
        matched = False
        patterns = field_patterns.get(internal_field, [])
        
        for pattern in patterns:
            if pattern.lower() in cdb_columns:
                actual_column = cdb_columns[pattern.lower()]
                yaml_output.append(f"      {internal_field}: {actual_column}")
                matched = True
                break
        
        if not matched:
            yaml_output.append(f"      {internal_field}: ???  # NOT FOUND - Update manually")
    
    yaml_str = '\n'.join(yaml_output)
    print(yaml_str)
    print()
    
    return yaml_str


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python analyze_cdb.py <path_to_sqlite_file>")
        print("\nExample:")
        print("  python analyze_cdb.py /app/data/namespaces/wdb2026/cdb/import/WDB_2026.sqlite")
        print("  python analyze_cdb.py ./pcm_database.sqlite")
        sys.exit(1)
    
    sqlite_path = sys.argv[1]
    
    # Analyze structure
    table_info = analyze_cdb_structure(sqlite_path)
    
    # Generate cyclist mapping
    cyclist_mapping = generate_cyclist_mapping(table_info)
    
    if cyclist_mapping:
        # Suggest output file
        output_file = 'cdb_mapping_suggestion.yaml'
        with open(output_file, 'w') as f:
            f.write(cyclist_mapping)
        print(f"Mapping suggestion saved to: {output_file}")
        print("Review and add to webapp/models/ns/cdb_mappings.yaml")


if __name__ == '__main__':
    main()
