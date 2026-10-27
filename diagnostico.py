"""
Diagnóstico de memoria (solo usuario Dios): qué está guardando Streamlit y en qué cantidad.
Usa las estadísticas internas de Streamlit (memoria de corto plazo, archivos subidos, archivos para descargar,
datos de cada sesión) y el resultado de las tareas en segundo plano.
"""
import gc
import pickle
import sys

import pandas as pd
import streamlit as st

NOMBRES = {
    "st_cache_data": "Memoria de corto plazo (consultas)",
    "st_cache_resource": "Recursos (conexión a Supabase)",
    "st_session_state": "Datos de las sesiones abiertas",
    "st_uploaded_file_manager": "Archivos subidos (fotos, PDFs)",
    "UploadedFileManager": "Archivos subidos (fotos, PDFs)",
    "st_media_file_manager": "Imágenes y archivos para descargar",
    "MediaFileManager": "Imágenes y archivos para descargar",
    "ForwardMessageCache": "Pantallas ya enviadas a los navegadores",
    "st_forward_message_cache": "Pantallas ya enviadas a los navegadores",
}


def _tam(obj):
    try:
        return len(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        return 0


def estadisticas():
    filas = []
    try:
        from streamlit.runtime import get_instance
        crudo = get_instance().stats_mgr.get_stats()
        lista = [x for grupo in crudo.values() for x in grupo] if isinstance(crudo, dict) else list(crudo)
        for s in lista:
            filas.append({"Qué": NOMBRES.get(s.category_name, s.category_name), "Detalle": str(s.cache_name or "")[-60:],
                          "MB": s.byte_length / 1048576})
    except Exception as ex:
        filas.append({"Qué": f"(no disponible: {ex})", "Detalle": "", "MB": 0.0})
    # Resultados de las tareas en segundo plano
    for mod in ("disponibilidad", "shopify_lista", "shopify_ventas", "shopify_productos", "cotizaciones"):
        m = sys.modules.get(mod)
        for nombre in ("_TAREA", "TAREA_VENCIMIENTO"):
            t = getattr(m, nombre, None) if m else None
            if t is not None:
                filas.append({"Qué": "Tarea en segundo plano", "Detalle": mod, "MB": _tam(t._resultado) / 1048576})
    return pd.DataFrame(filas)


def render():
    with st.expander("Diagnóstico de memoria"):
        df = estadisticas()
        if df.empty:
            st.caption("Sin datos.")
            return
        resumen = df.groupby("Qué", as_index=False)["MB"].sum().sort_values("MB", ascending=False)
        st.dataframe(resumen.style.format({"MB": "{:,.1f}"}), hide_index=True, use_container_width=True)
        st.caption(f"Total medido: {resumen['MB'].sum():,.0f} MB. El resto es Python, librerías y memoria aún no "
                   "devuelta al servidor.")
        top = df.sort_values("MB", ascending=False).head(12)
        st.dataframe(top.style.format({"MB": "{:,.2f}"}), hide_index=True, use_container_width=True)
        c1, c2 = st.columns(2)
        if c1.button("Liberar memoria", key="diag_liberar", help="Borra la memoria de corto plazo y devuelve al "
                     "servidor la memoria libre. No borra datos ni cierra sesiones."):
            st.cache_data.clear()
            try:
                from segundo_plano import liberar_memoria
                liberar_memoria()
            except Exception:
                gc.collect()
            st.rerun()
        c2.caption(f"Objetos de Python: {len(gc.get_objects()):,}")
