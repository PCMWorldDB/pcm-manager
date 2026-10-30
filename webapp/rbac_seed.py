"""Idempotent synchronization for RBAC metadata."""

from webapp.extensions import db
from webapp.models import Action, Resource, Role, RolePermission


ROLES = [
    {
        'name': 'admin',
        'display_name': 'Administrator',
        'description': 'Full system access',
    },
    {
        'name': 'release_manager',
        'display_name': 'Release Approver',
        'description': 'Can approve and publish releases',
    },
    {
        'name': 'release_editor',
        'display_name': 'Release Editor',
        'description': 'Can create and edit releases',
    },
    {
        'name': 'stat_editor',
        'display_name': 'Stat Editor',
        'description': 'Can edit rider statistics',
    },
    {
        'name': 'stat_editor_approver',
        'display_name': 'Stat Editor Approver',
        'description': 'Can approve rider/team/race statistics edits',
    },
    {
        'name': 'user_manager',
        'display_name': 'User Manager',
        'description': 'Can create users and update user account settings',
    },
    {
        'name': 'integrations_manager',
        'display_name': 'Integrations Manager',
        'description': 'Can view and run integrations and update Discord settings',
    },
    {
        'name': 'viewer',
        'display_name': 'Viewer',
        'description': 'Read-only access to data',
    },
]


RESOURCES = [
    {
        'name': 'audit_logs',
        'description': 'View application audit history',
        'supported_actions': ['read'],
    },
    {
        'name': 'users',
        'description': 'User management',
        'supported_actions': ['create', 'read', 'update', 'delete'],
    },
    {
        'name': 'roles',
        'description': 'Role management',
        'supported_actions': ['create', 'read', 'update', 'delete'],
    },
    {
        'name': 'namespaces',
        'description': 'Namespace management',
        'supported_actions': ['create', 'read', 'update', 'delete'],
    },
    {
        'name': 'teams',
        'description': 'Manage teams and rosters',
        'supported_actions': ['create', 'read', 'delete'],
    },
    {
        'name': 'stats',
        'description': 'Maintain rider stats from race results',
        'supported_actions': ['create', 'read', 'update', 'delete', 'approve', 'download'],
    },
    {
        'name': 'releases',
        'description': 'Manage database releases',
        'supported_actions': ['create', 'read', 'update', 'delete', 'approve', 'publish', 'download', 'export'],
    },
    {
        'name': 'integrations-firstcycling',
        'description': 'FirstCycling integration settings and syncs',
        'supported_actions': ['read', 'download'],
    },
    {
        'name': 'integrations-discord',
        'description': 'Discord integration settings',
        'supported_actions': ['read', 'update'],
    },
]


ACTIONS = [
    {'name': 'create', 'description': 'Create new records'},
    {'name': 'read', 'description': 'View/read records'},
    {'name': 'update', 'description': 'Edit existing records'},
    {'name': 'delete', 'description': 'Delete records'},
    {'name': 'approve', 'description': 'Approve records or workflow transitions'},
    {'name': 'publish', 'description': 'Publish releases'},
    {'name': 'download', 'description': 'Download files'},
    {'name': 'import', 'description': 'Import data from external sources'},
    {'name': 'export', 'description': 'Export data'},
]


ROLE_PERMISSIONS = {
    'admin': {
        'audit_logs': ['read'],
        'users': ['create', 'read', 'update', 'delete'],
        'roles': ['create', 'read', 'update', 'delete'],
        'namespaces': ['create', 'read', 'update', 'delete'],
        'teams': ['create', 'read', 'delete'],
        'stats': ['create', 'read', 'update', 'delete', 'approve', 'download'],
        'releases': ['create', 'read', 'update', 'delete', 'approve', 'publish', 'download', 'export'],
        'integrations-firstcycling': ['read', 'download'],
        'integrations-discord': ['read', 'update'],
    },
    'release_manager': {
        'users': ['read'],
        'roles': ['read'],
        'namespaces': ['create', 'read'],
        'teams': ['read'],
        'stats': ['read'],
        'releases': ['read', 'approve', 'publish', 'download', 'export'],
    },
    'release_editor': {
        'users': ['read'],
        'roles': ['read'],
        'namespaces': ['read'],
        'teams': ['read'],
        'stats': ['read'],
        'releases': ['create', 'read', 'update', 'download', 'export'],
    },
    'stat_editor': {
        'users': ['read'],
        'roles': ['read'],
        'teams': ['read'],
        'stats': ['create', 'read', 'update', 'download'],
        'releases': ['read', 'download'],
    },
    'stat_editor_approver': {
        'users': ['read'],
        'roles': ['read'],
        'teams': ['read'],
        'stats': ['read', 'approve'],
        'releases': ['read', 'download'],
    },
    'user_manager': {
        'users': ['create', 'read', 'update'],
        'roles': ['read'],
    },
    'integrations_manager': {
        'users': ['read'],
        'roles': ['read'],
        'integrations-firstcycling': ['read', 'download'],
        'integrations-discord': ['read', 'update'],
    },
    'viewer': {
        'users': ['read'],
        'roles': ['read'],
        'teams': ['read'],
        'stats': ['read'],
        'releases': ['read', 'download'],
    },
}


def _sync_rows(model, rows, lookup_field, update_fields, label, log, create_defaults=None):
    synced = {}
    create_defaults = create_defaults or {}
    for row_data in rows:
        lookup_value = row_data[lookup_field]
        model_data = {
            field_name: row_data[field_name]
            for field_name in (lookup_field, *update_fields)
        }
        model_data.update(create_defaults)
        row = model.query.filter_by(**{lookup_field: lookup_value}).first()
        if row is None:
            row = model(**model_data)
            db.session.add(row)
            log(f'  Created {label}: {lookup_value}')
        else:
            updated_fields = []
            for field_name in update_fields:
                new_value = row_data[field_name]
                if getattr(row, field_name) != new_value:
                    setattr(row, field_name, new_value)
                    updated_fields.append(field_name)

            if updated_fields:
                log(f"  Updated {label}: {lookup_value} ({', '.join(updated_fields)})")
        synced[lookup_value] = row
    return synced


def _prune_rows(model, lookup_field, configured_values, label, log):
    rows = model.query.filter(~getattr(model, lookup_field).in_(configured_values)).all()
    for row in rows:
        lookup_value = getattr(row, lookup_field)
        db.session.delete(row)
        log(f'  Deleted {label}: {lookup_value}')


def _prune_permissions_for_removed_metadata(configured_role_names, configured_resource_names, log):
    permissions = (
        RolePermission.query
        .join(Role, RolePermission.role_id == Role.id)
        .join(Resource, RolePermission.resource_id == Resource.id)
        .filter(
            (~Role.name.in_(configured_role_names))
            | (~Resource.name.in_(configured_resource_names))
        )
        .all()
    )
    for permission in permissions:
        log(
            f'  Revoked {permission.role.name}: '
            f'{permission.action.name} {permission.resource.name}'
        )
        db.session.delete(permission)


def _desired_permission_keys(roles, resources, actions):
    desired_keys = set()
    for role_name, resource_actions in ROLE_PERMISSIONS.items():
        role = roles[role_name]
        for resource_name, action_names in resource_actions.items():
            resource = resources[resource_name]
            for action_name in action_names:
                action = actions[action_name]
                desired_keys.add((role.id, resource.id, action.id))
    return desired_keys


def _sync_role_permissions(roles, resources, actions, log):
    desired_keys = _desired_permission_keys(roles, resources, actions)
    role_names_by_id = {role.id: name for name, role in roles.items()}
    resource_names_by_id = {resource.id: name for name, resource in resources.items()}
    action_names_by_id = {action.id: name for name, action in actions.items()}
    existing_permissions = RolePermission.query.all()
    existing_by_key = {
        (permission.role_id, permission.resource_id, permission.action_id): permission
        for permission in existing_permissions
    }

    for permission_key, permission in existing_by_key.items():
        if permission_key not in desired_keys:
            db.session.delete(permission)
            log(
                f'  Revoked {permission.role.name}: '
                f'{permission.action.name} {permission.resource.name}'
            )

    for role_id, resource_id, action_id in desired_keys:
        if (role_id, resource_id, action_id) in existing_by_key:
            continue

        db.session.add(RolePermission(
            role_id=role_id,
            resource_id=resource_id,
            action_id=action_id,
        ))
        log(
            f'  Granted {role_names_by_id[role_id]}: '
            f'{action_names_by_id[action_id]} {resource_names_by_id[resource_id]}'
        )


def _supported_actions_by_resource():
    return {
        resource['name']: set(resource['supported_actions'])
        for resource in RESOURCES
    }


def _validate_role_permissions():
    supported_actions = _supported_actions_by_resource()
    configured_actions = {action['name'] for action in ACTIONS}
    configured_resources = set(supported_actions)
    configured_roles = {role['name'] for role in ROLES}

    for role_name, resource_actions in ROLE_PERMISSIONS.items():
        if role_name not in configured_roles:
            raise ValueError(f'Role permissions reference unknown role: {role_name}')

        for resource_name, action_names in resource_actions.items():
            if resource_name not in configured_resources:
                raise ValueError(f'{role_name} permissions reference unknown resource: {resource_name}')

            for action_name in action_names:
                if action_name not in configured_actions:
                    raise ValueError(f'{role_name}:{resource_name} references unknown action: {action_name}')
                if action_name not in supported_actions[resource_name]:
                    raise ValueError(
                        f'{role_name}:{resource_name} grants unsupported action: {action_name}'
                    )


def sync_rbac_metadata(log=None):
    """Synchronize roles, resources, actions, and role permissions."""
    if log is None:
        log = lambda _: None
    _validate_role_permissions()

    configured_role_names = {role['name'] for role in ROLES}
    configured_resource_names = {resource['name'] for resource in RESOURCES}
    _prune_permissions_for_removed_metadata(configured_role_names, configured_resource_names, log)
    _prune_rows(Role, 'name', configured_role_names, 'role', log)
    _prune_rows(Resource, 'name', configured_resource_names, 'resource', log)
    db.session.flush()

    roles = _sync_rows(
        Role,
        ROLES,
        lookup_field='name',
        update_fields=('display_name', 'description'),
        label='role',
        log=log,
    )
    resources = _sync_rows(
        Resource,
        RESOURCES,
        lookup_field='name',
        update_fields=('description',),
        label='resource',
        log=log,
        create_defaults={'app_panel': 'rbac'},
    )
    actions = _sync_rows(
        Action,
        ACTIONS,
        lookup_field='name',
        update_fields=('description',),
        label='action',
        log=log,
    )

    db.session.commit()
    _sync_role_permissions(roles, resources, actions, log)

    db.session.commit()
    log('RBAC metadata synchronized successfully!')
