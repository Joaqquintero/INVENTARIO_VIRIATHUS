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
import os
import time

import streamlit as st
import streamlit.components.v1 as components

COOKIE = "viriathus_sesion"
# Componente chiquito que guarda / lee la llave en el almacenamiento del navegador (localStorage).
# Es más confiable que la cookie: en Streamlit Cloud la cookie puede no llegar a la app.
_almacen = components.declare_component(
    "sesion_almacen", path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sesion_componente"))
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
    """Deja la llave en el navegador. Se llama en CADA recarga mientras la sesión está abierta (la llave se arma
    una sola vez por sesión, así el componente no cambia): si una recarga rápida se lo «come», la siguiente la deja."""
    ss = st.session_state
    if ss.get("_llave_de") != usuario:
        ss["_llave"], ss["_llave_de"] = crear_llave(usuario, password), usuario
    _almacen(accion="guardar", llave=ss["_llave"], key="ses_guardar", default=None)
    components.html(_cookie_js(ss["_llave"], DURACION), height=0)


def cookies_disponibles():
    """False si este Streamlit es viejo (antes de 1.37) y no puede leer la llave."""
    try:
        st.context.cookies
        return True
    except Exception:
        return False


def olvidar():
    st.session_state.pop("_llave", None)
    st.session_state.pop("_llave_de", None)
    _almacen(accion="borrar", key="ses_borrar", default=None)
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
        # La llave del almacenamiento del navegador: la primera vuelta devuelve None (el componente todavía no
        # contestó); cuando contesta, Streamlit recarga solo y aquí llega la llave.
        llave = _almacen(accion="leer", key="ses_leer", default=None)
        usuario = usuario_de_llave(llave, usuarios) if llave else None
    if not usuario:
        return False
    ss["logged_in"] = True
    ss["login_usuario"] = usuario
    ss["user_info"] = usuarios[usuario]
    st.rerun()                                         # entra ya, sin mostrar la pantalla de inicio
