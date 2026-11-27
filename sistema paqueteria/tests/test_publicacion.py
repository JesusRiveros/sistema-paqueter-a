"""Publicación detrás de Cloudflare Tunnel: proxy, dominio canónico, salud y respaldos."""
import os

import pytest
from werkzeug.middleware.proxy_fix import ProxyFix

import voz_twilio

DOMINIO = 'https://balulaexpress.com'


@pytest.fixture
def detras_de_proxy(app):
    original = app.wsgi_app
    app.wsgi_app = ProxyFix(original, x_for=1, x_proto=1, x_host=1)
    app.config['PUBLIC_BASE_URL'] = DOMINIO
    yield
    app.wsgi_app = original
    app.config['PUBLIC_BASE_URL'] = ''


CABECERAS_TUNEL = {'Host': 'balulaexpress.com', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '201.141.10.20'}


def test_salud(client):
    r = client.get('/salud').get_json()
    assert r['ok'] is True and r['base_de_datos'] == 'ok' and r['pagos'] == 'simulado'
    assert 'sk_' not in str(r) and 'whsec' not in str(r)


def test_detras_del_tunel_la_app_se_ve_como_https(client, detras_de_proxy):
    r = client.get('/salud', headers=CABECERAS_TUNEL).get_json()
    assert r['https'] is True and r['url_publica'] == DOMINIO


def test_www_redirige_al_dominio_principal(client, detras_de_proxy):
    r = client.get('/rastreo?guia=BLL-12345678', headers=CABECERAS_TUNEL | {'Host': 'www.balulaexpress.com'})
    assert r.status_code == 301 and r.location == f'{DOMINIO}/rastreo?guia=BLL-12345678'
    assert client.get('/', headers=CABECERAS_TUNEL).status_code == 200


def test_firma_de_twilio_detras_del_tunel(client, detras_de_proxy, monkeypatch):
    """Twilio firma la URL pública https; la app la reconstruye con PUBLIC_BASE_URL aunque cloudflared llegue por http."""
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'tok')
    datos = {'CallSid': 'CA' + 'f' * 32}
    firma = voz_twilio.firma_esperada(f'{DOMINIO}/voz/twilio/entrante', datos, 'tok')
    r = client.post('/voz/twilio/entrante', data=datos, headers=CABECERAS_TUNEL | {'X-Twilio-Signature': firma})
    assert r.status_code == 200 and '<Gather' in r.get_data(as_text=True)


def test_url_de_regreso_de_stripe_usa_el_dominio_publico(client, csrf, app, detras_de_proxy, monkeypatch):
    import pagos
    from test_flujo import hasta_pago
    from test_stripe import StripeFalso
    falso = StripeFalso()
    pagos.asignar_cliente(falso)
    try:
        hasta_pago(client, csrf)
        client.post('/app/procesar_pago', data={'_csrf': csrf}, headers={'X-Forwarded-Proto': 'https'})
        params = falso.llamadas[0][1]
        assert params['success_url'].startswith(f'{DOMINIO}/app/pago/exito?session_id=')
        assert params['cancel_url'] == f'{DOMINIO}/app/pago/cancelado'
    finally:
        pagos.asignar_cliente(None)


def test_respaldo_de_la_base(app, tmp_path):
    r = app.test_cli_runner().invoke(args=['respaldar-base'])
    assert 'Respaldo creado' in r.output
    ruta = r.output.split('Respaldo creado:')[1].strip()
    assert os.path.getsize(ruta) > 0
    os.remove(ruta)
