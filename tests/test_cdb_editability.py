"""Manual edit allowlists fail closed and protect direct stats requests."""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.integrations.cdb import editability
from webapp.models import User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


def test_default_editable_fields():
    assert editability.is_field_editable('rider', 'stat_mo')
    for entity, attribute in [('rider', 'id'), ('rider', 'first_name'),
                              ('team', 'budget'), ('race', 'name'), ('unknown', 'name')]:
        assert not editability.is_field_editable(entity, attribute)


@pytest.mark.parametrize('policy', [None, [], {'rider': True}, {'rider': ['id']},
                                    {'rider': ['is_deleted']}, {'rider': ['missing']},
                                    {'race': ['name']}, {'unknown': []}])
def test_invalid_policy_fails_closed(monkeypatch, policy):
    monkeypatch.setattr(editability.yaml, 'safe_load', lambda stream: policy)
    with pytest.raises(ValueError):
        editability.get_editable_fields('rider')


@pytest.mark.parametrize('editable', [True, False])
def test_direct_individual_save_respects_field_policy(app, client, monkeypatch, editable):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    monkeypatch.setattr(routes, '_can_update_stats', lambda: True)
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: SimpleNamespace())
    monkeypatch.setattr(routes, '_load_namespace_roster_rider', lambda ns, rider: object())
    monkeypatch.setattr(editability.yaml, 'safe_load',
                        lambda stream: {'rider': ['stat_mo'] if editable else []})

    url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
    create_database(url)
    engine = create_engine(url)
    try:
        NamespaceBase.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text('INSERT INTO rider(id,stat_mo) VALUES (1,70)'))
        namespace = SimpleNamespace(database_name=url.database, id=91001,
                                    build_connection_string=lambda: url)
        monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
        payload = {'changes_json': '[{"first_cycling_rider_id":1,"stat_name":"mo","final_value":72}]'}
        response = client.post('/riders/1/stats/save', data=payload)
        assert response.status_code == (302 if editable else 403)
        with engine.connect() as conn:
            assert conn.scalar(text('SELECT COUNT(*) FROM stat_changes')) == int(editable)
    finally:
        engine.dispose()
        drop_database(url)
