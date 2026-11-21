"""Datos de demostración: `flask --app app sembrar-demo`.

Crea 1 administrador, 3 repartidores y ~20 envíos con direcciones reales de CDMX (coordenadas
aproximadas), repartidos en los últimos 14 días y en distintos estados. Las contraseñas se generan al
azar y se imprimen en la consola (o usa --password para fijar una).
"""
import random
import secrets
from datetime import date, datetime, timedelta

import click
from sqlalchemy import select

import db
import tarifas
from models import Envio, Evento, PruebaEntrega, Usuario, sa

# (calle, colonia, alcaldía, CP, lat, lng)
DIRECCIONES = [
    ('Av. Paseo de la Reforma 222', 'Juárez', 'Cuauhtémoc', '06600', 19.4270, -99.1630),
    ('Calle Madero 17', 'Centro', 'Cuauhtémoc', '06000', 19.4339, -99.1388),
    ('Av. Insurgentes Sur 1235', 'Del Valle', 'Benito Juárez', '03100', 19.3740, -99.1780),
    ('Calle Ámsterdam 240', 'Hipódromo', 'Cuauhtémoc', '06100', 19.4110, -99.1700),
    ('Av. Coyoacán 1435', 'Del Valle', 'Benito Juárez', '03100', 19.3715, -99.1700),
    ('Calle Francisco Sosa 383', 'Coyoacán', 'Coyoacán', '04000', 19.3480, -99.1680),
    ('Av. Universidad 1000', 'Santa Cruz Atoyac', 'Benito Juárez', '03310', 19.3620, -99.1640),
    ('Calz. de Tlalpan 3465', 'Santa Úrsula Coapa', 'Coyoacán', '04650', 19.3070, -99.1500),
    ('Av. Presidente Masaryk 360', 'Polanco', 'Miguel Hidalgo', '11560', 19.4320, -99.1950),
    ('Calle Lago Zurich 245', 'Ampliación Granada', 'Miguel Hidalgo', '11529', 19.4400, -99.2040),
    ('Av. Montevideo 363', 'Lindavista', 'Gustavo A. Madero', '07300', 19.4950, -99.1300),
    ('Calz. Vallejo 1268', 'Santa Rosa', 'Gustavo A. Madero', '07620', 19.5000, -99.1530),
    ('Av. Aquiles Serdán 400', 'Santa Apolonia', 'Azcapotzalco', '02790', 19.4720, -99.1900),
    ('Av. Ermita Iztapalapa 3016', 'Santa María Aztahuacán', 'Iztapalapa', '09500', 19.3560, -99.0350),
    ('Av. Tláhuac 4380', 'Granjas Estrella', 'Iztapalapa', '09880', 19.3250, -99.0950),
    ('Calz. Ignacio Zaragoza 1100', 'Agrícola Pantitlán', 'Iztacalco', '08100', 19.4110, -99.0700),
    ('Av. Río Churubusco 601', 'Xoco', 'Benito Juárez', '03330', 19.3590, -99.1660),
    ('Av. Revolución 1267', 'Tlacopac', 'Álvaro Obregón', '01049', 19.3500, -99.1900),
    ('Av. Santa Fe 482', 'Santa Fe', 'Cuajimalpa', '05348', 19.3650, -99.2640),
    ('Calle Durango 230', 'Roma Norte', 'Cuauhtémoc', '06700', 19.4190, -99.1650),
    ('Calle Tamaulipas 66', 'Condesa', 'Cuauhtémoc', '06140', 19.4120, -99.1720),
    ('Av. Cuauhtémoc 1080', 'Narvarte', 'Benito Juárez', '03020', 19.3930, -99.1540),
]
NOMBRES = ['Ana López', 'Luis Hernández', 'María García', 'Jorge Martínez', 'Sofía Ramírez', 'Carlos Torres',
           'Valeria Flores', 'Diego Sánchez', 'Fernanda Cruz', 'Ricardo Morales', 'Paola Reyes', 'Miguel Ortiz',
           'Daniela Gómez', 'Andrés Vargas', 'Camila Castillo', 'Emilio Rojas', 'Regina Mendoza', 'Santiago Díaz',
           'Ximena Herrera', 'Mateo Aguilar', 'Renata Silva', 'Iván Navarro']
REPARTIDORES = [('Pedro Salinas', 'pedro@balula.mx', 'Norte CDMX'), ('Lucía Benítez', 'lucia@balula.mx', 'Centro CDMX'),
                ('Raúl Ibarra', 'raul@balula.mx', 'Sur CDMX')]


def _persona(nombre, d, rnd):
    calle, colonia, alcaldia, cp, _, _ = d
    return {'nombre': nombre, 'telefono': f'55{rnd.randint(10000000, 99999999)}', 'calle': calle, 'colonia': colonia,
            'ciudad': alcaldia, 'estado': 'Ciudad de México', 'cp': cp}


def sembrar(password=None, semilla=2026):
    rnd = random.Random(semilla)
    credenciales = []
    if not db.usuario_por_correo('admin@balula.mx'):
        pw = password or secrets.token_urlsafe(9) + '7a'
        db.crear_usuario(nombre='Operaciones Balula', correo='admin@balula.mx', password=pw, rol='admin')
        credenciales.append(('admin@balula.mx', pw))
    reps = []
    for nombre, correo, zona in REPARTIDORES:
        u = db.usuario_por_correo(correo)
        if not u:
            pw = password or secrets.token_urlsafe(9) + '7a'
            u = db.crear_usuario(nombre=nombre, correo=correo, password=pw, rol='repartidor',
                                 telefono=f'55{rnd.randint(10000000, 99999999)}', zona=zona)
            credenciales.append((correo, pw))
        reps.append(u)

    if sa.session.scalar(select(Envio).limit(1)) is not None:
        return credenciales, 0  # ya hay envíos: no duplicar

    origen = ('Av. Industria 742', 'Industrial Vallejo', 'Azcapotzalco', '02300', 19.4895, -99.1795)
    hoy = date.today()
    ahora = datetime.now().replace(microsecond=0)
    creados = 0
    for i, d in enumerate(DIRECCIONES):
        dias_atras = rnd.randint(2, 13) if i >= 12 else 0
        if dias_atras:
            creado = datetime.combine(hoy - timedelta(days=dias_atras), datetime.min.time()) + timedelta(hours=rnd.randint(8, 18))
        else:  # envíos de hoy: creados en las últimas horas (nunca en el futuro)
            creado = ahora - timedelta(minutes=rnd.randint(90, 360))
        tipo = 'fast' if rnd.random() < 0.4 else 'eco'
        cot = tarifas.cotizar(distancia_km=round(rnd.uniform(4, 28), 1), peso=round(rnd.uniform(0.3, 9), 1),
                              largo=rnd.choice([20, 30, 40]), ancho=rnd.choice([15, 20, 30]), alto=rnd.choice([5, 10, 20]),
                              tipo_envio=tipo, compensar_co2=rnd.random() < 0.35, hoy=creado.date()).to_dict()
        guia = db.nuevo_codigo_guia()
        envio = Envio(guia=guia, creado_en=creado, estado='creado', remitente=_persona('Tienda Demo Balula', origen, rnd),
                      destinatario=_persona(NOMBRES[i], d, rnd) | {'referencias': rnd.choice(['', 'Portón negro', 'Depto 3, tocar timbre 2', 'Frente a la farmacia'])},
                      cotizacion=cot, tipo_entrega='recolector', correo_destinatario=None, total=cot['total'],
                      codigo_entrega=f'{rnd.randint(0, 9999):04d}', origen_lat=origen[4], origen_lng=origen[5],
                      destino_lat=d[4], destino_lng=d[5])
        envio.eventos.append(Evento(estado='creado', descripcion='Pago confirmado y guía generada',
                                    ubicacion='Azcapotzalco, Ciudad de México', fecha=creado))
        # Los más viejos ya se entregaron; los de hoy están en distintas etapas
        if dias_atras >= 2:
            rep = rnd.choice(reps)
            envio.repartidor_id, envio.asignado_en = rep.id, creado
            for j, est in enumerate(['recolectado', 'en_transito', 'en_reparto', 'entregado'], start=1):
                envio.eventos.append(Evento(estado=est, descripcion=db.NOMBRE_ESTADO[est], fecha=min(creado + timedelta(hours=6 * j), ahora),
                                            ubicacion='CEDIS Vallejo' if est == 'recolectado' else None, usuario_id=rep.id))
            envio.estado = 'entregado'
            sa.session.add(envio)
            sa.session.flush()
            sa.session.add(PruebaEntrega(guia=guia, repartidor_id=rep.id, receptor_nombre=NOMBRES[i], relacion='destinatario',
                                         fecha=min(creado + timedelta(hours=24), ahora), lat=d[4], lng=d[5]))
        else:
            if i < 9:  # asignados a un repartidor hoy
                rep = reps[i % len(reps)]
                envio.repartidor_id, envio.asignado_en = rep.id, creado
                for k, est in enumerate(['recolectado', 'en_transito', 'en_reparto'][:1 + (i // 3) % 3], start=1):
                    envio.eventos.append(Evento(estado=est, descripcion=db.NOMBRE_ESTADO[est], fecha=creado + timedelta(minutes=20 * k),
                                                usuario_id=rep.id))
                    envio.estado = est
            sa.session.add(envio)  # el resto queda sin asignar (para probar la asignación automática)
        creados += 1
    sa.session.commit()
    return credenciales, creados


def registrar_comando(app):
    @app.cli.command('sembrar-demo')
    @click.option('--password', default=None, help='Contraseña fija para todos los usuarios demo (mín. 10 caracteres con letras y números).')
    def sembrar_demo(password):
        """Crea usuarios y envíos de demostración (no duplica si ya existen)."""
        credenciales, n = sembrar(password)
        click.echo(f'Envíos creados: {n}')
        if credenciales:
            click.echo('Usuarios creados (guarda estas contraseñas; no se vuelven a mostrar):')
            for correo, pw in credenciales:
                click.echo(f'  {correo:24} {pw}')
        else:
            click.echo('Los usuarios demo ya existían.')
