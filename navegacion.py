"""
Navegación por secciones: solo se carga la sección elegida (con st.tabs se cargaban todas en cada clic).
"""
import streamlit as st


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
    ss[f"{key}__ultimo"] = valor
    return valor
