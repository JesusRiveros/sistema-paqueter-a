import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['GOOGLE_MAPS_API_KEY'] = ''  # las pruebas nunca llaman a Google

from app import app as flask_app  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def app(tmp_path):
    flask_app.config.update(TESTING=True, DATABASE=str(tmp_path / 'test.sqlite3'),
                            GOOGLE_MAPS_API_KEY='', PAGOS_SIMULADOS=True)
    with flask_app.app_context():
        db.init_db()
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf(client):
    html = client.get('/app/').get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
