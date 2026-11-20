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
