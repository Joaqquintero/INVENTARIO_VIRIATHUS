"""
Sesión que no se cierra sola.

Streamlit guarda el inicio de sesión solo en la memoria de la pestaña: si la compu o el celular se suspenden, o
pasa mucho rato, al reconectar empieza de cero y vuelve a pedir usuario y contraseña. Aquí, al iniciar sesión se
deja en el navegador una «llave» firmada (cookie) que dura un año; al volver a abrir la app se entra solo.

  * Se cierra únicamente con «Cerrar Sesión» (borra la llave de ese navegador).
  * Si cambias la contraseña de un usuario en los secretos, sus llaves dejan de servir (se cierra en todos lados).
  * Para cerrar TODAS las sesiones de todos: agrega o cambia SESION_SECRETO en los secretos de la app.
"""
import base64
import hashlib
import hmac
import time

import streamlit as st
import streamlit.components.v1 as components

COOKIE = "viriathus_sesion"
DURACION = 365 * 24 * 3600          # un año


def _clave():
    s = ""
    try:
        s = str(st.secrets.get("SESION_SECRETO", "") or "")
        if not s:
            s = str(st.secrets.get("SUPABASE_KEY", "") or st.secrets.get("supabase_key", "") or "")
    except Exception:
        pass
    return hashlib.sha256(("viriathus|" + s).encode()).digest()


def _firma(usuario, vence, password):
    msg = f"{usuario}|{vence}|{hashlib.sha256(password.encode()).hexdigest()}".encode()
    return hmac.new(_clave(), msg, hashlib.sha256).hexdigest()[:40]


def crear_llave(usuario, password):
    vence = int(time.time()) + DURACION
    crudo = f"{usuario}|{vence}|{_firma(usuario, vence, password)}"
    return base64.urlsafe_b64encode(crudo.encode()).decode().rstrip("=")


def usuario_de_llave(llave, usuarios):
    """El usuario si la llave es válida (firma correcta, no vencida, usuario y contraseña vigentes); si no, None."""
    try:
        crudo = base64.urlsafe_b64decode(llave + "=" * (-len(llave) % 4)).decode()
        usuario, vence, firma = crudo.rsplit("|", 2)
        if int(vence) < time.time() or usuario not in usuarios:
            return None
        if hmac.compare_digest(firma, _firma(usuario, int(vence), usuarios[usuario]["password"])):
            return usuario
    except Exception:
        pass
    return None


def _cookie_js(valor, max_age):
    # El cuadro de components.html es del mismo sitio que la app: puede escribir la cookie de la página.
    return f"""<script>
const v = "{COOKIE}={valor}; max-age={max_age}; path=/; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
try {{ window.parent.document.cookie = v; }} catch (e) {{}}
try {{ document.cookie = v; }} catch (e) {{}}
</script>"""


def recordar(usuario, password):
    components.html(_cookie_js(crear_llave(usuario, password), DURACION), height=0)


def olvidar():
    components.html(_cookie_js("", 0), height=0)


def entrar_solo(usuarios):
    """Si el navegador trae una llave válida, inicia la sesión sin pedir contraseña. Devuelve True si entró."""
    ss = st.session_state
    if ss.get("logged_in") or ss.get("_sesion_cerrada"):
        return False
    try:
        llave = st.context.cookies.get(COOKIE)          # Streamlit 1.37+
    except Exception:
        llave = None
    usuario = usuario_de_llave(llave, usuarios) if llave else None
    if not usuario:
        return False
    ss["logged_in"] = True
    ss["login_usuario"] = usuario
    ss["user_info"] = usuarios[usuario]
    return True
