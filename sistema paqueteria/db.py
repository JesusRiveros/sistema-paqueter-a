"""Persistencia en SQLite: envíos, eventos de rastreo y mensajes de contacto."""
import json
import os
import secrets
import sqlite3
from datetime import datetime

from flask import current_app, g

ESTADOS = [
    ('creado', 'Guía generada', 'fa-file-invoice'),
    ('recolectado', 'Paquete recolectado', 'fa-people-carry-box'),
    ('en_transito', 'En tránsito', 'fa-truck-fast'),
    ('en_reparto', 'En reparto', 'fa-route'),
    ('entregado', 'Entregado', 'fa-house-circle-check'),
]
ESTADOS_IDX = {clave: i for i, (clave, _, _) in enumerate(ESTADOS)}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS envios (
    guia            TEXT PRIMARY KEY,
    creado_en       TEXT NOT NULL,
    estado          TEXT NOT NULL DEFAULT 'creado',
    remitente       TEXT NOT NULL,   -- JSON
    destinatario    TEXT NOT NULL,   -- JSON
    cotizacion      TEXT NOT NULL,   -- JSON
    tipo_entrega    TEXT NOT NULL,
    correo_destinatario TEXT,
    total           REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guia        TEXT NOT NULL REFERENCES envios(guia),
    estado      TEXT NOT NULL,
    descripcion TEXT NOT NULL,
    ubicacion   TEXT,
    fecha       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eventos_guia ON eventos(guia, fecha);
CREATE TABLE IF NOT EXISTS mensajes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre    TEXT NOT NULL,
    correo    TEXT NOT NULL,
    mensaje   TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

-- Asistente de atención al cliente
CREATE TABLE IF NOT EXISTS conversaciones (
    id                    TEXT PRIMARY KEY,
    canal                 TEXT NOT NULL,           -- web | voz | telefono
    creada                TEXT NOT NULL,
    actualizada           TEXT NOT NULL,
    estado                TEXT NOT NULL DEFAULT 'abierta',  -- abierta | escalada
    turnos                INTEGER NOT NULL DEFAULT 0,
    llamadas_api          INTEGER NOT NULL DEFAULT 0,
    tokens_entrada        INTEGER NOT NULL DEFAULT 0,
    tokens_salida         INTEGER NOT NULL DEFAULT 0,
    tokens_cache_lectura  INTEGER NOT NULL DEFAULT 0,
    tokens_cache_escritura INTEGER NOT NULL DEFAULT 0,
    costo_usd             REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_mensajes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversacion_id TEXT NOT NULL REFERENCES conversaciones(id),
    rol             TEXT NOT NULL,   -- cliente | asistente | herramienta
    texto           TEXT NOT NULL,
    fecha           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_mensajes(conversacion_id, id);
CREATE TABLE IF NOT EXISTS quejas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folio           TEXT NOT NULL UNIQUE,
    conversacion_id TEXT REFERENCES conversaciones(id),
    guia            TEXT,
    tipo            TEXT NOT NULL,
    descripcion     TEXT NOT NULL,
    nombre_cliente  TEXT,
    telefono        TEXT,
    estado          TEXT NOT NULL DEFAULT 'abierta',
    creada          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalamientos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folio           TEXT NOT NULL UNIQUE,
    conversacion_id TEXT REFERENCES conversaciones(id),
    motivo          TEXT NOT NULL,   -- sospecha_fraude | amenaza_legal | monto_supera_tope | cliente_pide_humano
    resumen         TEXT NOT NULL,
    telefono        TEXT,
    estado          TEXT NOT NULL DEFAULT 'pendiente',
    creado          TEXT NOT NULL
);
"""


def _ruta_db():
    return current_app.config['DATABASE']


def get_db():
    if 'db' not in g:
        os.makedirs(os.path.dirname(_ruta_db()), exist_ok=True)
        g.db = sqlite3.connect(_ruta_db())
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def close_db(_exc=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    get_db().executescript(ESQUEMA)


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


def _ahora():
    return datetime.now().isoformat(timespec='seconds')


def nuevo_codigo_guia():
    db = get_db()
    while True:
        codigo = f'BLL-{secrets.token_hex(4).upper()}'
        if not db.execute('SELECT 1 FROM envios WHERE guia = ?', (codigo,)).fetchone():
            return codigo


def crear_envio(*, remitente, destinatario, cotizacion, tipo_entrega, correo_destinatario):
    db = get_db()
    guia = nuevo_codigo_guia()
    ahora = _ahora()
    db.execute(
        'INSERT INTO envios (guia, creado_en, estado, remitente, destinatario, cotizacion, '
        'tipo_entrega, correo_destinatario, total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (guia, ahora, 'creado', json.dumps(remitente, ensure_ascii=False),
         json.dumps(destinatario, ensure_ascii=False), json.dumps(cotizacion, ensure_ascii=False),
         tipo_entrega, correo_destinatario, cotizacion['total']),
    )
    db.execute(
        'INSERT INTO eventos (guia, estado, descripcion, ubicacion, fecha) VALUES (?, ?, ?, ?, ?)',
        (guia, 'creado', 'Pago confirmado y guía generada',
         f"{remitente.get('ciudad', '')}, {remitente.get('estado', '')}".strip(', '), ahora),
    )
    db.commit()
    return guia


def obtener_envio(guia):
    fila = get_db().execute('SELECT * FROM envios WHERE guia = ?', (guia.upper(),)).fetchone()
    if not fila:
        return None
    envio = dict(fila)
    for campo in ('remitente', 'destinatario', 'cotizacion'):
        envio[campo] = json.loads(envio[campo])
    envio['eventos'] = [dict(e) for e in get_db().execute(
        'SELECT estado, descripcion, ubicacion, fecha FROM eventos WHERE guia = ? ORDER BY fecha DESC, id DESC',
        (envio['guia'],))]
    return envio


def registrar_evento(guia, estado, descripcion=None, ubicacion=None):
    if estado not in ESTADOS_IDX:
        raise ValueError(f'Estado inválido: {estado}. Opciones: {", ".join(ESTADOS_IDX)}')
    db = get_db()
    if not db.execute('SELECT 1 FROM envios WHERE guia = ?', (guia,)).fetchone():
        raise ValueError(f'No existe la guía {guia}')
    descripcion = descripcion or dict((k, n) for k, n, _ in ESTADOS)[estado]
    db.execute('INSERT INTO eventos (guia, estado, descripcion, ubicacion, fecha) VALUES (?, ?, ?, ?, ?)',
               (guia, estado, descripcion, ubicacion, _ahora()))
    db.execute('UPDATE envios SET estado = ? WHERE guia = ?', (estado, guia))
    db.commit()


def guardar_mensaje(nombre, correo, mensaje):
    db = get_db()
    db.execute('INSERT INTO mensajes (nombre, correo, mensaje, creado_en) VALUES (?, ?, ?, ?)',
               (nombre, correo, mensaje, _ahora()))
    db.commit()


# ---------------------------------------------------------------------------
# Asistente
# ---------------------------------------------------------------------------

def nuevo_folio(prefijo, tabla):
    db = get_db()
    while True:
        folio = f'{prefijo}-{secrets.token_hex(3).upper()}'
        if not db.execute(f'SELECT 1 FROM {tabla} WHERE folio = ?', (folio,)).fetchone():
            return folio


def crear_conversacion(conversacion_id, canal):
    ahora = _ahora()
    db = get_db()
    db.execute('INSERT INTO conversaciones (id, canal, creada, actualizada) VALUES (?, ?, ?, ?)',
               (conversacion_id, canal, ahora, ahora))
    db.commit()


def obtener_conversacion(conversacion_id):
    fila = get_db().execute('SELECT * FROM conversaciones WHERE id = ?', (conversacion_id,)).fetchone()
    return dict(fila) if fila else None


def agregar_chat_mensaje(conversacion_id, rol, texto):
    db = get_db()
    db.execute('INSERT INTO chat_mensajes (conversacion_id, rol, texto, fecha) VALUES (?, ?, ?, ?)',
               (conversacion_id, rol, texto, _ahora()))
    db.execute('UPDATE conversaciones SET actualizada = ? WHERE id = ?', (_ahora(), conversacion_id))
    db.commit()


def chat_mensajes(conversacion_id, roles=('cliente', 'asistente'), limite=None):
    marcadores = ','.join('?' * len(roles))
    filas = get_db().execute(
        f'SELECT rol, texto, fecha FROM chat_mensajes WHERE conversacion_id = ? AND rol IN ({marcadores}) '
        f'ORDER BY id DESC' + (' LIMIT ?' if limite else ''),
        (conversacion_id, *roles, *([limite] if limite else []))).fetchall()
    return [dict(f) for f in reversed(filas)]


def sumar_uso_conversacion(conversacion_id, *, entrada, salida, cache_lectura, cache_escritura, costo, turno):
    db = get_db()
    db.execute(
        'UPDATE conversaciones SET llamadas_api = llamadas_api + 1, tokens_entrada = tokens_entrada + ?, '
        'tokens_salida = tokens_salida + ?, tokens_cache_lectura = tokens_cache_lectura + ?, '
        'tokens_cache_escritura = tokens_cache_escritura + ?, costo_usd = costo_usd + ?, turnos = turnos + ? '
        'WHERE id = ?',
        (entrada, salida, cache_lectura, cache_escritura, costo, 1 if turno else 0, conversacion_id))
    db.commit()


def marcar_conversacion(conversacion_id, estado):
    db = get_db()
    db.execute('UPDATE conversaciones SET estado = ? WHERE id = ?', (estado, conversacion_id))
    db.commit()


def registrar_queja(*, conversacion_id, guia, tipo, descripcion, nombre_cliente, telefono):
    db = get_db()
    folio = nuevo_folio('QJ', 'quejas')
    db.execute('INSERT INTO quejas (folio, conversacion_id, guia, tipo, descripcion, nombre_cliente, telefono, creada) '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
               (folio, conversacion_id, guia or None, tipo, descripcion, nombre_cliente or None, telefono or None, _ahora()))
    db.commit()
    return folio


def registrar_escalamiento(*, conversacion_id, motivo, resumen, telefono):
    db = get_db()
    folio = nuevo_folio('ESC', 'escalamientos')
    db.execute('INSERT INTO escalamientos (folio, conversacion_id, motivo, resumen, telefono, creado) '
               'VALUES (?, ?, ?, ?, ?, ?)', (folio, conversacion_id, motivo, resumen, telefono or None, _ahora()))
    db.execute("UPDATE conversaciones SET estado = 'escalada' WHERE id = ?", (conversacion_id,))
    db.commit()
    return folio


def resumen_costos_asistente():
    fila = get_db().execute(
        'SELECT COUNT(*) AS conversaciones, COALESCE(SUM(turnos), 0) AS turnos, '
        'COALESCE(SUM(llamadas_api), 0) AS llamadas, COALESCE(SUM(tokens_entrada), 0) AS entrada, '
        'COALESCE(SUM(tokens_salida), 0) AS salida, COALESCE(SUM(tokens_cache_lectura), 0) AS cache_lectura, '
        'COALESCE(SUM(tokens_cache_escritura), 0) AS cache_escritura, COALESCE(SUM(costo_usd), 0) AS costo '
        'FROM conversaciones WHERE llamadas_api > 0').fetchone()
    return dict(fila)
