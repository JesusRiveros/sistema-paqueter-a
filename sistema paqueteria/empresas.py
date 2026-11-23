"""Envíos masivos para empresas: carga CSV/Excel → vista previa con precios → pago → guías."""
import csv
import io
import re
import secrets

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session,
                   url_for)

import db
import geo
import tarifas
from models import Lote, sa

bp = Blueprint('empresas', __name__, url_prefix='/empresas')

MAX_RENGLONES = 50  # la geocodificación gratuita permite ~1 dirección por segundo
COLUMNAS = ['nombre', 'telefono', 'calle', 'colonia', 'ciudad', 'estado', 'cp', 'correo', 'referencias',
            'peso_kg', 'largo_cm', 'ancho_cm', 'alto_cm', 'servicio']
OBLIGATORIAS = ['nombre', 'telefono', 'calle', 'colonia', 'ciudad', 'estado', 'cp', 'peso_kg', 'largo_cm', 'ancho_cm', 'alto_cm']
SERVICIOS = {'rapido': 'fast', 'rápido': 'fast', 'fast': 'fast', 'ecologico': 'eco', 'ecológico': 'eco', 'eco': 'eco', '': 'eco'}
EJEMPLO = ['Ana López', '5512345678', 'Av. Paseo de la Reforma 222', 'Juárez', 'Cuauhtémoc', 'Ciudad de México', '06600',
           'ana@ejemplo.com', 'Portón negro', '1.5', '30', '20', '10', 'ecologico']


def _mis_lotes():
    return session.get('mis_lotes', [])


def _lote_propio(folio):
    lote = sa.session.get(Lote, folio.upper())
    if not lote or lote.folio not in _mis_lotes():
        abort(404)
    return lote


def leer_archivo(archivo):
    """Devuelve lista de dicts con las columnas de COLUMNAS. Acepta .csv (UTF-8) y .xlsx."""
    nombre = (archivo.filename or '').lower()
    if nombre.endswith('.xlsx'):
        from openpyxl import load_workbook
        hoja = load_workbook(io.BytesIO(archivo.read()), read_only=True, data_only=True).active
        filas = [[('' if c is None else str(c)).strip() for c in fila] for fila in hoja.iter_rows(values_only=True)]
    elif nombre.endswith('.csv'):
        texto = archivo.read().decode('utf-8-sig', errors='replace')
        filas = [[c.strip() for c in fila] for fila in csv.reader(io.StringIO(texto))]
    else:
        raise ValueError('Sube un archivo .csv o .xlsx (usa la plantilla).')
    filas = [f for f in filas if any(f)]
    if not filas:
        raise ValueError('El archivo está vacío.')
    encabezado = [re.sub(r'\s+', '_', h.lower()) for h in filas[0]]
    faltan = [c for c in OBLIGATORIAS if c not in encabezado]
    if faltan:
        raise ValueError(f'Faltan columnas en el archivo: {", ".join(faltan)}. Usa la plantilla.')
    datos = [dict(zip(encabezado, f)) for f in filas[1:]]
    if len(datos) > MAX_RENGLONES:
        raise ValueError(f'Máximo {MAX_RENGLONES} envíos por archivo (tiene {len(datos)}). Divídelo en varios.')
    return datos


def _validar_renglon(r):
    errores = []
    for c in OBLIGATORIAS:
        if not str(r.get(c, '')).strip():
            errores.append(f'falta {c}')
    tel = re.sub(r'\D', '', str(r.get('telefono', '')))
    if tel and len(tel) != 10:
        errores.append('teléfono debe tener 10 dígitos')
    if r.get('cp') and not re.fullmatch(r'\d{5}', str(r['cp']).split('.')[0].zfill(5)):
        errores.append('C.P. inválido')
    if str(r.get('servicio', '')).strip().lower() not in SERVICIOS:
        errores.append('servicio debe ser "rapido" o "ecologico"')
    return errores, tel


def cotizar_renglones(remitente, datos, compensar, api_key):
    origen_txt = f"{remitente['calle']}, {remitente['colonia']}, {remitente['ciudad']}, {remitente['estado']}, {remitente['cp']}"
    origen = geo.geocodificar(origen_txt, api_key)
    renglones = []
    for i, r in enumerate(datos, start=2):  # número de fila como en Excel
        errores, tel = _validar_renglon(r)
        cp = str(r.get('cp', '')).split('.')[0].zfill(5)
        dest = {'nombre': r.get('nombre', '').strip()[:120], 'telefono': tel, 'calle': r.get('calle', '').strip()[:200],
                'colonia': r.get('colonia', '').strip()[:120], 'ciudad': r.get('ciudad', '').strip()[:120],
                'estado': r.get('estado', '').strip()[:80], 'cp': cp, 'correo': r.get('correo', '').strip()[:160],
                'referencias': r.get('referencias', '').strip()[:300]}
        renglon = {'fila': i, 'destinatario': dest, 'error': None, 'cotizacion': None, 'destino_geo': None}
        if not errores:
            destino_txt = f"{dest['calle']}, {dest['colonia']}, {dest['ciudad']}, {dest['estado']}, {cp}"
            ubic = geo.geocodificar(destino_txt, api_key) or geo.geocodificar(f"{cp}, {dest['ciudad']}, {dest['estado']}", api_key)
            if not origen or not ubic:
                errores.append('no pudimos ubicar la dirección')
            else:
                try:
                    km, _ = geo.distancia_km(origen_txt, destino_txt, api_key,
                                             coords=[origen['lat'], origen['lng'], ubic['lat'], ubic['lng']])
                    cot = tarifas.cotizar(distancia_km=km, peso=str(r['peso_kg']).replace(',', '.'),
                                          largo=str(r['largo_cm']).replace(',', '.'), ancho=str(r['ancho_cm']).replace(',', '.'),
                                          alto=str(r['alto_cm']).replace(',', '.'),
                                          tipo_envio=SERVICIOS[str(r.get('servicio', '')).strip().lower()], compensar_co2=compensar)
                    renglon['cotizacion'] = cot.to_dict()
                    renglon['destino_geo'] = {'lat': ubic['lat'], 'lng': ubic['lng']}
                except (tarifas.ErrorCotizacion, geo.SinProveedores) as e:
                    errores.append(str(e).rstrip('.').lower())
        renglon['error'] = '; '.join(errores) or None
        renglones.append(renglon)
    return renglones, ({'lat': origen['lat'], 'lng': origen['lng']} if origen else None)


def aplicar_descuento(renglones):
    validos = [r for r in renglones if r['cotizacion']]
    pct = tarifas.descuento_volumen(len(validos))
    total = 0.0
    for r in validos:
        cot = r['cotizacion']
        cot['conceptos'] = [c for c in cot['conceptos'] if c.get('icono') != 'fa-tags']
        cot['total'] = round(sum(c['importe'] for c in cot['conceptos']), 2)
        if pct:
            desc = round(-cot['total'] * pct / 100, 2)
            cot['conceptos'].append({'concepto': f'Descuento por volumen ({pct}%)', 'importe': desc, 'icono': 'fa-tags'})
            cot['total'] = round(cot['total'] + desc, 2)
        cot['descuento_pct'] = pct
        total += cot['total']
    return pct, round(total, 2)


@bp.route('/')
def inicio():
    return render_template('empresas/inicio.html', max_renglones=MAX_RENGLONES, descuentos=tarifas.DESCUENTOS_VOLUMEN)


@bp.route('/plantilla.csv')
def plantilla():
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(COLUMNAS)
    w.writerow(EJEMPLO)
    return Response('﻿' + salida.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=plantilla_envios_balula.csv'})


@bp.route('/cargar', methods=['POST'])
def cargar():
    f = request.form
    remitente = {k: f.get(f'rem_{k}', '').strip()[:200] for k in ('nombre', 'telefono', 'calle', 'colonia', 'ciudad', 'estado', 'cp')}
    remitente['telefono'] = re.sub(r'\D', '', remitente['telefono'])
    empresa, correo = f.get('empresa', '').strip()[:160], f.get('correo', '').strip()[:160]
    if len(empresa) < 2 or '@' not in correo or any(len(v) < 2 for v in remitente.values()) \
            or len(remitente['telefono']) != 10 or not re.fullmatch(r'\d{5}', remitente['cp']):
        flash('Completa los datos de la empresa y la dirección de recolección (teléfono de 10 dígitos y C.P. de 5).', 'danger')
        return redirect(url_for('empresas.inicio'))
    try:
        datos = leer_archivo(request.files.get('archivo') or abort(400))
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('empresas.inicio'))
    except Exception:
        current_app.logger.exception('Archivo de carga masiva ilegible')
        flash('No pudimos leer el archivo. Revisa que sea la plantilla en .csv (UTF-8) o .xlsx.', 'danger')
        return redirect(url_for('empresas.inicio'))

    compensar = f.get('compensar') == 'si'
    renglones, origen = cotizar_renglones(remitente, datos, compensar, current_app.config['GOOGLE_MAPS_API_KEY'])
    pct, total = aplicar_descuento(renglones)
    remitente['geo'] = origen
    lote = Lote(folio=db.nuevo_folio('LT', Lote), empresa=empresa, rfc=f.get('rfc', '').strip().upper()[:13] or None,
                correo=correo, remitente=remitente, renglones=renglones, compensar_co2=compensar, descuento_pct=pct, total=total)
    sa.session.add(lote)
    sa.session.commit()
    session['mis_lotes'] = (_mis_lotes() + [lote.folio])[-10:]
    return redirect(url_for('empresas.lote', folio=lote.folio))


@bp.route('/lote/<folio>')
def lote(folio):
    lote = _lote_propio(folio)
    validos = [r for r in lote.renglones if r['cotizacion']]
    return render_template('empresas/lote.html', lote=lote, validos=validos,
                           con_error=[r for r in lote.renglones if not r['cotizacion']],
                           envios=sorted(lote_envios(lote), key=lambda e: e.guia))


def lote_envios(lote):
    from models import Envio
    return sa.session.query(Envio).filter(Envio.lote_folio == lote.folio).all()


@bp.route('/lote/<folio>/pagar', methods=['POST'])
def pagar(folio):
    lote = _lote_propio(folio)
    if lote.estado != 'borrador':
        return redirect(url_for('empresas.lote', folio=lote.folio))
    if not current_app.config['PAGOS_SIMULADOS']:
        flash('El pago en línea aún no está disponible. Contáctanos para completar tu lote.', 'warning')
        return redirect(url_for('empresas.lote', folio=lote.folio))
    rem = {k: v for k, v in lote.remitente.items() if k != 'geo'}
    guias = []
    for r in lote.renglones:
        if not r['cotizacion']:
            continue
        guias.append(db.crear_envio(remitente=rem, destinatario=r['destinatario'], cotizacion=r['cotizacion'],
                                    tipo_entrega='recolector', correo_destinatario=r['destinatario'].get('correo'),
                                    origen=lote.remitente.get('geo'), destino=r['destino_geo'], lote_folio=lote.folio,
                                    commit=False))
    lote.estado = 'pagado'
    sa.session.commit()
    import notificaciones
    for g in guias:
        notificaciones.notificar(g, 'creado')
    session['mis_guias'] = (session.get('mis_guias', []) + guias)[-200:]
    flash(f'¡Listo! Se generaron {len(guias)} guías. Pasaremos a recolectarlas a tu dirección.', 'success')
    return redirect(url_for('empresas.lote', folio=lote.folio))


@bp.route('/lote/<folio>/guias.csv')
def guias_csv(folio):
    lote = _lote_propio(folio)
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(['guia', 'destinatario', 'ciudad', 'total', 'entrega_desde', 'entrega_hasta', 'rastreo'])
    base = current_app.config['PUBLIC_BASE_URL'] or request.url_root.rstrip('/')
    for e in sorted(lote_envios(lote), key=lambda e: e.guia):
        w.writerow([e.guia, e.destinatario['nombre'], e.destinatario['ciudad'], e.total, e.cotizacion['fecha_min'],
                    e.cotizacion['fecha_max'], f"{base}{url_for('rastreo', guia=e.guia)}"])
    return Response('﻿' + salida.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=guias_{lote.folio}.csv'})


@bp.route('/lote/<folio>/certificado-verde')
def certificado(folio):
    lote = _lote_propio(folio)
    envios = lote_envios(lote)
    if lote.estado != 'pagado' or not lote.compensar_co2 or not envios:
        abort(404)
    from clientes import ARBOL_KG_CO2_ANUAL
    return render_template('sitio/certificado.html', lote=lote, envio=None, items=len(envios),
                           kg=sum(e.cotizacion.get('co2_kg', 0) for e in envios),
                           aportacion=tarifas.COSTO_CO2 * len(envios), arbol_kg=ARBOL_KG_CO2_ANUAL)
