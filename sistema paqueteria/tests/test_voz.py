"""Voz en la web (cambio de canal) y llamadas por Twilio (simuladas: sin red ni cuenta real)."""
from datetime import date, timedelta

import pytest

import asistente
import db
import voz_twilio
from models import Conversacion, Notificacion, sa
from test_asistente import ClaudeFalso, herramienta, respuesta, texto

TOKEN = 'token_de_prueba_twilio'
URL_BASE = 'https://balula.example.com'
CALL = 'CA' + 'a1b2c3d4' * 4


@pytest.fixture
def twilio(app, monkeypatch):
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', TOKEN)
    monkeypatch.setenv('TWILIO_NUMERO_HUMANO', '+525500000000')
    app.config['PUBLIC_BASE_URL'] = URL_BASE
    yield
    app.config['PUBLIC_BASE_URL'] = ''


@pytest.fixture
def claude():
    def instalar(*guion):
        falso = ClaudeFalso(guion)
        asistente.asignar_cliente(falso)
        return falso
    yield instalar
    asistente.asignar_cliente(None)


def llamar(client, ruta, datos, firmar=True):
    datos = {'CallSid': CALL, 'From': '+525511112222'} | datos
    firma = voz_twilio.firma_esperada(URL_BASE + ruta, datos, TOKEN) if firmar else 'invalida'
    return client.post(ruta, data=datos, headers={'X-Twilio-Signature': firma})


# ---------------------------------------------------------------- Twilio

def test_firma_algoritmo_y_sensibilidad():
    a = voz_twilio.firma_esperada('https://x.com/a', {'B': '2', 'A': '1'}, 'tok')
    assert a == voz_twilio.firma_esperada('https://x.com/a', {'A': '1', 'B': '2'}, 'tok')  # orden de parámetros no importa
    assert a != voz_twilio.firma_esperada('https://x.com/a', {'A': '1', 'B': '3'}, 'tok')
    assert a != voz_twilio.firma_esperada('https://x.com/a', {'A': '1', 'B': '2'}, 'otro')


def test_rechaza_peticiones_sin_firma_valida(client, twilio):
    assert llamar(client, '/voz/twilio/entrante', {}, firmar=False).status_code == 403


def test_sin_token_configurado_esta_desactivado(client, app, monkeypatch):
    monkeypatch.delenv('TWILIO_AUTH_TOKEN', raising=False)
    assert client.post('/voz/twilio/entrante', data={'CallSid': CALL}).status_code == 403


def test_llamada_entrante_saluda_y_escucha(client, twilio, app):
    r = llamar(client, '/voz/twilio/entrante', {})
    xml = r.get_data(as_text=True)
    assert r.mimetype == 'text/xml' and 'Soy Lula' in xml and '<Gather input="speech" language="es-MX"' in xml
    with app.app_context():
        conv = sa.session.get(Conversacion, 'tw' + ('a1b2c3d4' * 4)[:30])
        assert conv.canal == 'telefono'


def test_turno_responde_con_voz_y_sigue_escuchando(client, twilio, claude, app):
    falso = claude(respuesta(texto('Claro, ¿me dictas tu número de guía?')))
    llamar(client, '/voz/twilio/entrante', {})
    xml = llamar(client, '/voz/twilio/turno', {'SpeechResult': 'quiero rastrear mi paquete'}).get_data(as_text=True)
    assert '¿me dictas tu número de guía?' in xml and '<Gather' in xml
    assert 'Canal: llamada telefónica' in falso.llamadas[0]['system']
    assert falso.llamadas[0]['messages'][-1]['content'] == 'quiero rastrear mi paquete'


def test_silencio_reintenta_y_luego_cuelga(client, twilio):
    xml = llamar(client, '/voz/twilio/turno', {'SpeechResult': ''}).get_data(as_text=True)
    assert 'no te escuché' in xml and 'silencios=1' in xml
    xml = llamar(client, '/voz/twilio/turno?silencios=2', {'SpeechResult': ''}).get_data(as_text=True)
    assert '<Hangup/>' in xml


def test_escalamiento_transfiere_la_llamada(client, twilio, claude):
    claude(respuesta(herramienta('transferir_a_humano', {'motivo': 'cliente_pide_humano', 'resumen': 'Pide persona',
                                                         'telefono_contacto': '5511112222'})),
           respuesta(texto('Entiendo.')))
    xml = llamar(client, '/voz/twilio/turno', {'SpeechResult': 'quiero hablar con una persona'}).get_data(as_text=True)
    assert '<Dial>+525500000000</Dial>' in xml


def test_sin_api_key_de_claude_transfiere(client, twilio, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', '')
    asistente.asignar_cliente(None)
    xml = llamar(client, '/voz/twilio/turno', {'SpeechResult': 'hola'}).get_data(as_text=True)
    assert 'no puedo atenderte' in xml and '<Dial>' in xml


def test_texto_se_escapa_en_xml(client, twilio, claude):
    claude(respuesta(texto('Precio <total> & más')))
    xml = llamar(client, '/voz/twilio/turno', {'SpeechResult': 'hola'}).get_data(as_text=True)
    assert 'Precio &lt;total&gt; &amp; más' in xml


# ---------------------------------------------------------------- Voz en la web

def test_chat_cambia_a_canal_voz(client, csrf, claude, app):
    falso = claude(respuesta(texto('Hola')), respuesta(texto('Hola de nuevo')))
    client.post('/api/asistente/mensaje', json={'texto': 'hola', 'canal': 'web'}, headers={'X-CSRF-Token': csrf})
    client.post('/api/asistente/mensaje', json={'texto': 'hola', 'canal': 'voz'}, headers={'X-CSRF-Token': csrf})
    assert 'Canal: chat del sitio web' in falso.llamadas[0]['system']
    assert 'Canal: voz' in falso.llamadas[1]['system']


def test_widget_tiene_microfono(client):
    html = client.get('/').get_data(as_text=True)
    assert 'id="lula-mic"' in html and 'id="lula-voz"' in html


def test_enlace_de_foto_por_correo(client, csrf, claude, app):
    with app.app_context():
        guia = db.crear_envio(remitente={'nombre': 'A', 'telefono': '5500001111', 'ciudad': 'X', 'estado': 'Y'},
                              destinatario={'nombre': 'Luis', 'telefono': '5511223344'},
                              cotizacion={'total': 100, 'fecha_min': date.today().isoformat(), 'fecha_max': date.today().isoformat(),
                                          'tipo_envio': 'eco', 'conceptos': []},
                              tipo_entrega='recolector', correo_destinatario=None)
    claude(respuesta(herramienta('evaluar_compensacion', {'guia': guia, 'ultimos4_telefono': '3344', 'problema': 'danado',
                                                          'detalle': 'Llegó roto', 'correo_contacto': 'luis@ejemplo.com'})),
           respuesta(texto('Te mandé el enlace a tu correo.')))
    client.post('/api/asistente/mensaje', json={'texto': 'llegó roto', 'canal': 'voz'}, headers={'X-CSRF-Token': csrf})
    with app.app_context():
        n = sa.session.query(Notificacion).filter_by(para='luis@ejemplo.com').one()
        assert '/evidencia/' in n.cuerpo


def test_firma_coincide_con_ejemplo_oficial_de_twilio():
    p = {'CallSid': 'CA1234567890ABCDE', 'Caller': '+12349013030', 'Digits': '1234', 'From': '+12349013030', 'To': '+18005551212'}
    assert voz_twilio.firma_esperada('https://mycompany.com/myapp.php?foo=1&bar=2', p, '12345') == '0/KCTR6DLpKmkAf8muzZqo1nDgQ='
