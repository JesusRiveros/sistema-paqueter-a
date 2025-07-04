from flask import Flask, render_template, request, session, redirect, url_for
from datetime import datetime, timedelta
import requests
import os
from fpdf import FPDF
import io
from flask import send_file
import uuid
import qrcode
from PIL import Image

PDF_FOLDER = os.path.join(os.getcwd(), 'static', 'guias')
os.makedirs(PDF_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = 'una_clave_super_secreta_1234'

DISTANCIA_TARIFAS = [
    (50, 60),
    (500, 120),
    (1500, 250),
    (3000, 350),
    (float('inf'), 400)
]

COSTO_POR_KG_EXTRA = 10
COSTO_ENVIO_EXPRES = 20
COSTO_CO2 = 5
BOX_COSTS = {
    'small': 10,
    'books': 15,
    'shoes': 25,
    'move': 40
}

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY") or "AIzaSyA549SLkse93t1ZxtweOjrCUn8jfWYowdk"

def descomponer_direccion(direccion):
    try:
        url = 'https://maps.googleapis.com/maps/api/geocode/json'
        params = {'address': direccion, 'key': GOOGLE_MAPS_API_KEY}
        resp = requests.get(url, params=params).json()
        components = resp['results'][0]['address_components']

        resultado = {
            'street': '',
            'neighborhood': '',
            'city': '',
            'state': ''
        }
        for comp in components:
            tipos = comp['types']
            if 'street_number' in tipos:
                resultado['street'] = comp['long_name'] + ' ' + resultado['street']
            if 'route' in tipos:
                resultado['street'] += comp['long_name']
            if 'sublocality_level_1' in tipos or 'neighborhood' in tipos:
                resultado['neighborhood'] = comp['long_name']
            if 'locality' in tipos:
                resultado['city'] = comp['long_name']
            if 'administrative_area_level_1' in tipos:
                resultado['state'] = comp['long_name']
        resultado['street'] = resultado['street'].strip()
        return resultado
    except Exception as e:
        print("Error descomponiendo dirección:", e)
        return {
            'street': direccion,
            'neighborhood': '',
            'city': '',
            'state': ''
        }

# Ruta raíz muestra la web estática (inicio)
@app.route('/')
def home():
    return render_template('html/inicio.html')

# --- Sistema paquetería bajo /app ---

@app.route('/app/')
def index():
    return render_template('index.html', paso_actual=1)

@app.route('/app/calculate', methods=['POST'], endpoint='calculate')
def calcular_envio():
    origin = request.form.get('origin')
    destination = request.form.get('destination')
    tipo_entrega = request.form.get('tipo_entrega')
    email = request.form.get('email')
    weight = float(request.form.get('weight'))
    length = float(request.form.get('length'))
    width = float(request.form.get('width'))
    height = float(request.form.get('height'))
    lat = request.form.get('lat')
    lon = request.form.get('lon')
    eco_packaging = request.form.get('eco_packaging')
    shipping_type = request.form.get('shipping_type')
    compensate = request.form.get('compensate') == 'yes'
    box_choice = request.form.get('box_choice')

    sender_parts = descomponer_direccion(origin)
    receiver_parts = descomponer_direccion(destination)

    session['sender_street'] = sender_parts['street']
    session['sender_neighborhood'] = sender_parts['neighborhood']
    session['sender_city'] = sender_parts['city']
    session['sender_state'] = sender_parts['state']

    session['receiver_street'] = receiver_parts['street']
    session['receiver_neighborhood'] = receiver_parts['neighborhood']
    session['receiver_city'] = receiver_parts['city']
    session['receiver_state'] = receiver_parts['state']

    distance_km = 100
    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/distancematrix/json',
            params={'origins': origin, 'destinations': destination, 'key': GOOGLE_MAPS_API_KEY}
        )
        data = response.json()
        distance_meters = data['rows'][0]['elements'][0]['distance']['value']
        distance_km = round(distance_meters / 1000, 2)
    except Exception as e:
        print("Error al obtener distancia:", e)

    costo_base = next(precio for rango, precio in DISTANCIA_TARIFAS if distance_km <= rango)
    peso_extra = max(0, weight - 1)
    costo_peso = peso_extra * COSTO_POR_KG_EXTRA
    costo_caja = BOX_COSTS.get(box_choice, 0)

    costo_extra = 0
    if shipping_type == 'fast':
        costo_extra += COSTO_ENVIO_EXPRES
    if compensate:
        costo_extra += COSTO_CO2

    cost = round(costo_base + costo_peso + costo_caja + costo_extra, 2)
    dias_estimados = 1 if shipping_type == 'fast' else 3
    delivery_date = (datetime.now() + timedelta(days=dias_estimados)).strftime('%Y-%m-%d')
    co2_emission = round(weight * distance_km * 0.21 / 1000, 2)

    session['costo_estimado'] = cost
    session['fecha_entrega_estimada'] = delivery_date
    session['weight'] = weight 

    return render_template(
        'result.html',
        origin=origin,
        tipo_entrega=tipo_entrega,
        destination=destination,
        email=email,
        cost=cost,
        delivery_date=delivery_date,
        lat=lat,
        lon=lon,
        eco_packaging=eco_packaging,
        shipping_type=shipping_type,
        co2_emission=co2_emission,
        distance_km=distance_km,
        compensated=compensate,
        box_choice=box_choice,
        weight=weight,
        length=length,
        width=width,
        height=height,
        paso_actual=2
    )

@app.route('/app/completar', methods=['GET', 'POST'])
def completar():
    if request.method == 'POST':
        session['sender_name'] = request.form.get('sender_name', '')
        session['sender_phone'] = request.form.get('sender_phone', '')
        session['sender_street'] = request.form.get('sender_street', '')
        session['sender_neighborhood'] = request.form.get('sender_neighborhood', '')
        session['sender_city'] = request.form.get('sender_city', '')
        session['sender_state'] = request.form.get('sender_state', '')

        session['receiver_name'] = request.form.get('receiver_name', '')
        session['receiver_phone'] = request.form.get('receiver_phone', '')
        session['receiver_street'] = request.form.get('receiver_street', '')
        session['receiver_neighborhood'] = request.form.get('receiver_neighborhood', '')
        session['receiver_city'] = request.form.get('receiver_city', '')
        session['receiver_state'] = request.form.get('receiver_state', '')
        session['receiver_references'] = request.form.get('receiver_references', '')

        session['costo_estimado'] = request.form.get('costo_estimado', session.get('costo_estimado', 'No definido'))
        session['fecha_entrega_estimada'] = request.form.get('fecha_entrega_estimada', session.get('fecha_entrega_estimada', 'No definida'))

        return redirect(url_for('pago'))

    return render_template(
        'completar.html',
        paso_actual=3,
        sender_street=session.get('sender_street', ''),
        sender_neighborhood=session.get('sender_neighborhood', ''),
        sender_city=session.get('sender_city', ''),
        sender_state=session.get('sender_state', ''),
        receiver_street=session.get('receiver_street', ''),
        receiver_neighborhood=session.get('receiver_neighborhood', ''),
        receiver_city=session.get('receiver_city', ''),
        receiver_state=session.get('receiver_state', '')
    )

@app.route('/app/pago')
def pago():
    resumen = {
        'sender_name': session.get('sender_name', ''),
        'sender_phone': session.get('sender_phone', ''),
        'sender_street': session.get('sender_street', ''),
        'sender_neighborhood': session.get('sender_neighborhood', ''),
        'sender_city': session.get('sender_city', ''),
        'sender_state': session.get('sender_state', ''),
        'receiver_name': session.get('receiver_name', ''),
        'receiver_phone': session.get('receiver_phone', ''),
        'receiver_street': session.get('receiver_street', ''),
        'receiver_neighborhood': session.get('receiver_neighborhood', ''),
        'receiver_city': session.get('receiver_city', ''),
        'receiver_state': session.get('receiver_state', ''),
        'receiver_references': session.get('receiver_references', ''),
        'cost': session.get('costo_estimado', 'No definido'),
        'delivery_date': session.get('fecha_entrega_estimada', 'No definida')
    }
    return render_template('pago.html', resumen=resumen, paso_actual=4)

@app.route('/app/procesar_pago', methods=['POST'])
def procesar_pago():
    return redirect(url_for('fake_checkout'))

@app.route('/app/fake_checkout', methods=['GET', 'POST'])
def fake_checkout():
    resumen = {
        'cost': session.get('costo_estimado', 'No definido'),
        'sender_name': session.get('sender_name', ''),
        'receiver_name': session.get('receiver_name', '')
    }

    if request.method == 'POST':
        resultado = request.form.get('resultado')
        if resultado == 'success':
            return redirect(url_for('pago_exitoso'))
        elif resultado == 'failure':
            return redirect(url_for('pago_fallido'))
        else:
            return redirect(url_for('pago_pendiente'))

    return render_template('fake_checkout.html', resumen=resumen)

@app.route('/app/pago_exitoso')
def pago_exitoso():
    tracking_code = f"BLL-{str(uuid.uuid4())[:8].upper()}"
    session['tracking'] = tracking_code

    data = {
        'nombre_rem': session.get('sender_name', ''),
        'origen': f"{session.get('sender_street', '')}, {session.get('sender_city', '')}, {session.get('sender_state', '')}",
        'nombre_dest': session.get('receiver_name', ''),
        'destino': f"{session.get('receiver_street', '')}, {session.get('receiver_city', '')}, {session.get('receiver_state', '')}",
        'peso': session.get('weight', 'N/A'),
        'costo': session.get('costo_estimado', 'N/A'),
        'fecha_entrega': session.get('fecha_entrega_estimada', 'N/A')
    }

    ruta_pdf = generar_guia_pdf(data, tracking_code)
    session['ruta_pdf'] = ruta_pdf  # 🔧 aquí lo guardas en la sesión
    print("PDF generado y guardado en sesión:", session.get('pdf_buf') is not None)


    return render_template('pago_exitoso.html',
                           sender_name=data['nombre_rem'],
                           receiver_name=data['nombre_dest'],
                           tracking=tracking_code)


@app.route('/app/pago_fallido')
def pago_fallido():
    return render_template('pago_fallido.html')

@app.route('/app/pago_pendiente')
def pago_pendiente():
    return render_template('pago_pendiente.html')

class PDFDHLStyle(FPDF):
    def header(self):
        try:
            self.image('static/Balula logo.png', 10, 8, 40)  # Cambia a tu logo DHL o quita si no tienes
        except:
            pass
        self.set_draw_color(204, 0, 0)  # rojo DHL
        self.set_line_width(5)
        self.line(10, 35, 200, 35)
        self.ln(30)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(128)
        fecha = datetime.now().strftime('%d/%m/%Y %H:%M')
        self.cell(0, 10, f'Guía generada el {fecha} - Página {self.page_no()}', align='C')

def generar_guia_pdf(data, tracking_code):
    pdf = PDFDHLStyle()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Encabezado
    pdf.set_font("Arial", 'B', 20)
    pdf.set_text_color(0)
    pdf.cell(0, 15, "GUÍA DE ENVÍO BALULA", ln=True, align='C')
    pdf.ln(5)

    # Código de rastreo
    pdf.set_font("Arial", 'B', 14)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(0, 12, f"CÓDIGO DE RASTREO: {tracking_code}", border=1, ln=True, align='C', fill=True)
    pdf.ln(10)

    # Sección Remitente / Destinatario
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(95, 10, "REMITENTE", border=1, align='C')
    pdf.cell(0, 10, "DESTINATARIO", border=1, align='C', ln=True)

    pdf.set_font("Arial", '', 11)
    pdf.cell(95, 10, data.get('nombre_rem', ''), border=1)
    pdf.cell(0, 10, data.get('nombre_dest', ''), border=1, ln=True)

    y_before = pdf.get_y()
    pdf.multi_cell(95, 10, data.get('origen', ''), border=1)
    y_after = pdf.get_y()

    pdf.set_xy(105, y_before)
    pdf.multi_cell(95, 10, data.get('destino', ''), border=1)

    pdf.set_y(max(y_after, pdf.get_y()) + 5)

    # Línea divisoria
    pdf.set_draw_color(150)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(8)

    # Información del envío
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(0, 10, "DETALLES DEL ENVÍO", ln=True)

    pdf.set_font("Arial", '', 11)
    pdf.cell(60, 8, f"Peso: {data.get('peso', 'N/A')} kg", ln=False)
    pdf.cell(80, 8, f"Costo estimado: ${data.get('costo', 'N/A')}", ln=False)
    pdf.cell(0, 8, f"Entrega estimada: {data.get('fecha_entrega', 'N/A')}", ln=True)

    pdf.ln(12)

    # Footer info
    pdf.set_font("Arial", 'I', 10)
    pdf.multi_cell(0, 8, "Gracias por elegir Balula. Esta guía es válida como comprobante de envío. Para seguimiento, escanee el código QR o visite nuestro sitio web.")
    pdf.ln(5)

    # 📌 Generar QR con URL al rastreo
    qr_url = f"http://localhost:5001/rastreo?tracking={tracking_code}"
    qr_img = qrcode.make(qr_url)
    qr_path = os.path.join(PDF_FOLDER, f"qr_{tracking_code}.png")
    qr_img.save(qr_path)

    # Insertar QR en el PDF (parte inferior)
    pdf.ln(5)
    pdf.set_font("Arial", 'I', 10)
    pdf.cell(0, 10, "Escanea para rastrear tu paquete:", ln=True)
    pdf.image(qr_path, x=80, w=50)  # Centrado aproximadamente

    # Agregar botón clickeable para redirigir al rastreo
    link_url = qr_url
    x_btn = 60
    y_btn = pdf.get_y() + 10
    w_btn = 90
    h_btn = 12

    # Dibujar rectángulo del botón (morado)
    pdf.set_fill_color(108, 85, 249)
    pdf.rect(x_btn, y_btn, w_btn, h_btn, style='F')

    # Texto blanco centrado dentro del botón
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(x_btn, y_btn + 3)
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(w_btn, h_btn, "Rastrear paquete", border=0, align='C', link=link_url)

    # Restaurar color texto negro para cualquier contenido futuro
    pdf.set_text_color(0, 0, 0)

    # Guardar PDF
    ruta_pdf = os.path.join(PDF_FOLDER, f"guia_{tracking_code}.pdf")
    pdf.output(ruta_pdf)
    return ruta_pdf


@app.route('/app/download/guia')
def download_guia():
    print("Entrando a /download/guia")
    print("¿PDF está en sesión?:", 'pdf_buf' in session)
    print("¿Tracking está en sesión?:", 'tracking' in session)
    print("Tamaño del PDF:", len(session.get('pdf_buf', b'')))
    ruta_pdf = session.get('ruta_pdf')
    tracking = session.get('tracking')

    if not ruta_pdf or not tracking or not os.path.exists(ruta_pdf):
        return "No hay guía generada", 404

    return send_file(ruta_pdf,
                     download_name=f"guia_{tracking}.pdf",
                     as_attachment=True)

@app.route('/formulario')
def formulario():
    return render_template('html/formulario.html')

@app.route('/inicio')
def inicio():
    return render_template('html/inicio.html')

@app.route('/rastreo')
def rastreo():
    return render_template('html/rastreo.html')


if __name__ == '__main__':
    app.run(debug=True, port=5001)
