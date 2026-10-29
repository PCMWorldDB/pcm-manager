from pathlib import Path

import yaml


# Load PCM Version Field Mappings from YAML
# Maps PCM CDB fields to internal model fields
def get_cdb_mappings():
    """Load CDB mappings from YAML file
    
    Returns:
        dict: Complete mapping configuration with PCM_VERSION_MAPPINGS and CDB_TABLE_MAPPINGS
    """
    yaml_path = Path(__file__).with_name('cdb_mappings.yaml')
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)


def get_field_mapping(pcm_version, table_type='cyclist'):
    """Get field mapping for a specific PCM version and table type
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        dict: Mapping configuration with 'cdb_table_name' and 'fields'
    """
    cdb_mappings = get_cdb_mappings()
    
    # Get the mapping version for this PCM version and table type
    version_mappings = cdb_mappings.get('PCM_VERSION_MAPPINGS', {})
    pcm_config = version_mappings.get(pcm_version.upper())
    if not pcm_config:
        raise ValueError(f"Unsupported PCM version: {pcm_version}")
    
    mapping_version = pcm_config.get(table_type)
    if not mapping_version:
        raise ValueError(f"Table type '{table_type}' not defined for PCM version {pcm_version}")
    
    # Get the actual table mapping from CDB_TABLE_MAPPINGS
    table_mappings = cdb_mappings.get('CDB_TABLE_MAPPINGS', {})
    version_config = table_mappings.get(mapping_version)
    if not version_config:
        raise ValueError(f"Mapping version '{mapping_version}' not found in CDB_TABLE_MAPPINGS")
    
    table_mapping = version_config.get(table_type)
    if not table_mapping:
        raise ValueError(f"Table type '{table_type}' not defined in mapping version '{mapping_version}'")
    
    return table_mapping


def get_cdb_table_name(pcm_version, table_type='cyclist'):
    """Get the CDB table name for a specific PCM version
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        str: CDB table name
    """
    mapping = get_field_mapping(pcm_version, table_type)
    return mapping['cdb_table_name']
