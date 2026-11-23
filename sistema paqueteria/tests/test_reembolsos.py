"""Reembolsos y compensaciones: reglas de negocio, confirmación, límites, admin, cupones y asistente."""
import io
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

import asistente
import db
import politicas
import reembolsos as rb
from models import Caso, Conversacion, Cupon, Envio, Evidencia, Notificacion, PruebaEntrega, Queja, sa
from test_asistente import ClaudeFalso, herramienta, respuesta, texto
from test_flujo import hasta_pago

REM = {'nombre': 'Tienda Demo', 'telefono': '5500001111', 'ciudad': 'Azcapotzalco', 'estado': 'CDMX', 'cp': '02300'}
DEST = {'nombre': 'Luis Gómez', 'telefono': '5511223344', 'calle': 'Calle 1', 'colonia': 'Centro', 'ciudad': 'Cuauhtémoc',
        'estado': 'CDMX', 'cp': '06000'}


def cot(total=200.0, fecha_max=None, **extra):
    fecha_max = fecha_max or (date.today() + timedelta(days=3)).isoformat()
    conceptos = [{'concepto': 'Tarifa base', 'importe': total - 40, 'icono': 'fa-route'},
                 {'concepto': 'Empaque', 'importe': 25.0, 'icono': 'fa-box-open'},
                 {'concepto': 'Envío rápido', 'importe': 15.0, 'icono': 'fa-bolt'}]
    return {'total': total, 'fecha_min': fecha_max, 'fecha_max': fecha_max, 'tipo_envio': 'fast', 'caja': 'propia',
            'peso_facturable': 1, 'conceptos': conceptos, 'co2_kg': 0.1, 'compensar_co2': False} | extra


def envio(app, estado='creado', total=200.0, fecha_max=None, correo=None, repartidor=False, **extra):
    with app.app_context():
        guia = db.crear_envio(remitente=REM, destinatario=DEST, cotizacion=cot(total, fecha_max), tipo_entrega='recolector',
                              correo_destinatario=correo, destino={'lat': 19.43, 'lng': -99.13})
        e = db.obtener_envio_modelo(guia)
        e.estado = estado
        if repartidor:
            rep = db.crear_usuario(nombre='Pedro Salinas', correo=f'p{guia}@b.mx', password='Clave12345', rol='repartidor')
            e.repartidor_id = rep.id
        for k, v in extra.items():
            setattr(e, k, v)
        sa.session.commit()
        return guia


def conv(app, turnos=1):
    with app.app_context():
        cid = asistente.nueva_conversacion('web')
        sa.session.get(Conversacion, cid).turnos = turnos
        sa.session.commit()
        return cid


def evaluar(app, cid, guia, problema, ultimos4='3344', turno=1, **kw):
    with app.app_context():
        return rb.evaluar(conversacion_id=cid, turno=turno, guia=guia, ultimos4_telefono=ultimos4, problema=problema,
                          detalle=kw.get('detalle', 'detalle'), correo_contacto=kw.get('correo', ''))


def ejecutar(app, cid, folio, resolucion, turno=2):
    with app.app_context():
        return rb.ejecutar(conversacion_id=cid, turno_actual=turno, folio=folio, resolucion=resolucion)


# ---------------------------------------------------------------- verificación

def test_verificacion_por_telefono_y_sospecha_de_fraude(app):
    guia, cid = envio(app), conv(app)
    assert evaluar(app, cid, guia, 'retraso', '1111')['cliente_es'] == 'remitente'
    for i in range(1, 4):
        r = evaluar(app, cid, guia, 'retraso', '9999')
        assert r['verificado'] is False and r['intentos_fallidos'] == i
    assert r['sospecha_fraude'] is True
    r = evaluar(app, cid, guia, 'retraso', '3344')  # aun con los dígitos correctos
    assert r['verificado'] is False and r['sospecha_fraude'] is True


# ---------------------------------------------------------------- tabla de compensaciones

def test_retraso_dentro_de_plazo_no_aplica(app):
    r = evaluar(app, conv(app), envio(app), 'retraso')
    assert r['aplica_compensacion'] is False and 'dentro del plazo' in r['nota']


def test_retraso_menor_cupon_10(app):
    ayer = (date.today() - timedelta(days=1)).isoformat()  # venció anoche: < 24 h
    r = evaluar(app, conv(app), envio(app, fecha_max=ayer), 'retraso')
    assert [(o['resolucion'], o['cupon_pct']) for o in r['opciones']] == [('cupon', 10)]


def test_retraso_mayor_cupon_20_o_reembolso_costo_envio(app):
    hace3 = (date.today() - timedelta(days=3)).isoformat()
    r = evaluar(app, conv(app), envio(app, fecha_max=hace3), 'retraso')
    opciones = {o['resolucion']: o for o in r['opciones']}
    assert opciones['cupon']['cupon_pct'] == 20
    assert opciones['reembolso']['monto_mxn'] == 175.0  # 200 pagado - 25 de empaque


def test_danado_pide_foto_y_luego_ofrece(app, client):
    guia, cid = envio(app, 'entregado'), conv(app)
    r = evaluar(app, cid, guia, 'danado')
    assert r['falta_foto'] and r['enlace_para_subir_foto'].startswith('/evidencia/')
    url = r['enlace_para_subir_foto']
    html = client.get(url).get_data(as_text=True)
    tok = __import__('re').search(r'name="_csrf" value="([^"]+)"', html).group(1)
    client.post(url, data={'_csrf': tok, 'foto': (io.BytesIO(b'\x89PNG fake'), 'dano.png', 'image/png')}, content_type='multipart/form-data')
    r = evaluar(app, cid, guia, 'danado')
    assert {o['resolucion'] for o in r['opciones']} == {'reenvio', 'reembolso'}
    assert next(o for o in r['opciones'] if o['resolucion'] == 'reembolso')['monto_mxn'] == 200.0


def _prueba(app, guia, foto=None, lat=19.43, lng=-99.13):
    with app.app_context():
        rep = db.crear_usuario(nombre='Raúl', correo=f'r{guia}@b.mx', password='Clave12345', rol='repartidor')
        sa.session.add(PruebaEntrega(guia=guia, repartidor_id=rep.id, receptor_nombre='Luis Gómez', relacion='destinatario',
                                     firma_png='data:image/png;base64,AA==', foto_archivo=foto, lat=lat, lng=lng))
        sa.session.commit()


def test_entregado_no_recibido_con_evidencia_clara(app):
    guia = envio(app, 'entregado')
    _prueba(app, guia)  # firma + ubicación en el domicilio
    r = evaluar(app, conv(app), guia, 'entregado_no_recibido')
    assert r['evidencia_clara'] is True and [o['resolucion'] for o in r['opciones']] == ['revision']
    assert r['evidencia']['recibio'] == 'Luis'


def test_entregado_no_recibido_sin_evidencia_clara(app):
    guia = envio(app, 'entregado')
    _prueba(app, guia, lat=19.50, lng=-99.20)  # firma a ~10 km del domicilio, sin foto
    r = evaluar(app, conv(app), guia, 'entregado_no_recibido')
    assert r['evidencia_clara'] is False and {o['resolucion'] for o in r['opciones']} == {'reenvio', 'reembolso'}


def test_perdido_solo_si_sin_movimiento_y_plazo_vencido(app):
    guia = envio(app, 'en_transito')
    assert evaluar(app, conv(app), guia, 'perdido')['aplica_compensacion'] is False
    hace5 = (date.today() - timedelta(days=5)).isoformat()
    guia2 = envio(app, 'en_transito', fecha_max=hace5)
    with app.app_context():
        for ev in db.obtener_envio_modelo(guia2).eventos:
            ev.fecha = datetime.now() - timedelta(days=4)
        sa.session.commit()
    assert {o['resolucion'] for o in evaluar(app, conv(app), guia2, 'perdido')['opciones']} == {'reenvio', 'reembolso'}


def test_cobro_duplicado_devuelve_diferencia_exacta(app):
    guia = envio(app)
    duplicado = envio(app)  # mismo remitente, destino y monto en minutos
    r = evaluar(app, conv(app), guia, 'cobro_incorrecto')
    assert r['cobro_duplicado'] and r['opciones'][0]['monto_mxn'] == 200.0 and r['guia_duplicada'] == duplicado


def test_cobro_correcto_solo_revision(app):
    with app.app_context():
        guia = db.crear_envio(remitente=REM | {'telefono': '5599998888'}, destinatario=DEST, cotizacion=cot(), tipo_entrega='recolector',
                              correo_destinatario=None)
    r = evaluar(app, conv(app), guia, 'cobro_incorrecto', ultimos4='8888')
    assert [o['resolucion'] for o in r['opciones']] == ['revision']


# ---------------------------------------------------------------- ejecución y reglas de dinero

def test_no_ejecuta_sin_confirmacion_en_turno_posterior(app):
    hace3 = (date.today() - timedelta(days=3)).isoformat()
    cid = conv(app)
    r = evaluar(app, cid, envio(app, fecha_max=hace3), 'retraso')
    with pytest.raises(rb.ErrorCaso, match='confirmación'):
        ejecutar(app, cid, r['folio'], 'reembolso', turno=1)
    with pytest.raises(rb.ErrorCaso, match='no se ofreció'):
        ejecutar(app, cid, r['folio'], 'reenvio')
    with pytest.raises(rb.ErrorCaso, match='No existe'):
        ejecutar(app, conv(app), r['folio'], 'reembolso')  # otra conversación
    res = ejecutar(app, cid, r['folio'], 'reembolso')
    assert res['estado'] == 'aprobado' and res['monto_mxn'] == 175.0 and res['se_refleja_en'] == '5 a 10 días hábiles'
    with pytest.raises(rb.ErrorCaso, match='ya fue procesado'):
        ejecutar(app, cid, r['folio'], 'reembolso')


def test_cupon_aprobado_y_queja_del_repartidor(app):
    guia, cid = envio(app, 'entregado', repartidor=True), conv(app)
    r = evaluar(app, cid, guia, 'trato_repartidor', detalle='Fue grosero')
    res = ejecutar(app, cid, r['folio'], 'cupon')
    assert res['codigo_cupon'].startswith('BAL') and res['porcentaje'] == 10
    with app.app_context():
        q = sa.session.scalar(select(Queja).where(Queja.guia == guia))
        assert q.repartidor_id == db.obtener_envio_modelo(guia).repartidor_id and 'grosero' in q.descripcion


def test_tope_de_300_va_a_pendiente(app):
    guia, cid = envio(app, 'entregado', total=450.0), conv(app)
    _prueba(app, guia, lat=19.6, lng=-99.3)
    r = evaluar(app, cid, guia, 'entregado_no_recibido')
    res = ejecutar(app, cid, r['folio'], 'reembolso')
    assert res['estado'] == 'pendiente_de_aprobacion' and 'supera el tope' in res['motivo']
    assert res['tiempo_de_respuesta'] == '24 horas hábiles'


def test_nunca_mas_de_lo_pagado(app):
    with app.app_context():
        politicas.guardar({'reembolso_tope_mxn': '1000'}, 'admin')
    guia, cid = envio(app, 'entregado', total=120.0), conv(app)
    _prueba(app, guia, lat=19.6, lng=-99.3)
    r = evaluar(app, cid, guia, 'entregado_no_recibido')
    assert ejecutar(app, cid, r['folio'], 'reembolso')['estado'] == 'aprobado'  # 120 ≤ min(1000, 120)


def test_limite_2_por_cliente_cada_30_dias(app):
    hace3 = (date.today() - timedelta(days=3)).isoformat()
    estados = []
    for _ in range(3):
        cid = conv(app)
        r = evaluar(app, cid, envio(app, fecha_max=hace3), 'retraso')
        estados.append(ejecutar(app, cid, r['folio'], 'cupon')['estado'])
    assert estados == ['aprobado', 'aprobado', 'pendiente_de_aprobacion']


def test_reenvio_crea_guia_nueva_sin_costo(app):
    guia, cid = envio(app, 'entregado'), conv(app)
    _prueba(app, guia, lat=19.6, lng=-99.3)
    r = evaluar(app, cid, guia, 'entregado_no_recibido')
    res = ejecutar(app, cid, r['folio'], 'reenvio')
    with app.app_context():
        nuevo = db.obtener_envio_modelo(res['guia_reenvio'])
        assert nuevo.total == 0.0 and nuevo.destinatario == DEST and nuevo.estado == 'creado'


def test_politicas_editables_cambian_el_resultado(app):
    with app.app_context():
        politicas.guardar({'cupon_retraso_menor_pct': '15'}, 'admin')
        with pytest.raises(ValueError):
            politicas.guardar({'cupon_retraso_menor_pct': '150'}, 'admin')
    ayer = (date.today() - timedelta(days=1)).isoformat()
    assert evaluar(app, conv(app), envio(app, fecha_max=ayer), 'retraso')['opciones'][0]['cupon_pct'] == 15


# ---------------------------------------------------------------- admin

def _admin(client, csrf, app):
    with app.app_context():
        db.crear_usuario(nombre='Admin', correo='a@b.mx', password='Clave12345', rol='admin')
    client.post('/iniciar-sesion', data={'_csrf': csrf, 'correo': 'a@b.mx', 'password': 'Clave12345'})


def _pendiente(app, correo='luis@ejemplo.com'):
    guia, cid = envio(app, 'entregado', total=450.0), conv(app)
    _prueba(app, guia, lat=19.6, lng=-99.3)
    r = evaluar(app, cid, guia, 'entregado_no_recibido', correo=correo)
    ejecutar(app, cid, r['folio'], 'reembolso')
    return r['folio']


def test_admin_aprueba_pendiente_y_notifica(client, csrf, app):
    folio = _pendiente(app)
    _admin(client, csrf, app)
    assert folio in client.get('/admin/reembolsos').get_data(as_text=True)
    assert 'Evidencia de entrega' in client.get(f'/admin/reembolsos/{folio}').get_data(as_text=True)
    client.post(f'/admin/reembolsos/{folio}/resolver', data={'_csrf': csrf, 'decision': 'aprobar', 'nota': 'Procede'})
    with app.app_context():
        caso = sa.session.scalar(select(Caso).where(Caso.folio == folio))
        assert caso.estado == 'aprobado_admin' and caso.resuelto_por == 'a@b.mx'
        n = sa.session.scalar(select(Notificacion).where(Notificacion.para == 'luis@ejemplo.com'))
        assert 'aprobado' in n.asunto and '$450.00' in n.cuerpo and 'Procede' in n.cuerpo


def test_admin_rechaza_y_no_dos_veces(client, csrf, app):
    folio = _pendiente(app)
    _admin(client, csrf, app)
    client.post(f'/admin/reembolsos/{folio}/resolver', data={'_csrf': csrf, 'decision': 'rechazar'})
    client.post(f'/admin/reembolsos/{folio}/resolver', data={'_csrf': csrf, 'decision': 'aprobar'})
    with app.app_context():
        assert sa.session.scalar(select(Caso).where(Caso.folio == folio)).estado == 'rechazado'
        assert 'no procede' in sa.session.scalar(select(Notificacion).where(Notificacion.para == 'luis@ejemplo.com')).cuerpo


def test_admin_edita_politicas(client, csrf, app):
    _admin(client, csrf, app)
    assert 'Monto máximo de reembolso' in client.get('/admin/politicas').get_data(as_text=True)
    valores = {k: str(v) for k, v in politicas.POLITICAS.items() for v in [v[0]]}
    client.post('/admin/politicas', data={'_csrf': csrf} | valores | {'reembolso_tope_mxn': '500'})
    with app.app_context():
        assert politicas.obtener('reembolso_tope_mxn') == 500.0


# ---------------------------------------------------------------- cupones en el cotizador

def test_cupon_se_usa_al_pagar_una_sola_vez(client, csrf, app):
    with app.app_context():
        codigo = rb.crear_cupon(20).codigo
        sa.session.commit()
    hasta_pago(client, csrf)
    client.post('/app/cupon', data={'_csrf': csrf, 'codigo': codigo.lower()})
    assert 'Cupón ' + codigo in client.get('/app/pago').get_data(as_text=True)
    guia = client.post('/app/checkout', data={'_csrf': csrf, 'resultado': 'success'}).location.rsplit('/', 1)[1]
    with app.app_context():
        e = db.obtener_envio_modelo(guia)
        assert e.total == 248.0  # 310 - 20%
        assert sa.session.get(Cupon, codigo).usado_en_guia == guia
    hasta_pago(client, csrf)
    r = client.post('/app/cupon', data={'_csrf': csrf, 'codigo': codigo}, follow_redirects=True)
    assert 'ya se usó' in r.get_data(as_text=True)


# ---------------------------------------------------------------- de punta a punta con el asistente

def test_asistente_reembolso_de_punta_a_punta(client, csrf, app):
    hace3 = (date.today() - timedelta(days=3)).isoformat()
    guia = envio(app, fecha_max=hace3)

    def folio_de(falso):
        return json.loads(falso.llamadas[1]['messages'][-1]['content'][0]['content'])['folio']

    falso = ClaudeFalso([
        respuesta(herramienta('evaluar_compensacion', {'guia': guia, 'ultimos4_telefono': '3344', 'problema': 'retraso',
                                                       'detalle': 'Llegó 3 días tarde', 'correo_contacto': ''})),
        respuesta(texto('Lo siento mucho. Puedo darte un cupón de 20% o reembolsarte $175. ¿Cuál prefieres?')),
    ])
    asistente.asignar_cliente(falso)
    try:
        r = client.post('/api/asistente/mensaje', json={'texto': 'mi paquete llegó tarde, estoy furioso'}, headers={'X-CSRF-Token': csrf})
        assert '¿Cuál prefieres?' in r.get_json()['texto']
        folio = folio_de(falso)
        # Siguiente turno: el cliente confirma
        falso.guion = [respuesta(herramienta('ejecutar_compensacion', {'folio': folio, 'resolucion': 'reembolso'}, 'tu_2')),
                       respuesta(texto('Listo, aprobado. Se refleja en 5 a 10 días hábiles.'))]
        falso.llamadas = []
        r = client.post('/api/asistente/mensaje', json={'texto': 'el reembolso por favor'}, headers={'X-CSRF-Token': csrf})
        resultado = json.loads(falso.llamadas[1]['messages'][-1]['content'][0]['content'])
        assert resultado['estado'] == 'aprobado' and resultado['monto_mxn'] == 175.0
        with app.app_context():
            caso = sa.session.scalar(select(Caso).where(Caso.folio == folio))
            assert caso.estado == 'aprobado_auto' and 'furioso' in caso.transcripcion  # se guarda la conversación
    finally:
        asistente.asignar_cliente(None)


def test_asistente_no_puede_ejecutar_en_el_mismo_turno(client, csrf, app):
    hace3 = (date.today() - timedelta(days=3)).isoformat()
    guia = envio(app, fecha_max=hace3)
    falso = ClaudeFalso([
        respuesta(herramienta('evaluar_compensacion', {'guia': guia, 'ultimos4_telefono': '3344', 'problema': 'retraso',
                                                       'detalle': 'tarde', 'correo_contacto': ''})),
        respuesta(herramienta('ejecutar_compensacion', {'folio': 'X', 'resolucion': 'reembolso'}, 'tu_2')),
        respuesta(texto('...')),
    ])
    asistente.asignar_cliente(falso)
    try:
        client.post('/api/asistente/mensaje', json={'texto': 'reembolsame ya'}, headers={'X-CSRF-Token': csrf})
        folio = json.loads(falso.llamadas[1]['messages'][-1]['content'][0]['content'])['folio']
        # El modelo intenta ejecutar con el folio real en el mismo turno: el código lo impide
        falso.guion = [respuesta(herramienta('evaluar_compensacion', {'guia': guia, 'ultimos4_telefono': '3344', 'problema': 'retraso',
                                                                     'detalle': 'tarde', 'correo_contacto': ''})),
                       respuesta(herramienta('ejecutar_compensacion', {'folio': 'PLACEHOLDER', 'resolucion': 'reembolso'}, 'tu_9')),
                       respuesta(texto('...'))]
        falso.llamadas = []
        orig = falso.create

        def create(**kw):  # sustituye el folio del caso recién evaluado
            r = orig(**kw)
            for b in r.content:
                if b.type == 'tool_use' and b.input.get('folio') == 'PLACEHOLDER':
                    b.input['folio'] = json.loads(kw['messages'][-1]['content'][0]['content'])['folio']
            return r
        falso.create = create
        client.post('/api/asistente/mensaje', json={'texto': 'hazlo ya'}, headers={'X-CSRF-Token': csrf})
        res = json.loads(falso.llamadas[2]['messages'][-1]['content'][0]['content'])
        assert 'confirmación' in res['error']
    finally:
        asistente.asignar_cliente(None)
