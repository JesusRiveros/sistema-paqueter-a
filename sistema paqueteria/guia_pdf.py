"""Generación de la guía de envío en PDF (en memoria, sin archivos temporales)."""
import os
from datetime import date, datetime

import qrcode
from fpdf import FPDF

import tarifas

NARANJA = (253, 55, 0)
NARANJA_CLARO = (255, 240, 233)
TINTA = (38, 24, 18)
GRIS = (120, 108, 102)
LINEA = (238, 226, 220)

LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'img', 'logo.png')


def _t(texto):
    """Las fuentes base del PDF son latin-1: reemplaza los caracteres que no lo son."""
    texto = str(texto if texto is not None else '')
    for a, b in {'₂': '2', '—': '-', '–': '-', '“': '"', '”': '"', '’': "'", '…': '...'}.items():
        texto = texto.replace(a, b)
    return texto.encode('latin-1', 'replace').decode('latin-1')


def _fecha(iso):
    meses = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
    d = date.fromisoformat(iso[:10])
    return f'{d.day} {meses[d.month - 1]} {d.year}'


class _GuiaPDF(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font('Helvetica', '', 8)
        self.set_text_color(*GRIS)
        self.cell(0, 6, _t(f'Balula Express  ·  Guía generada el {datetime.now():%d/%m/%Y %H:%M}'
                           f'  ·  Página {self.page_no()}'), align='C')


def _titulo_seccion(pdf, texto, x, w):
    pdf.set_xy(x, pdf.get_y())
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(*NARANJA)
    pdf.cell(w, 6, _t(texto.upper()))
    pdf.ln(7)


def _bloque_persona(pdf, titulo, p, x, y, w):
    pdf.set_draw_color(*LINEA)
    pdf.set_fill_color(255, 255, 255)
    pdf.set_xy(x, y)
    _titulo_seccion(pdf, titulo, x, w)
    pdf.set_x(x)
    pdf.set_font('Helvetica', 'B', 12)
    pdf.set_text_color(*TINTA)
    pdf.multi_cell(w, 6, _t(p.get('nombre', '')))
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(70, 60, 55)
    lineas = [p.get('calle', ''), p.get('colonia', ''),
              ', '.join(v for v in (p.get('ciudad'), p.get('estado'), p.get('cp')) if v),
              f"Tel. {p.get('telefono', '')}"]
    if p.get('referencias'):
        lineas.append(f"Ref.: {p['referencias']}")
    for linea in lineas:
        if linea:
            pdf.set_x(x)
            pdf.multi_cell(w, 5, _t(linea))
    return pdf.get_y()


def generar_guia_pdf(envio, url_rastreo):
    """Devuelve los bytes del PDF para un envío (dict de db.obtener_envio)."""
    cot = envio['cotizacion']
    guia = envio['guia']
    pdf = _GuiaPDF(format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    ancho = pdf.w - 30

    # Encabezado
    pdf.set_fill_color(*NARANJA)
    pdf.rect(0, 0, pdf.w, 4, style='F')
    if os.path.exists(LOGO):
        pdf.image(LOGO, x=15, y=11, h=16)
    pdf.set_xy(34, 12)
    pdf.set_font('Helvetica', 'B', 18)
    pdf.set_text_color(*TINTA)
    pdf.cell(60, 8, 'Balula')
    pdf.set_xy(34, 19)
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(*NARANJA)
    pdf.cell(60, 6, 'EXPRESS')
    pdf.set_xy(110, 12)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(*GRIS)
    pdf.cell(85, 5, _t('GUÍA DE ENVÍO'), align='R')
    pdf.set_xy(110, 17)
    pdf.set_font('Helvetica', 'B', 20)
    pdf.set_text_color(*TINTA)
    pdf.cell(85, 10, guia, align='R')

    # Banda de código: código de barras + QR
    y = 34
    pdf.set_fill_color(*NARANJA_CLARO)
    pdf.rect(15, y, ancho, 44, style='F', round_corners=True, corner_radius=3)
    pdf.set_fill_color(0, 0, 0)
    pdf.code39(f'*{guia}*', x=22, y=y + 8, w=0.9, h=20)
    pdf.set_xy(22, y + 30)
    pdf.set_font('Courier', 'B', 11)
    pdf.set_text_color(*TINTA)
    pdf.cell(110, 6, guia)
    servicio = tarifas.TIPOS_ENVIO[cot['tipo_envio']]
    pdf.set_xy(22, y + 36)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(*NARANJA)
    pdf.cell(110, 5, _t(f"SERVICIO {servicio['nombre'].upper()}  ·  "
                        f"{tarifas.TIPOS_ENTREGA.get(envio['tipo_entrega'], {}).get('nombre', '').upper()}"))

    qr = qrcode.QRCode(border=1, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url_rastreo)
    qr.make(fit=True)
    pdf.image(qr.make_image(fill_color='black', back_color='white').get_image(),
              x=15 + ancho - 40, y=y + 3, w=38, h=38, link=url_rastreo)

    # Remitente / destinatario
    y = y + 52
    col = (ancho - 10) / 2
    y1 = _bloque_persona(pdf, 'Remitente', envio['remitente'], 15, y, col)
    y2 = _bloque_persona(pdf, 'Destinatario', envio['destinatario'], 15 + col + 10, y, col)
    pdf.set_draw_color(*LINEA)
    pdf.line(15 + col + 5, y, 15 + col + 5, max(y1, y2))
    pdf.set_y(max(y1, y2) + 8)

    # Detalles del paquete
    _titulo_seccion(pdf, 'Detalles del paquete', 15, ancho)
    caja = tarifas.CAJAS.get(cot['caja'], {}).get('nombre', '')
    detalles = [
        ('Medidas', f"{cot['largo']:g} x {cot['ancho']:g} x {cot['alto']:g} cm"),
        ('Peso real', f"{cot['peso_real']:g} kg"),
        ('Peso volumétrico', f"{cot['peso_volumetrico']:g} kg"),
        ('Peso facturable', f"{cot['peso_facturable']:g} kg"),
        ('Empaque', caja),
        ('Distancia', f"{cot['distancia_km']:g} km"),
        ('Entrega estimada', f"{_fecha(cot['fecha_min'])} - {_fecha(cot['fecha_max'])}"),
        ('CO2 estimado', f"{cot['co2_kg']:g} kg" + (' (compensado)' if cot['compensar_co2'] else '')),
    ]
    celda = ancho / 4
    for i, (etiqueta, valor) in enumerate(detalles):
        if i % 4 == 0:
            fila_y = pdf.get_y()
        x = 15 + (i % 4) * celda
        pdf.set_xy(x, fila_y)
        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(*GRIS)
        pdf.cell(celda, 4, _t(etiqueta))
        pdf.set_xy(x, fila_y + 4)
        pdf.set_font('Helvetica', 'B', 10)
        pdf.set_text_color(*TINTA)
        pdf.cell(celda - 2, 6, _t(valor))
        if i % 4 == 3:
            pdf.set_y(fila_y + 13)
    pdf.ln(4)

    # Desglose de costos
    _titulo_seccion(pdf, 'Desglose de costos', 15, ancho)
    pdf.set_font('Helvetica', '', 10)
    for c in cot['conceptos']:
        pdf.set_text_color(70, 60, 55)
        pdf.cell(ancho - 35, 7, _t(c['concepto']))
        pdf.set_text_color(*TINTA)
        pdf.cell(35, 7, f"${c['importe']:,.2f}", align='R', new_x='LMARGIN', new_y='NEXT')
        pdf.set_draw_color(*LINEA)
        pdf.line(15, pdf.get_y(), 15 + ancho, pdf.get_y())
    pdf.ln(2)
    pdf.set_font('Helvetica', 'B', 13)
    pdf.cell(ancho - 35, 9, 'Total pagado (MXN)')
    pdf.set_text_color(*NARANJA)
    pdf.cell(35, 9, f"${cot['total']:,.2f}", align='R', new_x='LMARGIN', new_y='NEXT')

    # Nota final
    pdf.ln(6)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(*GRIS)
    pdf.multi_cell(ancho, 5, align='L', text=_t(
        'Imprime esta guía y pégala en un lado visible del paquete. Escanea el código QR o visita '
        f'{url_rastreo} para conocer el estado de tu envío. Gracias por elegir Balula Express.'))

    return bytes(pdf.output())
