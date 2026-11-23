"""Pruebas del asistente con un cliente de Claude simulado (no gasta tokens ni necesita API key)."""
import copy
import json
from types import SimpleNamespace as NS

import anthropic
import httpx2 as httpx
import pytest

import asistente
import db
from test_flujo import hasta_pago


# ---------------------------------------------------------------- cliente simulado

def texto(t):
    return NS(type='text', text=t)


def herramienta(nombre, entrada, id_='tu_1'):
    return NS(type='tool_use', name=nombre, input=entrada, id=id_)


def respuesta(*bloques, stop=None):
    if stop is None:
        stop = 'tool_use' if any(b.type == 'tool_use' for b in bloques) else 'end_turn'
    return NS(content=list(bloques), stop_reason=stop,
              usage=NS(input_tokens=1000, output_tokens=50, cache_read_input_tokens=2000,
                       cache_creation_input_tokens=0))


class ClaudeFalso:
    def __init__(self, guion):
        self.guion = list(guion)
        self.llamadas = []
        self.messages = self

    def create(self, **kwargs):
        self.llamadas.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != 'tools'}) | {'tools': kwargs['tools']})
        siguiente = self.guion.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente


@pytest.fixture
def claude(app):
    def instalar(*guion):
        falso = ClaudeFalso(guion)
        asistente.asignar_cliente(falso)
        return falso
    yield instalar
    asistente.asignar_cliente(None)


def enviar(client, csrf, texto_, canal='web'):
    return client.post('/api/asistente/mensaje', json={'texto': texto_, 'canal': canal},
                       headers={'X-CSRF-Token': csrf})


def crear_envio(client, csrf):
    hasta_pago(client, csrf)
    r = client.post('/app/checkout', data={'_csrf': csrf, 'resultado': 'success'})
    return r.location.rsplit('/', 1)[1]


# ---------------------------------------------------------------- pruebas

def test_esquemas_estrictos_validos():
    for h in asistente.HERRAMIENTAS:
        s = h['input_schema']
        assert h['strict'] is True
        assert s['additionalProperties'] is False
        assert sorted(s['required']) == sorted(s['properties'])
        assert h['name'] in asistente.EJECUTORES


def test_respuesta_simple_y_registro_de_costos(client, csrf, claude, app):
    falso = claude(respuesta(texto('¡Hola! ¿En qué te ayudo?')))
    r = enviar(client, csrf, 'hola')
    assert r.get_json() == {'ok': True, 'texto': '¡Hola! ¿En qué te ayudo?', 'escalado': False}

    llamada = falso.llamadas[0]
    assert llamada['model'] == 'claude-haiku-5-5'
    assert llamada['output_config'] == {'effort': 'low'}
    assert llamada['cache_control'] == {'type': 'ephemeral'}
    assert 'temperature' not in llamada
    assert llamada['messages'] == [{'role': 'user', 'content': 'hola'}]
    assert 'Canal: chat del sitio web' in llamada['system']

    with app.app_context():
        costos = db.resumen_costos_asistente()
    assert costos['conversaciones'] == 1 and costos['turnos'] == 1
    assert costos['entrada'] == 1000 and costos['cache_lectura'] == 2000
    assert costos['costo'] == pytest.approx((1000 * .10 + 2000 * .01 + 50 * .50) / 1e6)


def test_rastreo_con_herramienta(client, csrf, claude):
    guia = crear_envio(client, csrf)
    falso = claude(
        respuesta(herramienta('rastrear_envio', {'guia': guia.lower()})),
        respuesta(texto(f'Tu paquete {guia} ya tiene guía generada.')),
    )
    r = enviar(client, csrf, f'¿dónde está mi paquete {guia}?').get_json()
    assert guia in r['texto']

    segunda = falso.llamadas[1]['messages']
    resultado = segunda[-1]['content'][0]
    assert resultado['type'] == 'tool_result' and resultado['tool_use_id'] == 'tu_1'
    datos = json.loads(resultado['content'])
    assert datos['encontrado'] is True and datos['destino'] == 'Guadalajara, Jalisco'
    assert 'Calle 1' not in resultado['content']  # nunca se expone la dirección completa


def test_cotizar_por_chat_usa_las_tarifas_reales(client, csrf, claude, monkeypatch):
    monkeypatch.setattr(asistente.geo, 'distancia_km', lambda *a, **k: (540.0, 'osm'))
    falso = claude(
        respuesta(herramienta('cotizar_envio', {'origen': 'CDMX', 'destino': 'Guadalajara', 'peso_kg': 2,
                                                'largo_cm': 30, 'ancho_cm': 20, 'alto_cm': 15, 'tipo_envio': 'fast'})),
        respuesta(texto('Te sale en $280.')),
    )
    enviar(client, csrf, 'cotiza 2 kg de cdmx a gdl rápido')
    datos = json.loads(falso.llamadas[1]['messages'][-1]['content'][0]['content'])
    assert datos['precio_total_mxn'] == 250 + 10 + 20


def test_queja_registra_folio(client, csrf, claude, app):
    guia = crear_envio(client, csrf)
    falso = claude(
        respuesta(herramienta('registrar_queja', {'guia': guia, 'tipo': 'retraso', 'descripcion': 'No ha llegado',
                                                  'nombre_cliente': 'Ana', 'telefono_contacto': '5512345678'})),
        respuesta(texto('Lamento mucho el retraso. Tu folio es el que te comparto.')),
    )
    enviar(client, csrf, 'mi paquete no llega y estoy muy molesta')
    datos = json.loads(falso.llamadas[1]['messages'][-1]['content'][0]['content'])
    assert datos['registrada'] is True and datos['folio'].startswith('QJ-')
    with app.app_context():
        fila = db.sa.session.query(db.Queja).one()
        assert fila.guia == guia and fila.tipo == 'retraso'


def test_queja_con_guia_inexistente_no_se_registra(client, csrf, claude, app):
    claude(respuesta(herramienta('registrar_queja', {'guia': 'BLL-00000000', 'tipo': 'retraso', 'descripcion': 'x',
                                                     'nombre_cliente': '', 'telefono_contacto': ''})),
           respuesta(texto('No encuentro esa guía, ¿me la confirmas?')))
    enviar(client, csrf, 'queja')
    with app.app_context():
        assert db.sa.session.query(db.Queja).count() == 0


def test_transferir_a_humano_marca_escalado(client, csrf, claude, app):
    claude(respuesta(herramienta('transferir_a_humano', {'motivo': 'amenaza_legal',
                                                         'resumen': 'Cliente amenaza con Profeco por retraso.',
                                                         'telefono_contacto': '5512345678'})),
           respuesta(texto('Entiendo. Ya pasé tu caso a una persona del equipo.')))
    r = enviar(client, csrf, 'los voy a demandar en Profeco').get_json()
    assert r['escalado'] is True
    with app.app_context():
        esc = db.sa.session.query(db.Escalamiento).one()
        conv = db.sa.session.query(db.Conversacion).one()
        assert esc.motivo == 'amenaza_legal' and 'Profeco' in esc.resumen
        assert conv.estado == 'escalada'


def test_historial_acotado_y_conversacion_guardada(client, csrf, claude, monkeypatch):
    monkeypatch.setenv('ASISTENTE_MAX_TURNOS_HISTORIAL', '1')
    falso = claude(respuesta(texto('uno')), respuesta(texto('dos')), respuesta(texto('tres')))
    for t in ('a', 'b', 'c'):
        enviar(client, csrf, t)
    # Con 1 turno de historial, la 3a llamada solo lleva el turno anterior + el mensaje nuevo
    assert falso.llamadas[2]['messages'] == [{'role': 'user', 'content': 'b'},
                                             {'role': 'assistant', 'content': 'dos'},
                                             {'role': 'user', 'content': 'c'}]
    historial = client.get('/api/asistente/historial').get_json()['mensajes']
    assert [m['texto'] for m in historial] == ['a', 'uno', 'b', 'dos', 'c', 'tres']


def test_error_de_api_da_mensaje_amable(client, csrf, claude):
    peticion = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    claude(anthropic.APIConnectionError(request=peticion))
    r = enviar(client, csrf, 'hola').get_json()
    assert r['ok'] is True and 'problema técnico' in r['texto']


def test_rechazo_de_seguridad(client, csrf, claude):
    claude(respuesta(stop='refusal'))
    assert 'no te puedo ayudar' in enviar(client, csrf, '...').get_json()['texto']


def test_falla_de_herramienta_no_tumba_la_conversacion(client, csrf, claude, monkeypatch):
    def explota(*a, **k):
        raise RuntimeError('db caída')
    monkeypatch.setitem(asistente.EJECUTORES, 'rastrear_envio', explota)
    falso = claude(respuesta(herramienta('rastrear_envio', {'guia': 'BLL-12345678'})),
                   respuesta(texto('Ahorita no puedo consultar el rastreo.')))
    assert enviar(client, csrf, 'rastrea').get_json()['ok'] is True
    assert falso.llamadas[1]['messages'][-1]['content'][0]['is_error'] is True


def test_sin_api_key_responde_503(client, csrf, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', '')
    asistente.asignar_cliente(None)
    r = enviar(client, csrf, 'hola')
    assert r.status_code == 503 and 'no está disponible' in r.get_json()['error']


def test_validaciones_y_limite_de_mensajes(client, csrf, claude):
    assert enviar(client, csrf, '   ').status_code == 400
    assert enviar(client, csrf, 'x' * 1001).status_code == 400
    claude(*[respuesta(texto('ok')) for _ in range(25)])
    codigos = [enviar(client, csrf, f'm{i}').status_code for i in range(21)]
    assert codigos[:20] == [200] * 20 and codigos[20] == 429


def test_sin_csrf_rechazado(client, claude):
    r = client.post('/api/asistente/mensaje', json={'texto': 'hola'})
    assert r.status_code == 400


def test_widget_en_las_paginas(client):
    html = client.get('/').get_data(as_text=True)
    assert 'id="lula"' in html and 'asistente.js' in html
