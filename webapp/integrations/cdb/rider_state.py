"""Refresh derived rider flags in the caller's namespace transaction."""
from sqlalchemy import text


def refresh_pending_flags(conn, rider_ids=None):
    """Recompute across all scopes, including changes outside this approval.

    Lock before reading counts in a separate statement so concurrent refreshes
    see changes committed by the previous lock holder at READ COMMITTED.
    None refreshes the namespace; an empty collection does nothing.
    """
    if rider_ids is not None:
        rider_ids = sorted({int(rider_id) for rider_id in rider_ids if rider_id is not None})
        if not rider_ids:
            return
    params = {'ids': rider_ids}
    conn.execute(text('''
        SELECT id FROM rider WHERE CAST(:ids AS INTEGER[]) IS NULL OR id = ANY(:ids)
        ORDER BY id FOR UPDATE
    '''), params).all()
    conn.execute(text('''
        UPDATE rider r SET
            has_pending_stat_changes = EXISTS (
                SELECT 1 FROM stat_changes s WHERE s.rider_id = r.id AND s.is_approved = FALSE
            ) OR EXISTS (
                SELECT 1 FROM cdb_changes c WHERE c.entity_type = 'rider' AND c.entity_id = r.id
                AND c.status = 'pending' AND c.applied_at IS NULL AND LEFT(c.attribute_name,5) = 'stat_'
            ),
            has_pending_attribute_changes = EXISTS (
                SELECT 1 FROM cdb_changes c WHERE c.entity_type = 'rider' AND c.entity_id = r.id
                AND c.status = 'pending' AND c.applied_at IS NULL AND LEFT(c.attribute_name,5) <> 'stat_'
            )
        WHERE CAST(:ids AS INTEGER[]) IS NULL OR r.id = ANY(:ids)
    '''), params)
