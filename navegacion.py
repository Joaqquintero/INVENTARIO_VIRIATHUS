"""
Navegación por secciones: solo se carga la sección elegida (con st.tabs se cargaban todas en cada clic).
"""
import streamlit as st

# PDFs y archivos pesados que se guardan en la sesión: al cambiar de sección se borran (se pueden volver a generar).
# Así la memoria de la app no crece con cada computadora que la tiene abierta todo el día.
_PESADOS_PREFIJOS = ("ctg_pdf_",)
_PESADOS = ("et_pdf", "ct_ultimo", "vt_ultimo", "vp_pdf", "rm_pdf")


def liberar_pesados():
    ss = st.session_state
    borrados = False
    for k in list(ss.keys()):
        if k in _PESADOS or any(str(k).startswith(p) for p in _PESADOS_PREFIJOS):
            ss.pop(k, None)
            borrados = True
    if borrados:
        try:
            from segundo_plano import liberar_memoria
            liberar_memoria()
        except Exception:
            pass


def selector(opciones, key, etiqueta="Sección"):
    """Botones de sección (segmented control, o radio horizontal en Streamlit viejo). Devuelve la elegida."""
    ss = st.session_state
    opciones = list(opciones)
    ultimo = ss.get(f"{key}__ultimo")
    if ultimo not in opciones:
        ultimo = opciones[0]
    if ss.get(key) not in opciones:          # primera vez, o se deseleccionó: vuelve a la última
        ss[key] = ultimo
    seg = getattr(st, "segmented_control", None)
    if seg is not None:
        valor = seg(etiqueta, opciones, key=key, label_visibility="collapsed")
    else:
        valor = st.radio(etiqueta, opciones, key=key, horizontal=True, label_visibility="collapsed")
    if valor not in opciones:
        valor = ultimo
    if valor != ultimo:
        liberar_pesados()          # se cambió de sección: fuera los PDFs que ya no se ven
    ss[f"{key}__ultimo"] = valor
    return valor
