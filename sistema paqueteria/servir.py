"""Servidor de producción (Waitress) para publicar con Cloudflare Tunnel.

    python servir.py

- Aplica migraciones pendientes y luego sirve la app en 127.0.0.1:5001 (solo local: el túnel de
  Cloudflare es quien la expone a internet; nadie de tu red puede entrar directo).
- Waitress funciona bien en Windows y aguanta varias personas a la vez (a diferencia del servidor
  de desarrollo de Flask).
"""
import os

from waitress import serve

import db
from app import app

if __name__ == '__main__':
    db.preparar_base(app)
    puerto = int(os.getenv('PUERTO', '5001'))
    print(f'Balula Express sirviendo en http://127.0.0.1:{puerto}  (URL pública: {app.config["PUBLIC_BASE_URL"] or "no configurada"})')
    # Waitress descarta las cabeceras X-Forwarded-* salvo que vengan de un proxy de confianza.
    # cloudflared se conecta desde 127.0.0.1, así que solo confiamos en esa dirección (un salto).
    serve(app, host='127.0.0.1', port=puerto, threads=8,
          trusted_proxy='127.0.0.1', trusted_proxy_count=1,
          trusted_proxy_headers={'x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host'},
          clear_untrusted_proxy_headers=True)
