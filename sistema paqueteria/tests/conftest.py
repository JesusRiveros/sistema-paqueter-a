import os
import re
import sys
import tempfile

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Las pruebas nunca usan las llaves ni la configuración de publicación del .env real: se fijan aquí
# ANTES de importar la app (load_dotenv no sobrescribe variables que ya existen).
for _var in ('GOOGLE_MAPS_API_KEY', 'ANTHROPIC_API_KEY',            # nunca llamar a Google ni a Claude
             'STRIPE_SECRET_KEY', 'STRIPE_PUBLISHABLE_KEY', 'STRIPE_WEBHOOK_SECRET',  # ni a Stripe
             'TWILIO_AUTH_TOKEN', 'TWILIO_NUMERO_HUMANO',
             'SMTP_HOST', 'SMTP_USUARIO', 'SMTP_PASSWORD',            # ni mandar correos
             'PUBLIC_BASE_URL'):
    os.environ[_var] = ''
os.environ.update(TRUST_PROXY='0', SESSION_COOKIE_SECURE='0', FLASK_DEBUG='0', PAGOS_SIMULADOS='1')
_tmp = tempfile.mkdtemp(prefix='balula-tests-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_tmp, 'test.sqlite3').replace(os.sep, '/')
os.environ['UPLOAD_FOLDER'] = os.path.join(_tmp, 'archivos')

from app import app as flask_app  # noqa: E402
import geo  # noqa: E402
from models import sa  # noqa: E402


@pytest.fixture(autouse=True)
def sin_internet(monkeypatch):
    """Ninguna prueba sale a internet: geocodificación y rutas fallan como si no hubiera red."""
    def sin_red(*a, **k):
        raise requests.ConnectionError('sin red en pruebas')
    monkeypatch.setattr(geo.requests, 'get', sin_red)
    geo._osm_geocode.cache_clear()
    geo._osrm_distancia.cache_clear()


@pytest.fixture
def app():
    flask_app.config.update(TESTING=True, GOOGLE_MAPS_API_KEY='', PAGOS_SIMULADOS=True)
    with flask_app.app_context():
        sa.drop_all()
        sa.create_all()
    yield flask_app
    with flask_app.app_context():
        sa.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf(client):
    html = client.get('/app/').get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
