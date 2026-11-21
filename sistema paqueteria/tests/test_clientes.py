"""Rastreo en vivo, reprogramar entrega, certificado verde y envíos masivos de empresas."""
import io
from datetime import datetime, timedelta

import pytest
from openpyxl import Workbook

import db
import empresas
import geo
from models import Envio, Lote, UbicacionRepartidor, sa

COT = {'total': 150.0, 'fecha_min': '2030-01-07', 'fecha_max': '2030-01-09', 'tipo_envio': 'eco', 'caja': 'propia',
       'peso_facturable': 1, 'co2_kg': 0.8, 'compensar_co2': True, 'conceptos': []}
DEST = {'nombre': 'Luis Gómez', 'telefono': '5511223344', 'calle': 'Calle 1', 'colonia': 'Centro', 'ciudad': 'Cuauhtémoc',
        'estado': 'Ciudad de México', 'cp': '06000'}


def nuevo_envio(app, estado='creado', **extra):
    with app.app_context():
        guia = db.crear_envio(remitente={'nombre': 'Tienda', 'ciudad': 'Azcapotzalco', 'estado': 'CDMX'}, destinatario=DEST,
                              cotizacion=dict(COT, **extra.pop('cot', {})), tipo_entrega='recolector', correo_destinatario=None,
                              destino={'lat': 19.43, 'lng': -99.13})
        e = db.obtener_envio_modelo(guia)
        e.estado = estado
        for k, v in extra.items():
            setattr(e, k, v)
        sa.session.commit()
        return guia


# ---------------------------------------------------------------- Rastreo en vivo

def test_rastreo_en_vivo(client, app):
    with app.app_context():
        rep = db.crear_usuario(nombre='Pedro Salinas', correo='p@b.mx', password='Clave12345', rol='repartidor')
        sa.session.add(UbicacionRepartidor(repartidor_id=rep.id, lat=19.45, lng=-99.14, fecha=datetime.now()))
        sa.session.commit()
        rid = rep.id
    guia = nuevo_envio(app, 'en_reparto', repartidor_id=rid, orden_ruta=1)
    html = client.get(f'/rastreo?guia={guia}').get_data(as_text=True)
    assert 'Pedro va en camino' in html and 'min' in html
    vivo = client.get(f'/api/rastreo/{guia}/vivo').get_json()['vivo']
    assert vivo['lat'] == 19.45 and vivo['minutos'] > 0
    assert 'Calle 1' not in str(vivo) and '19.43' not in str(vivo)  # nunca la dirección del cliente

    with app.app_context():  # ubicación vieja: no se muestra
        sa.session.get(UbicacionRepartidor, rid).fecha = datetime.now() - timedelta(hours=1)
        sa.session.commit()
    assert client.get(f'/api/rastreo/{guia}/vivo').get_json()['vivo'] is None


# ---------------------------------------------------------------- Reprogramar

def test_reprogramar_entrega(client, csrf, app):
    guia = nuevo_envio(app)
    html = client.get(f'/rastreo?guia={guia}').get_data(as_text=True)
    assert 'Elige cuándo y dónde lo dejamos' in html
    base = {'_csrf': csrf, 'fecha': '2030-01-08', 'instruccion': 'sucursal', 'nota': ''}
    client.post(f'/rastreo/{guia}/preferencias', data=base | {'verificacion': '0000'})
    with app.app_context():
        assert db.obtener_envio_modelo(guia).fecha_preferida is None
    client.post(f'/rastreo/{guia}/preferencias', data=base | {'verificacion': '3344', 'instruccion': 'vecino'})  # vecino sin nota
    with app.app_context():
        assert db.obtener_envio_modelo(guia).fecha_preferida is None
    client.post(f'/rastreo/{guia}/preferencias', data=base | {'verificacion': '3344'})
    with app.app_context():
        e = db.obtener_envio_modelo(guia)
        assert e.fecha_preferida == '2030-01-08' and e.instruccion_entrega == 'sucursal'
        assert 'pidió entregar el martes 8' in db.obtener_envio(guia)['eventos'][0]['descripcion']


def test_reprogramar_bloqueos(client, csrf, app):
    guia = nuevo_envio(app)
    for _ in range(5):
        client.post(f'/rastreo/{guia}/preferencias', data={'_csrf': csrf, 'verificacion': '9999', 'fecha': '2030-01-08', 'instruccion': 'domicilio'})
    client.post(f'/rastreo/{guia}/preferencias', data={'_csrf': csrf, 'verificacion': '3344', 'fecha': '2030-01-08', 'instruccion': 'domicilio'})
    with app.app_context():
        assert db.obtener_envio_modelo(guia).fecha_preferida is None
    en_reparto = nuevo_envio(app, 'en_reparto')
    assert 'Elige cuándo' not in client.get(f'/rastreo?guia={en_reparto}').get_data(as_text=True)


# ---------------------------------------------------------------- Certificado verde

def test_certificado_verde(client, app):
    si = nuevo_envio(app)
    no = nuevo_envio(app, cot={'compensar_co2': False})
    r = client.get(f'/certificado-verde/{si}')
    assert r.status_code == 200 and '0.80 kg' in r.get_data(as_text=True)
    assert client.get(f'/certificado-verde/{no}').status_code == 404


# ---------------------------------------------------------------- Empresas

@pytest.fixture
def geo_falso(monkeypatch):
    monkeypatch.setattr(geo, 'geocodificar', lambda d, k='': {'lat': 19.4, 'lng': -99.1, 'partes': {}})
    monkeypatch.setattr(geo, 'distancia_km', lambda *a, **k: (12.0, 'osm'))


EMPRESA = {'empresa': 'Tienda Demo', 'correo': 'ops@tienda.mx', 'rem_nombre': 'Almacén', 'rem_telefono': '5500000000',
           'rem_calle': 'Av. Industria 742', 'rem_colonia': 'Industrial Vallejo', 'rem_ciudad': 'Azcapotzalco',
           'rem_estado': 'Ciudad de México', 'rem_cp': '02300', 'compensar': 'si'}


def csv_archivo(n_validos, n_malos=0):
    filas = [','.join(empresas.COLUMNAS)]
    for i in range(n_validos):
        filas.append(f'Cliente {i},5512345678,Calle {i},Centro,Cuauhtémoc,Ciudad de México,06000,,,1,10,10,10,ecologico')
    for i in range(n_malos):
        filas.append(f'Malo {i},123,Calle,Centro,Cuauhtémoc,CDMX,06000,,,1,10,10,10,volando')
    return (io.BytesIO('\n'.join(filas).encode('utf-8')), 'envios.csv')


def test_plantilla(client):
    r = client.get('/empresas/plantilla.csv')
    assert r.mimetype == 'text/csv' and 'peso_kg' in r.get_data(as_text=True)


def test_carga_masiva_con_descuento_y_pago(client, csrf, app, geo_falso):
    r = client.post('/empresas/cargar', data=EMPRESA | {'_csrf': csrf, 'archivo': csv_archivo(10, 2)},
                    content_type='multipart/form-data')
    folio = r.location.rsplit('/', 1)[1]
    html = client.get(f'/empresas/lote/{folio}').get_data(as_text=True)
    assert 'Fila 12' in html and 'teléfono debe tener 10 dígitos' in html and 'servicio debe ser' in html
    with app.app_context():
        lote = sa.session.get(Lote, folio)
        assert lote.descuento_pct == 5  # 10 válidos
        # 12 km → $60 base + $5 CO2 = $65, menos 5% = $61.75 por envío
        assert lote.total == pytest.approx(61.75 * 10)

    assert app.test_client().get(f'/empresas/lote/{folio}').status_code == 404  # otro navegador no lo ve

    client.post(f'/empresas/lote/{folio}/pagar', data={'_csrf': csrf})
    client.post(f'/empresas/lote/{folio}/pagar', data={'_csrf': csrf})  # doble clic: no duplica
    with app.app_context():
        envios = sa.session.query(Envio).filter_by(lote_folio=folio).all()
        assert len(envios) == 10 and all(e.total == 61.75 for e in envios)
        assert any('Descuento por volumen' in c['concepto'] for c in envios[0].cotizacion['conceptos'])
        guia = envios[0].guia
    assert client.get(f'/app/guia/{guia}.pdf').mimetype == 'application/pdf'
    assert guia in client.get(f'/empresas/lote/{folio}/guias.csv').get_data(as_text=True)
    assert '10 envíos' in client.get(f'/empresas/lote/{folio}/certificado-verde').get_data(as_text=True)


def test_carga_excel(client, csrf, app, geo_falso):
    wb = Workbook()
    ws = wb.active
    ws.append(empresas.COLUMNAS)
    ws.append(['Ana', 5512345678, 'Calle 1', 'Centro', 'Cuauhtémoc', 'Ciudad de México', 6000, '', '', 2.5, 30, 20, 10, 'rapido'])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = client.post('/empresas/cargar', data=EMPRESA | {'_csrf': csrf, 'archivo': (buf, 'envios.xlsx')}, content_type='multipart/form-data')
    with app.app_context():
        lote = sa.session.get(Lote, r.location.rsplit('/', 1)[1])
        cot = lote.renglones[0]['cotizacion']
        assert cot and cot['tipo_envio'] == 'fast' and lote.renglones[0]['destinatario']['cp'] == '06000'
        assert lote.descuento_pct == 0


@pytest.mark.parametrize('archivo, mensaje', [
    ((io.BytesIO(b'nombre,telefono\nA,1'), 'x.csv'), 'Faltan columnas'),
    ((io.BytesIO(b'hola'), 'x.txt'), '.csv o .xlsx'),
])
def test_archivos_invalidos(client, csrf, archivo, mensaje):
    r = client.post('/empresas/cargar', data=EMPRESA | {'_csrf': csrf, 'archivo': archivo}, content_type='multipart/form-data', follow_redirects=True)
    assert mensaje in r.get_data(as_text=True)


def test_limite_de_renglones(client, csrf, geo_falso):
    r = client.post('/empresas/cargar', data=EMPRESA | {'_csrf': csrf, 'archivo': csv_archivo(51)}, content_type='multipart/form-data', follow_redirects=True)
    assert 'Máximo 50 envíos' in r.get_data(as_text=True)
