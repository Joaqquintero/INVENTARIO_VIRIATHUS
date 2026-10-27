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
            if not hasattr(s, "byte_length"):
                continue                      # contadores que no son memoria
            filas.append({"Qué": NOMBRES.get(s.category_name, s.category_name),
                          "Detalle": str(getattr(s, "cache_name", "") or "")[-60:], "MB": s.byte_length / 1048576})
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


def memoria_malloc():
    """(MB en uso real, MB libres pero no devueltos al servidor) según el sistema (solo Linux)."""
    import ctypes

    class MallInfo2(ctypes.Structure):
        _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                                     "fsmblks", "uordblks", "fordblks", "keepcost")]
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallinfo2.restype = MallInfo2
        mi = libc.mallinfo2()
        return (mi.uordblks + mi.hblkhd) / 1048576, mi.fordblks / 1048576
    except Exception:
        return None, None


def tipos_de_objetos(n=10):
    """Los tipos de objetos de Python más numerosos (cantidad y tamaño aproximado)."""
    cuenta, tam = {}, {}
    for o in gc.get_objects():
        t = type(o).__name__
        cuenta[t] = cuenta.get(t, 0) + 1
        try:
            tam[t] = tam.get(t, 0) + sys.getsizeof(o)
        except Exception:
            pass
    top = sorted(cuenta, key=lambda k: tam.get(k, 0), reverse=True)[:n]
    return pd.DataFrame([{"Tipo": t, "Cantidad": cuenta[t], "MB": tam.get(t, 0) / 1048576} for t in top])


def _rastreador():
    import tracemalloc
    st.markdown("**Rastreador** (qué parte del código guarda memoria)")
    if not tracemalloc.is_tracing():
        st.caption("1) Enciéndelo. 2) Usen la app normal 15–30 min. 3) Vuelve y toca «Ver qué creció». "
                   "Mientras está encendido la app va un poco más lenta.")
        if st.button("Encender rastreador", key="diag_tm_on"):
            tracemalloc.start(10)
            st.session_state["_diag_snap"] = None
            import diagnostico as _d
            _d._BASE = tracemalloc.take_snapshot()
            st.rerun()
        return
    c1, c2 = st.columns(2)
    if c1.button("Ver qué creció", key="diag_tm_ver"):
        import diagnostico as _d
        actual = tracemalloc.take_snapshot()
        base = getattr(_d, "_BASE", None)
        stats = actual.compare_to(base, "traceback") if base else actual.statistics("traceback")
        filas = []
        for stt in stats[:12]:
            fr = [f for f in stt.traceback if "/site-packages/" not in f.filename and "/lib/python" not in f.filename]
            lugar = fr[-1] if fr else stt.traceback[-1]
            filas.append({"Dónde": f"{lugar.filename.split('/')[-1]}:{lugar.lineno}",
                          "Creció MB": getattr(stt, "size_diff", stt.size) / 1048576,
                          "Total MB": stt.size / 1048576})
        st.session_state["_diag_crecio"] = filas
    if c2.button("Apagar rastreador", key="diag_tm_off"):
        tracemalloc.stop()
        st.session_state.pop("_diag_crecio", None)
        st.rerun()
    if st.session_state.get("_diag_crecio"):
        st.dataframe(pd.DataFrame(st.session_state["_diag_crecio"]).style.format({"Creció MB": "{:,.1f}", "Total MB": "{:,.1f}"}),
                     hide_index=True, use_container_width=True)
        st.caption("Mándale una captura de esta tabla a quien mantiene la app.")


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
        en_uso, libre = memoria_malloc()
        if en_uso is not None:
            st.markdown(f"**En uso de verdad:** {en_uso:,.0f} MB · **Libre pero no devuelto:** {libre:,.0f} MB")
        if st.checkbox("Ver tipos de objetos", key="diag_tipos"):
            st.dataframe(tipos_de_objetos().style.format({"MB": "{:,.1f}"}), hide_index=True, use_container_width=True)
        _rastreador()
