"""Capa de datos: funciones de alto nivel sobre los modelos de SQLAlchemy (models.py).

El resto de la app usa estas funciones y recibe diccionarios simples, así las plantillas y el
asistente no dependen del ORM.
"""
import os
import secrets
from datetime import datetime, timedelta

from flask_migrate import Migrate
from sqlalchemy import func, inspect, select

from models import (ChatMensaje, Conversacion, Envio, Escalamiento, Evento, Incidencia, Lote, MensajeContacto,
                    Notificacion, PruebaEntrega, Queja, UbicacionRepartidor, Usuario, ahora, sa)

ESTADOS = [
    ('creado', 'Guía generada', 'fa-file-invoice'),
    ('recolectado', 'Paquete recolectado', 'fa-people-carry-box'),
    ('en_transito', 'En tránsito', 'fa-truck-fast'),
    ('en_reparto', 'En reparto', 'fa-route'),
    ('entregado', 'Entregado', 'fa-house-circle-check'),
]
ESTADOS_IDX = {clave: i for i, (clave, _, _) in enumerate(ESTADOS)}
NOMBRE_ESTADO = {clave: nombre for clave, nombre, _ in ESTADOS}

migrate = Migrate(render_as_batch=True)  # necesario para ALTER en SQLite
MIGRACIONES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')


def init_app(app):
    sa.init_app(app)
    migrate.init_app(app, sa, directory=MIGRACIONES)


def preparar_base(app):
    """Aplica migraciones pendientes. Si encuentra una base de la versión anterior (sin Alembic),
    la respalda y crea una nueva."""
    from flask_migrate import upgrade
    with app.app_context():
        insp = inspect(sa.engine)
        tablas = set(insp.get_table_names())
        if tablas and 'alembic_version' not in tablas:
            ruta = sa.engine.url.database
            sa.engine.dispose()
            if ruta and os.path.exists(ruta):
                respaldo = f'{ruta}.respaldo-{datetime.now():%Y%m%d%H%M%S}'
                os.replace(ruta, respaldo)
                app.logger.warning('Base anterior respaldada en %s', respaldo)
        upgrade(directory=MIGRACIONES)


def get_session():
    return sa.session


def _iso(valor):
    return valor.isoformat(timespec='seconds') if isinstance(valor, datetime) else valor


def _ahora_iso():
    return _iso(ahora())


# ---------------------------------------------------------------------------
# Envíos y rastreo
# ---------------------------------------------------------------------------

def nuevo_codigo_guia():
    while True:
        codigo = f'BLL-{secrets.token_hex(4).upper()}'
        if not sa.session.get(Envio, codigo):
            return codigo


def crear_envio(*, remitente, destinatario, cotizacion, tipo_entrega, correo_destinatario,
                origen=None, destino=None, lote_folio=None, commit=True):
    """origen/destino: dict opcional con lat y lng."""
    envio = Envio(
        guia=nuevo_codigo_guia(), estado='creado', remitente=remitente, destinatario=destinatario,
        cotizacion=cotizacion, tipo_entrega=tipo_entrega, correo_destinatario=correo_destinatario or None,
        total=cotizacion['total'], codigo_entrega=f'{secrets.randbelow(10000):04d}',
        origen_lat=(origen or {}).get('lat'), origen_lng=(origen or {}).get('lng'),
        destino_lat=(destino or {}).get('lat'), destino_lng=(destino or {}).get('lng'), lote_folio=lote_folio,
    )
    envio.eventos.append(Evento(
        estado='creado', descripcion='Pago confirmado y guía generada',
        ubicacion=f"{remitente.get('ciudad', '')}, {remitente.get('estado', '')}".strip(', ')))
    sa.session.add(envio)
    if commit:
        sa.session.commit()
    else:
        sa.session.flush()
    return envio.guia


def envio_a_dict(envio, eventos=True):
    d = {c.key: getattr(envio, c.key) for c in inspect(Envio).mapper.column_attrs}
    d['creado_en'] = _iso(envio.creado_en)
    d['asignado_en'] = _iso(envio.asignado_en)
    d['repartidor_nombre'] = envio.repartidor.nombre if envio.repartidor else None
    if eventos:
        d['eventos'] = [{'estado': e.estado, 'descripcion': e.descripcion, 'ubicacion': e.ubicacion,
                         'fecha': _iso(e.fecha), 'lat': e.lat, 'lng': e.lng}
                        for e in sorted(envio.eventos, key=lambda e: (e.fecha, e.id), reverse=True)]
    return d


def obtener_envio(guia):
    envio = sa.session.get(Envio, (guia or '').upper())
    return envio_a_dict(envio) if envio else None


def obtener_envio_modelo(guia):
    return sa.session.get(Envio, (guia or '').upper())


def registrar_evento(guia, estado, descripcion=None, ubicacion=None, usuario_id=None, lat=None, lng=None):
    if estado not in ESTADOS_IDX:
        raise ValueError(f'Estado inválido: {estado}. Opciones: {", ".join(ESTADOS_IDX)}')
    envio = sa.session.get(Envio, guia)
    if not envio:
        raise ValueError(f'No existe la guía {guia}')
    envio.eventos.append(Evento(estado=estado, descripcion=descripcion or NOMBRE_ESTADO[estado],
                                ubicacion=ubicacion, usuario_id=usuario_id, lat=lat, lng=lng))
    envio.estado = estado
    sa.session.commit()


def guardar_mensaje(nombre, correo, mensaje):
    sa.session.add(MensajeContacto(nombre=nombre, correo=correo, mensaje=mensaje))
    sa.session.commit()


# ---------------------------------------------------------------------------
# Asistente
# ---------------------------------------------------------------------------

def nuevo_folio(prefijo, modelo):
    while True:
        folio = f'{prefijo}-{secrets.token_hex(3).upper()}'
        if not sa.session.scalar(select(modelo).where(modelo.folio == folio)):
            return folio


def crear_conversacion(conversacion_id, canal):
    sa.session.add(Conversacion(id=conversacion_id, canal=canal))
    sa.session.commit()


def obtener_conversacion(conversacion_id):
    c = sa.session.get(Conversacion, conversacion_id) if conversacion_id else None
    if not c:
        return None
    return {col.key: getattr(c, col.key) for col in inspect(Conversacion).mapper.column_attrs}


def agregar_chat_mensaje(conversacion_id, rol, texto):
    sa.session.add(ChatMensaje(conversacion_id=conversacion_id, rol=rol, texto=texto))
    conv = sa.session.get(Conversacion, conversacion_id)
    conv.actualizada = ahora()
    sa.session.commit()


def chat_mensajes(conversacion_id, roles=('cliente', 'asistente'), limite=None):
    q = (select(ChatMensaje).where(ChatMensaje.conversacion_id == conversacion_id, ChatMensaje.rol.in_(roles))
         .order_by(ChatMensaje.id.desc()))
    if limite:
        q = q.limit(limite)
    filas = sa.session.scalars(q).all()
    return [{'rol': m.rol, 'texto': m.texto, 'fecha': _iso(m.fecha)} for m in reversed(filas)]


def sumar_uso_conversacion(conversacion_id, *, entrada, salida, cache_lectura, cache_escritura, costo, turno):
    c = sa.session.get(Conversacion, conversacion_id)
    c.llamadas_api += 1
    c.tokens_entrada += entrada
    c.tokens_salida += salida
    c.tokens_cache_lectura += cache_lectura
    c.tokens_cache_escritura += cache_escritura
    c.costo_usd += costo
    c.turnos += 1 if turno else 0
    sa.session.commit()


def registrar_queja(*, conversacion_id, guia, tipo, descripcion, nombre_cliente, telefono):
    folio = nuevo_folio('QJ', Queja)
    sa.session.add(Queja(folio=folio, conversacion_id=conversacion_id, guia=guia or None, tipo=tipo,
                         descripcion=descripcion, nombre_cliente=nombre_cliente or None, telefono=telefono or None))
    sa.session.commit()
    return folio


def registrar_escalamiento(*, conversacion_id, motivo, resumen, telefono):
    folio = nuevo_folio('ESC', Escalamiento)
    sa.session.add(Escalamiento(folio=folio, conversacion_id=conversacion_id, motivo=motivo, resumen=resumen,
                                telefono=telefono or None))
    sa.session.get(Conversacion, conversacion_id).estado = 'escalada'
    sa.session.commit()
    return folio


def resumen_costos_asistente():
    c = Conversacion
    fila = sa.session.execute(
        select(func.count(), func.coalesce(func.sum(c.turnos), 0), func.coalesce(func.sum(c.llamadas_api), 0),
               func.coalesce(func.sum(c.tokens_entrada), 0), func.coalesce(func.sum(c.tokens_salida), 0),
               func.coalesce(func.sum(c.tokens_cache_lectura), 0), func.coalesce(func.sum(c.tokens_cache_escritura), 0),
               func.coalesce(func.sum(c.costo_usd), 0.0)).where(c.llamadas_api > 0)).one()
    claves = ['conversaciones', 'turnos', 'llamadas', 'entrada', 'salida', 'cache_lectura', 'cache_escritura', 'costo']
    return dict(zip(claves, fila))


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------

def usuario_por_correo(correo):
    return sa.session.scalar(select(Usuario).where(func.lower(Usuario.correo) == (correo or '').strip().lower()))


def crear_usuario(*, nombre, correo, password, rol, telefono=None, zona=None, debe_cambiar_password=False):
    if rol not in ('admin', 'repartidor'):
        raise ValueError('Rol inválido')
    if usuario_por_correo(correo):
        raise ValueError('Ya existe un usuario con ese correo')
    u = Usuario(nombre=nombre, correo=correo.strip().lower(), rol=rol, telefono=telefono, zona=zona,
                debe_cambiar_password=debe_cambiar_password)
    u.fijar_password(password)
    sa.session.add(u)
    sa.session.commit()
    return u


MAX_INTENTOS = 5
MINUTOS_BLOQUEO = 15


def autenticar(correo, password):
    """Devuelve (usuario, error). Bloquea la cuenta tras varios intentos fallidos."""
    u = usuario_por_correo(correo)
    generico = 'Correo o contraseña incorrectos.'
    if not u or not u.activo:
        # Comparación de todos modos para no revelar qué correos existen por el tiempo de respuesta
        Usuario(password_hash='scrypt:32768:8:1$x$' + '0' * 128).verificar_password(password or '')
        return None, generico
    if u.bloqueado_hasta and u.bloqueado_hasta > datetime.now():
        minutos = int((u.bloqueado_hasta - datetime.now()).total_seconds() // 60) + 1
        return None, f'Cuenta bloqueada por intentos fallidos. Intenta de nuevo en {minutos} min.'
    if not u.verificar_password(password or ''):
        u.intentos_fallidos += 1
        if u.intentos_fallidos >= MAX_INTENTOS:
            u.bloqueado_hasta = datetime.now() + timedelta(minutes=MINUTOS_BLOQUEO)
            u.intentos_fallidos = 0
        sa.session.commit()
        return None, generico
    u.intentos_fallidos = 0
    u.bloqueado_hasta = None
    u.ultimo_acceso = ahora()
    sa.session.commit()
    return u, None


def obtener_usuario(usuario_id):
    return sa.session.get(Usuario, usuario_id) if usuario_id else None


__all__ = ['sa', 'Usuario', 'Envio', 'Evento', 'PruebaEntrega', 'Incidencia', 'UbicacionRepartidor',
           'MensajeContacto', 'Notificacion', 'Conversacion', 'ChatMensaje', 'Queja', 'Escalamiento']
