"""Inicio de sesión para repartidores y administradores."""
import re
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

import db

bp = Blueprint('auth', __name__)

RE_PASSWORD = re.compile(r'^(?=.*[A-Za-z])(?=.*\d).{10,128}$')
DURACION_SESION = timedelta(hours=12)


def destino_por_rol(usuario):
    return url_for('admin.panel') if usuario.rol == 'admin' else url_for('repartidor.hoy')


@bp.before_app_request
def cargar_usuario():
    g.usuario = None
    uid = session.get('usuario_id')
    if uid:
        u = db.obtener_usuario(uid)
        if u and u.activo:
            g.usuario = u
        else:
            session.pop('usuario_id', None)


@bp.app_context_processor
def usuario_en_plantillas():
    return {'usuario_actual': g.get('usuario')}


def login_requerido(*roles):
    """@login_requerido() para cualquier usuario; @login_requerido('admin') para un rol."""
    def decorador(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            u = g.get('usuario')
            if not u:
                if request.path.startswith('/api/'):
                    return {'ok': False, 'error': 'Inicia sesión.'}, 401
                return redirect(url_for('auth.login', siguiente=request.full_path.rstrip('?')))
            if u.debe_cambiar_password and request.endpoint != 'auth.cambiar_password':
                flash('Por seguridad, cambia tu contraseña temporal.', 'warning')
                return redirect(url_for('auth.cambiar_password'))
            if roles and u.rol not in roles:
                abort(403)
            return vista(*args, **kwargs)
        return envoltura
    return decorador


def _siguiente_seguro(valor):
    """Solo redirige a rutas internas (evita redirecciones abiertas)."""
    if not valor:
        return None
    url = urlparse(valor)
    if url.scheme or url.netloc or not valor.startswith('/') or valor.startswith('//'):
        return None
    return valor


@bp.route('/iniciar-sesion', methods=['GET', 'POST'], endpoint='login')
def login():
    if g.get('usuario'):
        return redirect(destino_por_rol(g.usuario))
    correo = ''
    if request.method == 'POST':
        correo = request.form.get('correo', '').strip()[:160]
        usuario, error = db.autenticar(correo, request.form.get('password', '')[:128])
        if error:
            flash(error, 'danger')
            return render_template('auth/login.html', correo=correo), 401
        csrf = session.get('_csrf')
        session.clear()  # evita fijación de sesión
        if csrf:
            session['_csrf'] = csrf
        session.permanent = True
        session['usuario_id'] = usuario.id
        if usuario.debe_cambiar_password:
            return redirect(url_for('auth.cambiar_password'))
        return redirect(_siguiente_seguro(request.args.get('siguiente')) or destino_por_rol(usuario))
    return render_template('auth/login.html', correo=correo)


@bp.route('/cerrar-sesion', methods=['POST'])
def logout():
    session.pop('usuario_id', None)
    flash('Cerraste sesión.', 'success')
    return redirect(url_for('auth.login'))


@bp.route('/cambiar-contrasena', methods=['GET', 'POST'], endpoint='cambiar_password')
@login_requerido()
def cambiar_password():
    if request.method == 'POST':
        actual = request.form.get('actual', '')
        nueva = request.form.get('nueva', '')
        if not g.usuario.verificar_password(actual):
            flash('Tu contraseña actual no es correcta.', 'danger')
        elif not RE_PASSWORD.match(nueva):
            flash('La nueva contraseña debe tener al menos 10 caracteres, con letras y números.', 'danger')
        elif nueva != request.form.get('confirmar', ''):
            flash('Las contraseñas no coinciden.', 'danger')
        elif nueva == actual:
            flash('La nueva contraseña debe ser distinta a la actual.', 'danger')
        else:
            g.usuario.fijar_password(nueva)
            g.usuario.debe_cambiar_password = False
            db.sa.session.commit()
            flash('Contraseña actualizada.', 'success')
            return redirect(destino_por_rol(g.usuario))
    return render_template('auth/cambiar_password.html')


@bp.route('/olvide-contrasena')
def olvide():
    return render_template('auth/olvide.html')
