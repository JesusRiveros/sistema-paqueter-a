"""Inicio de sesión, portal del repartidor, panel de admin, rutas y modelo de retraso."""
import re

import pytest
from sqlalchemy import select

import db
import demo
import modelo_retraso
import rutas
from models import Envio, Incidencia, Notificacion, PruebaEntrega, UbicacionRepartidor, Usuario, sa
from test_flujo import hasta_pago

PW = 'Clave12345segura'
FIRMA = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='


@pytest.fixture
def datos(app):
    with app.app_context():
        demo.sembrar(password=PW)


def entrar(client, csrf, correo, password=PW, siguiente=None):
    url = '/iniciar-sesion' + (f'?siguiente={siguiente}' if siguiente else '')
    return client.post(url, data={'_csrf': csrf, 'correo': correo, 'password': password})


def envio_de(app, correo, estado=None):
    with app.app_context():
        u = db.usuario_por_correo(correo)
        q = select(Envio).where(Envio.repartidor_id == u.id)
        if estado:
            q = q.where(Envio.estado == estado)
        return sa.session.scalars(q).first().guia


# ---------------------------------------------------------------- Inicio de sesión

def test_login_correcto_redirige_por_rol(client, csrf, datos):
    assert entrar(client, csrf, 'admin@balula.mx').location.endswith('/admin/')
    client.post('/cerrar-sesion', data={'_csrf': csrf})
    assert entrar(client, csrf, 'PEDRO@balula.mx').location.endswith('/repartidor/')


def test_login_incorrecto_mensaje_generico(client, csrf, datos):
    r = entrar(client, csrf, 'admin@balula.mx', 'mala')
    assert r.status_code == 401 and 'Correo o contraseña incorrectos' in r.get_data(as_text=True)
    r = entrar(client, csrf, 'noexiste@balula.mx', 'mala')
    assert r.status_code == 401 and 'Correo o contraseña incorrectos' in r.get_data(as_text=True)


def test_bloqueo_tras_5_intentos(client, csrf, datos):
    for _ in range(5):
        entrar(client, csrf, 'pedro@balula.mx', 'mala')
    r = entrar(client, csrf, 'pedro@balula.mx')  # aunque ahora sea la correcta
    assert r.status_code == 401 and 'bloqueada' in r.get_data(as_text=True)


def test_redireccion_abierta_bloqueada(client, csrf, datos):
    assert entrar(client, csrf, 'admin@balula.mx', siguiente='//evil.com').location.endswith('/admin/')
    client.post('/cerrar-sesion', data={'_csrf': csrf})
    assert entrar(client, csrf, 'admin@balula.mx', siguiente='/admin/envios').location.endswith('/admin/envios')


def test_protecciones_por_rol(client, csrf, datos):
    assert '/iniciar-sesion' in client.get('/repartidor/').location
    assert '/iniciar-sesion' in client.get('/admin/').location
    entrar(client, csrf, 'pedro@balula.mx')
    assert client.get('/admin/').status_code == 403
    assert client.get('/repartidor/').status_code == 200


def test_password_temporal_obliga_a_cambiarla(client, csrf, app):
    with app.app_context():
        db.crear_usuario(nombre='Nuevo', correo='nuevo@balula.mx', password='Temporal123', rol='repartidor', debe_cambiar_password=True)
    assert entrar(client, csrf, 'nuevo@balula.mx', 'Temporal123').location.endswith('/cambiar-contrasena')
    assert client.get('/repartidor/').location.endswith('/cambiar-contrasena')
    r = client.post('/cambiar-contrasena', data={'_csrf': csrf, 'actual': 'Temporal123', 'nueva': 'corta', 'confirmar': 'corta'})
    assert 'al menos 10 caracteres' in r.get_data(as_text=True)
    r = client.post('/cambiar-contrasena', data={'_csrf': csrf, 'actual': 'Temporal123', 'nueva': 'NuevaClave2026', 'confirmar': 'NuevaClave2026'})
    assert r.location.endswith('/repartidor/')
    assert client.get('/repartidor/').status_code == 200


# ---------------------------------------------------------------- Repartidor

def test_portal_repartidor_muestra_solo_sus_envios(client, csrf, app, datos):
    entrar(client, csrf, 'pedro@balula.mx')
    html = client.get('/repartidor/').get_data(as_text=True)
    with app.app_context():
        pedro = db.usuario_por_correo('pedro@balula.mx')
        propias = sa.session.scalars(select(Envio).where(Envio.repartidor_id == pedro.id, Envio.estado != 'entregado')).all()
        ajena = sa.session.scalars(select(Envio).where(Envio.repartidor_id != pedro.id)).first()
    assert propias and all(e.guia in html for e in propias)
    assert client.get(f'/repartidor/envio/{ajena.guia}').status_code == 404
    assert 'Riesgo' in html


def test_avanzar_estado_y_transicion_invalida(client, csrf, app, datos):
    guia = envio_de(app, 'pedro@balula.mx', 'recolectado')
    entrar(client, csrf, 'pedro@balula.mx')
    client.post(f'/repartidor/envio/{guia}/avanzar', data={'_csrf': csrf, 'estado': 'entregado'})  # salto inválido
    with app.app_context():
        assert db.obtener_envio(guia)['estado'] == 'recolectado'
    client.post(f'/repartidor/envio/{guia}/avanzar', data={'_csrf': csrf, 'estado': 'en_transito', 'lat': '19.4', 'lng': '-99.1'})
    with app.app_context():
        e = db.obtener_envio(guia)
        assert e['estado'] == 'en_transito' and e['eventos'][0]['lat'] == 19.4


def test_entrega_requiere_codigo_firma_y_bloquea_intentos(client, csrf, app, datos):
    guia = envio_de(app, 'pedro@balula.mx', 'en_reparto')
    with app.app_context():
        codigo = db.obtener_envio_modelo(guia).codigo_entrega
    entrar(client, csrf, 'pedro@balula.mx')
    base = {'_csrf': csrf, 'receptor': 'Ana López', 'relacion': 'destinatario', 'firma': FIRMA}
    incorrecto = '0000' if codigo != '0000' else '1111'

    client.post(f'/repartidor/envio/{guia}/entregar', data=base | {'codigo': incorrecto})
    client.post(f'/repartidor/envio/{guia}/entregar', data=base | {'codigo': codigo, 'firma': ''})  # sin firma
    with app.app_context():
        assert db.obtener_envio(guia)['estado'] == 'en_reparto'

    r = client.post(f'/repartidor/envio/{guia}/entregar', data=base | {'codigo': codigo, 'lat': '19.42', 'lng': '-99.16'})
    assert r.location.endswith('/repartidor/')
    with app.app_context():
        assert db.obtener_envio(guia)['estado'] == 'entregado'
        prueba = sa.session.scalar(select(PruebaEntrega).where(PruebaEntrega.guia == guia))
        assert prueba.receptor_nombre == 'Ana López' and prueba.firma_png == FIRMA and prueba.lat == 19.42


def test_cinco_codigos_incorrectos_bloquean_la_entrega(client, csrf, app, datos):
    guia = envio_de(app, 'pedro@balula.mx', 'en_reparto')
    with app.app_context():
        codigo = db.obtener_envio_modelo(guia).codigo_entrega
    entrar(client, csrf, 'pedro@balula.mx')
    base = {'_csrf': csrf, 'receptor': 'Ana López', 'relacion': 'destinatario', 'firma': FIRMA}
    malo = '0000' if codigo != '0000' else '1111'
    for _ in range(5):
        client.post(f'/repartidor/envio/{guia}/entregar', data=base | {'codigo': malo})
    client.post(f'/repartidor/envio/{guia}/entregar', data=base | {'codigo': codigo})
    with app.app_context():
        assert db.obtener_envio(guia)['estado'] == 'en_reparto'


def test_incidencia(client, csrf, app, datos):
    guia = envio_de(app, 'lucia@balula.mx')
    entrar(client, csrf, 'lucia@balula.mx')
    client.post(f'/repartidor/envio/{guia}/incidencia', data={'_csrf': csrf, 'tipo': 'ausente', 'nota': 'Nadie abrió'})
    with app.app_context():
        assert sa.session.scalar(select(Incidencia).where(Incidencia.guia == guia)).tipo == 'ausente'
        assert 'Incidencia' in db.obtener_envio(guia)['eventos'][0]['descripcion']


def test_optimizar_ruta_y_ubicacion(client, csrf, app, datos):
    entrar(client, csrf, 'raul@balula.mx')
    r = client.post('/repartidor/ubicacion', json={'lat': 19.36, 'lng': -99.17, 'precision': 10}, headers={'X-CSRF-Token': csrf})
    assert r.get_json() == {'ok': True}
    client.post('/repartidor/optimizar', data={'_csrf': csrf})
    with app.app_context():
        raul = db.usuario_por_correo('raul@balula.mx')
        assert sa.session.get(UbicacionRepartidor, raul.id).lat == 19.36
        ordenes = [e.orden_ruta for e in raul.envios if e.estado != 'entregado']
        assert sorted(ordenes) == list(range(1, len(ordenes) + 1))
    with app.app_context():
        guia = next(e.guia for e in db.usuario_por_correo('raul@balula.mx').envios if e.estado != 'entregado')
    alt = client.get(f'/repartidor/envio/{guia}/rutas').get_json()
    assert alt['ok'] and alt['fuente'] == 'estimada' and alt['rutas'][0]['recomendada']  # sin red: estimación


# ---------------------------------------------------------------- Admin

def test_paginas_admin(client, csrf, app, datos):
    entrar(client, csrf, 'admin@balula.mx')
    for url in ['/admin/', '/admin/envios', '/admin/envios?filtro=sin_asignar', '/admin/envios?filtro=entregado',
                '/admin/mapa', '/admin/repartidores', '/admin/atencion']:
        assert client.get(url).status_code == 200, url
    with app.app_context():
        guia = sa.session.scalars(select(Envio)).first().guia
    assert client.get(f'/admin/envios/{guia}').status_code == 200
    assert 'Entregas a tiempo' in client.get('/admin/').get_data(as_text=True)


def test_auto_asignar_reparte_todo(client, csrf, app, datos):
    entrar(client, csrf, 'admin@balula.mx')
    with app.app_context():
        assert sa.session.query(Envio).filter(Envio.repartidor_id.is_(None)).count() > 0
    client.post('/admin/envios/auto-asignar', data={'_csrf': csrf})
    with app.app_context():
        assert sa.session.query(Envio).filter(Envio.repartidor_id.is_(None), Envio.estado != 'entregado').count() == 0


def test_asignar_manual_y_estado(client, csrf, app, datos):
    entrar(client, csrf, 'admin@balula.mx')
    with app.app_context():
        e = sa.session.scalars(select(Envio).where(Envio.repartidor_id.is_(None))).first()
        guia, lucia = e.guia, db.usuario_por_correo('lucia@balula.mx').id
    client.post('/admin/envios/asignar', data={'_csrf': csrf, 'guia': guia, 'repartidor_id': lucia})
    client.post(f'/admin/envios/{guia}/estado', data={'_csrf': csrf, 'estado': 'recolectado', 'ubicacion': 'CEDIS'})
    with app.app_context():
        e = db.obtener_envio_modelo(guia)
        assert e.repartidor_id == lucia and e.estado == 'recolectado'


def test_alta_y_baja_de_repartidor(client, csrf, app, datos):
    entrar(client, csrf, 'admin@balula.mx')
    r = client.post('/admin/repartidores/nuevo', data={'_csrf': csrf, 'nombre': 'Nora Paz', 'correo': 'nora@balula.mx', 'rol': 'repartidor'}, follow_redirects=True)
    pw = re.search(r'nora@balula.mx: (\w{12})', r.get_data(as_text=True)).group(1)
    with app.app_context():
        nora = db.usuario_por_correo('nora@balula.mx')
        assert nora.debe_cambiar_password and nora.verificar_password(pw)
        pedro = db.usuario_por_correo('pedro@balula.mx')
        pendientes = [e.guia for e in pedro.envios if e.estado != 'entregado']
    client.post(f'/admin/repartidores/{pedro.id}/activo', data={'_csrf': csrf})
    with app.app_context():
        assert not db.usuario_por_correo('pedro@balula.mx').activo
        assert all(db.obtener_envio_modelo(g).repartidor_id is None for g in pendientes)


def test_fotos_solo_para_admin(client, csrf, datos):
    assert '/iniciar-sesion' in client.get('/admin/fotos/x.jpg').location
    entrar(client, csrf, 'pedro@balula.mx')
    assert client.get('/admin/fotos/x.jpg').status_code == 403


# ---------------------------------------------------------------- Rutas, modelo y avisos

def test_optimizador_mejora_un_orden_malo():
    inicio = (19.43, -99.13)
    # Ida y vuelta entre dos extremos: el orden original es pésimo
    paradas = [{'id': i, 'lat': lat, 'lng': -99.13} for i, lat in enumerate([19.50, 19.30, 19.49, 19.31, 19.48, 19.32])]
    r = rutas.optimizar(inicio, paradas)
    assert r['km'] < r['km_original'] and r['km_ahorrados'] > 10
    assert sorted(p['id'] for p in r['paradas']) == list(range(6))


def test_modelo_de_retraso():
    m = modelo_retraso.entrenar(modelo_retraso.datos_simulados(800), epocas=200)
    assert m['exactitud'] > 0.6
    base = {'hora_pico': 0.0, 'km_parada': 2, 'num_parada': 3, 'es_rapido': 0.0, 'incidencias_zona': 0, 'fin_de_semana': 0.0}
    assert modelo_retraso.predecir(base | {'hora_pico': 1.0, 'incidencias_zona': 3}) > modelo_retraso.predecir(base)


def test_aviso_al_destinatario_con_codigo(client, csrf, app):
    hasta_pago(client, csrf)  # el destinatario dejó correo: recibe aviso con su código de entrega
    client.post('/app/checkout', data={'_csrf': csrf, 'resultado': 'success'})
    with app.app_context():
        aviso = sa.session.query(Notificacion).one()
        assert aviso.para == 'destinatario@ejemplo.com' and 'código de entrega' in aviso.cuerpo
        guia = db.crear_envio(remitente={'nombre': 'A', 'ciudad': 'X', 'estado': 'Y'}, destinatario={'nombre': 'Luis Gómez'},
                              cotizacion={'total': 100, 'fecha_min': '2026-11-23', 'fecha_max': '2026-11-24', 'tipo_envio': 'eco'},
                              tipo_entrega='recolector', correo_destinatario='luis@ejemplo.com')
        import notificaciones
        with app.test_request_context():
            assert notificaciones.notificar(guia, 'creado') == 'registrado'  # sin SMTP: se registra
        n = sa.session.query(Notificacion).filter_by(para='luis@ejemplo.com').one()
        assert db.obtener_envio_modelo(guia).codigo_entrega in n.cuerpo


def test_comando_sembrar_demo(app):
    r = app.test_cli_runner().invoke(args=['sembrar-demo', '--password', PW])
    assert 'Envíos creados: 22' in r.output and 'admin@balula.mx' in r.output
    r = app.test_cli_runner().invoke(args=['sembrar-demo'])
    assert 'Envíos creados: 0' in r.output and 'ya existían' in r.output
