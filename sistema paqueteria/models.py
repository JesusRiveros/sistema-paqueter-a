"""Modelos de datos (SQLAlchemy). En desarrollo usa SQLite; en producción basta con poner
DATABASE_URL=postgresql+psycopg://usuario:clave@host/balula en el .env.

Cambios de esquema: edita este archivo y luego
    flask --app app db migrate -m "describe el cambio"
    flask --app app db upgrade
"""
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

sa = SQLAlchemy()


def ahora():
    return datetime.now().replace(microsecond=0)


class Usuario(sa.Model):
    __tablename__ = 'usuarios'
    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    correo: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    rol: Mapped[str] = mapped_column(String(20))  # admin | repartidor
    telefono: Mapped[str | None] = mapped_column(String(20))
    zona: Mapped[str | None] = mapped_column(String(80))  # zona de reparto preferente
    activo: Mapped[bool] = mapped_column(default=True)
    debe_cambiar_password: Mapped[bool] = mapped_column(default=False)
    intentos_fallidos: Mapped[int] = mapped_column(default=0)
    bloqueado_hasta: Mapped[datetime | None]
    ultimo_acceso: Mapped[datetime | None]
    creado: Mapped[datetime] = mapped_column(default=ahora)

    envios: Mapped[list['Envio']] = relationship(back_populates='repartidor')

    def fijar_password(self, password):
        self.password_hash = generate_password_hash(password)

    def verificar_password(self, password):
        return check_password_hash(self.password_hash, password)


class Envio(sa.Model):
    __tablename__ = 'envios'
    guia: Mapped[str] = mapped_column(String(20), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(default=ahora)
    estado: Mapped[str] = mapped_column(String(20), default='creado', index=True)
    remitente: Mapped[dict] = mapped_column(JSON)
    destinatario: Mapped[dict] = mapped_column(JSON)
    cotizacion: Mapped[dict] = mapped_column(JSON)
    tipo_entrega: Mapped[str] = mapped_column(String(30))
    correo_destinatario: Mapped[str | None] = mapped_column(String(160))
    total: Mapped[float]
    # Logística
    origen_lat: Mapped[float | None]
    origen_lng: Mapped[float | None]
    destino_lat: Mapped[float | None]
    destino_lng: Mapped[float | None]
    repartidor_id: Mapped[int | None] = mapped_column(ForeignKey('usuarios.id'), index=True)
    asignado_en: Mapped[datetime | None]
    orden_ruta: Mapped[int | None]
    codigo_entrega: Mapped[str] = mapped_column(String(4))  # el destinatario se lo dicta al repartidor
    # Preferencias del destinatario (reprogramación)
    fecha_preferida: Mapped[str | None] = mapped_column(String(10))
    instruccion_entrega: Mapped[str | None] = mapped_column(String(30))  # domicilio | vecino | sucursal
    nota_entrega: Mapped[str | None] = mapped_column(String(300))
    lote_folio: Mapped[str | None] = mapped_column(ForeignKey('lotes.folio'), index=True)

    repartidor: Mapped[Usuario | None] = relationship(back_populates='envios')
    eventos: Mapped[list['Evento']] = relationship(back_populates='envio', order_by='Evento.id',
                                                    cascade='all, delete-orphan')


class Lote(sa.Model):
    """Carga masiva de envíos de una empresa (CSV/Excel)."""
    __tablename__ = 'lotes'
    folio: Mapped[str] = mapped_column(String(20), primary_key=True)
    empresa: Mapped[str] = mapped_column(String(160))
    rfc: Mapped[str | None] = mapped_column(String(13))
    correo: Mapped[str] = mapped_column(String(160))
    remitente: Mapped[dict] = mapped_column(JSON)
    renglones: Mapped[list] = mapped_column(JSON)   # vista previa: datos, cotización o error por renglón
    compensar_co2: Mapped[bool] = mapped_column(default=False)
    descuento_pct: Mapped[float] = mapped_column(default=0.0)
    total: Mapped[float] = mapped_column(default=0.0)
    estado: Mapped[str] = mapped_column(String(20), default='borrador')  # borrador | pagado
    creado: Mapped[datetime] = mapped_column(default=ahora)


class Evento(sa.Model):
    __tablename__ = 'eventos'
    id: Mapped[int] = mapped_column(primary_key=True)
    guia: Mapped[str] = mapped_column(ForeignKey('envios.guia'), index=True)
    estado: Mapped[str] = mapped_column(String(20))
    descripcion: Mapped[str] = mapped_column(String(300))
    ubicacion: Mapped[str | None] = mapped_column(String(200))
    fecha: Mapped[datetime] = mapped_column(default=ahora)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey('usuarios.id'))
    lat: Mapped[float | None]
    lng: Mapped[float | None]

    envio: Mapped[Envio] = relationship(back_populates='eventos')


class PruebaEntrega(sa.Model):
    __tablename__ = 'pruebas_entrega'
    id: Mapped[int] = mapped_column(primary_key=True)
    guia: Mapped[str] = mapped_column(ForeignKey('envios.guia'), unique=True)
    repartidor_id: Mapped[int] = mapped_column(ForeignKey('usuarios.id'))
    receptor_nombre: Mapped[str] = mapped_column(String(120))
    relacion: Mapped[str] = mapped_column(String(30))  # destinatario | familiar | vecino | recepcion
    firma_png: Mapped[str | None] = mapped_column(Text)  # data URL
    foto_archivo: Mapped[str | None] = mapped_column(String(200))
    lat: Mapped[float | None]
    lng: Mapped[float | None]
    fecha: Mapped[datetime] = mapped_column(default=ahora)


class Incidencia(sa.Model):
    __tablename__ = 'incidencias'
    id: Mapped[int] = mapped_column(primary_key=True)
    guia: Mapped[str] = mapped_column(ForeignKey('envios.guia'), index=True)
    repartidor_id: Mapped[int | None] = mapped_column(ForeignKey('usuarios.id'))
    tipo: Mapped[str] = mapped_column(String(30))  # ausente | direccion_incorrecta | rechazado | danado | otro
    nota: Mapped[str | None] = mapped_column(String(500))
    lat: Mapped[float | None]
    lng: Mapped[float | None]
    fecha: Mapped[datetime] = mapped_column(default=ahora)
    resuelta: Mapped[bool] = mapped_column(default=False)


class UbicacionRepartidor(sa.Model):
    """Última posición conocida (rastreo en vivo)."""
    __tablename__ = 'ubicaciones_repartidor'
    repartidor_id: Mapped[int] = mapped_column(ForeignKey('usuarios.id'), primary_key=True)
    lat: Mapped[float]
    lng: Mapped[float]
    precision_m: Mapped[float | None]
    fecha: Mapped[datetime] = mapped_column(default=ahora)


class MensajeContacto(sa.Model):
    __tablename__ = 'mensajes'
    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    correo: Mapped[str] = mapped_column(String(160))
    mensaje: Mapped[str] = mapped_column(Text)
    creado_en: Mapped[datetime] = mapped_column(default=ahora)
    atendido: Mapped[bool] = mapped_column(default=False)


class Notificacion(sa.Model):
    """Avisos al cliente (correo). Se registran siempre; se envían si hay SMTP configurado."""
    __tablename__ = 'notificaciones'
    id: Mapped[int] = mapped_column(primary_key=True)
    guia: Mapped[str] = mapped_column(ForeignKey('envios.guia'), index=True)
    para: Mapped[str] = mapped_column(String(160))
    asunto: Mapped[str] = mapped_column(String(200))
    cuerpo: Mapped[str] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20))  # enviado | registrado | error
    fecha: Mapped[datetime] = mapped_column(default=ahora)


# ---------------------------------------------------------------- Asistente

class Conversacion(sa.Model):
    __tablename__ = 'conversaciones'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    canal: Mapped[str] = mapped_column(String(20))
    creada: Mapped[datetime] = mapped_column(default=ahora)
    actualizada: Mapped[datetime] = mapped_column(default=ahora)
    estado: Mapped[str] = mapped_column(String(20), default='abierta')
    turnos: Mapped[int] = mapped_column(default=0)
    llamadas_api: Mapped[int] = mapped_column(default=0)
    tokens_entrada: Mapped[int] = mapped_column(default=0)
    tokens_salida: Mapped[int] = mapped_column(default=0)
    tokens_cache_lectura: Mapped[int] = mapped_column(default=0)
    tokens_cache_escritura: Mapped[int] = mapped_column(default=0)
    costo_usd: Mapped[float] = mapped_column(default=0.0)
    fallos_verificacion: Mapped[int] = mapped_column(default=0)


class ChatMensaje(sa.Model):
    __tablename__ = 'chat_mensajes'
    id: Mapped[int] = mapped_column(primary_key=True)
    conversacion_id: Mapped[str] = mapped_column(ForeignKey('conversaciones.id'))
    rol: Mapped[str] = mapped_column(String(20))
    texto: Mapped[str] = mapped_column(Text)
    fecha: Mapped[datetime] = mapped_column(default=ahora)

    __table_args__ = (Index('idx_chat_conv', 'conversacion_id', 'id'),)


class Queja(sa.Model):
    __tablename__ = 'quejas'
    id: Mapped[int] = mapped_column(primary_key=True)
    folio: Mapped[str] = mapped_column(String(20), unique=True)
    conversacion_id: Mapped[str | None] = mapped_column(ForeignKey('conversaciones.id'))
    guia: Mapped[str | None] = mapped_column(String(20))
    tipo: Mapped[str] = mapped_column(String(30))
    descripcion: Mapped[str] = mapped_column(Text)
    nombre_cliente: Mapped[str | None] = mapped_column(String(120))
    telefono: Mapped[str | None] = mapped_column(String(20))
    estado: Mapped[str] = mapped_column(String(20), default='abierta')
    creada: Mapped[datetime] = mapped_column(default=ahora)
    repartidor_id: Mapped[int | None] = mapped_column(ForeignKey('usuarios.id'), index=True)


class Escalamiento(sa.Model):
    __tablename__ = 'escalamientos'
    id: Mapped[int] = mapped_column(primary_key=True)
    folio: Mapped[str] = mapped_column(String(20), unique=True)
    conversacion_id: Mapped[str | None] = mapped_column(ForeignKey('conversaciones.id'))
    motivo: Mapped[str] = mapped_column(String(30))
    resumen: Mapped[str] = mapped_column(Text)
    telefono: Mapped[str | None] = mapped_column(String(20))
    estado: Mapped[str] = mapped_column(String(20), default='pendiente')
    creado: Mapped[datetime] = mapped_column(default=ahora)


# ---------------------------------------------------------------- Reembolsos y compensaciones

class Configuracion(sa.Model):
    """Valores de negocio editables desde el admin (políticas de reembolso, etc.)."""
    __tablename__ = 'configuracion'
    clave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[dict] = mapped_column(JSON)  # {"v": valor}
    actualizado: Mapped[datetime] = mapped_column(default=ahora)
    actualizado_por: Mapped[str | None] = mapped_column(String(160))


class Evidencia(sa.Model):
    """Foto que sube el cliente (p. ej. daño del paquete) mediante un enlace con token."""
    __tablename__ = 'evidencias'
    token: Mapped[str] = mapped_column(String(40), primary_key=True)
    guia: Mapped[str] = mapped_column(ForeignKey('envios.guia'), index=True)
    conversacion_id: Mapped[str | None] = mapped_column(ForeignKey('conversaciones.id'))
    archivo: Mapped[str | None] = mapped_column(String(200))
    creada: Mapped[datetime] = mapped_column(default=ahora)
    subida: Mapped[datetime | None]


class Caso(sa.Model):
    """Reembolso o compensación (propuesto, aprobado automáticamente, pendiente o resuelto)."""
    __tablename__ = 'casos'
    id: Mapped[int] = mapped_column(primary_key=True)
    folio: Mapped[str] = mapped_column(String(20), unique=True)
    guia: Mapped[str] = mapped_column(ForeignKey('envios.guia'), index=True)
    conversacion_id: Mapped[str | None] = mapped_column(ForeignKey('conversaciones.id'))
    cliente_telefono: Mapped[str] = mapped_column(String(10), index=True)
    cliente_rol: Mapped[str] = mapped_column(String(20))          # remitente | destinatario
    cliente_correo: Mapped[str | None] = mapped_column(String(160), index=True)  # correo REGISTRADO (no el que dicta en el chat)
    otp_hash: Mapped[str | None] = mapped_column(String(64))       # código de 6 dígitos (solo se guarda su hash)
    otp_expira: Mapped[datetime | None]
    otp_intentos: Mapped[int] = mapped_column(default=0)
    correo_contacto: Mapped[str | None] = mapped_column(String(160))
    problema: Mapped[str] = mapped_column(String(40))
    detalle: Mapped[str] = mapped_column(Text)
    opciones: Mapped[list] = mapped_column(JSON)                  # lo que se le ofreció al cliente
    resolucion: Mapped[str | None] = mapped_column(String(30))    # reembolso | cupon | reenvio | revision
    monto: Mapped[float] = mapped_column(default=0.0)             # MXN a devolver
    cupon_codigo: Mapped[str | None] = mapped_column(String(20))
    estado: Mapped[str] = mapped_column(String(30), default='propuesto')
    # propuesto | aprobado_auto | pendiente | aprobado_admin | rechazado | descartado
    motivo_estado: Mapped[str | None] = mapped_column(Text)       # por qué quedó así (reglas aplicadas)
    turno_propuesta: Mapped[int] = mapped_column(default=0)       # para exigir confirmación en un turno posterior
    repartidor_id: Mapped[int | None] = mapped_column(ForeignKey('usuarios.id'))
    reenvio_guia: Mapped[str | None] = mapped_column(String(20))
    transcripcion: Mapped[str | None] = mapped_column(Text)        # copia de la conversación al decidir
    creado: Mapped[datetime] = mapped_column(default=ahora)
    resuelto: Mapped[datetime | None]
    resuelto_por: Mapped[str | None] = mapped_column(String(160))
    nota_admin: Mapped[str | None] = mapped_column(Text)


class Cupon(sa.Model):
    __tablename__ = 'cupones'
    codigo: Mapped[str] = mapped_column(String(20), primary_key=True)
    porcentaje: Mapped[int]
    caso_id: Mapped[int | None] = mapped_column(ForeignKey('casos.id'))
    creado: Mapped[datetime] = mapped_column(default=ahora)
    vence: Mapped[datetime]
    usado_en_guia: Mapped[str | None] = mapped_column(String(20))
    usado: Mapped[datetime | None]
