"""
Bitácora de memoria: anota cuánto sube (o baja) la memoria de la app con cada actividad.

  * Cada tarea en segundo plano (revisiones de Shopify, vencimiento de cotizaciones) queda anotada sola.
  * Cada vez que alguien usa una sección de la app (Buscar, Ventas › Pedidos, …) también.
  * El usuario Dios la ve en la barra lateral, debajo del medidor de memoria.

La bitácora vive en memoria (se reinicia con el reboot) y es compartida por todas las sesiones.
"""
import collections
import datetime as dt
import threading

_LOCK = threading.Lock()
_REGISTROS = collections.deque(maxlen=200)      # los movimientos notables más recientes
_RESUMEN = {}                                    # actividad -> {veces, subio, max}
_INICIO = {"hora": dt.datetime.now(), "mb": None}
NOTABLE_MB = 3.0                                 # solo se listan los movimientos de 3 MB o más

TAREAS = {
    "shopify_ventas": "Shopify: ventas en línea (segundo plano)",
    "shopify_productos": "Shopify: nombre/descripción/precio (segundo plano)",
    "disponibilidad": "Shopify: diferencias de disponibilidad (segundo plano)",
    "shopify_lista": "Shopify: lista de productos (segundo plano)",
    "cotizaciones": "Cotizaciones: vencimientos (segundo plano)",
}


def rss_mb():
    """Memoria que usa la app ahora (MB). En Mac da el pico (macOS no da el dato actual)."""
    try:
        with open("/proc/self/status") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return int(linea.split()[1]) / 1024
    except Exception:
        pass
    try:
        import resource
        import sys
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 1048576 if sys.platform == "darwin" else r / 1024
    except Exception:
        return None


def anotar(actividad, antes, despues):
    if antes is None or despues is None:
        return
    delta = despues - antes
    with _LOCK:
        if _INICIO["mb"] is None:
            _INICIO["mb"] = antes
        r = _RESUMEN.setdefault(actividad, {"veces": 0, "subio": 0.0, "max": 0.0})
        r["veces"] += 1
        if delta > 0:
            r["subio"] += delta
            r["max"] = max(r["max"], delta)
        if abs(delta) >= NOTABLE_MB:
            _REGISTROS.append({"Hora": dt.datetime.now().strftime("%H:%M:%S"), "Actividad": actividad,
                               "Memoria antes (MB)": round(antes), "Cambio (MB)": round(delta, 1)})


class medir:
    """with medir("Ventas › Pedidos"): ...  → anota cuánto cambió la memoria en ese bloque.
    Si `contenedor` se da, también entra/sale de él (para usarlo en lugar de un st.container)."""
    def __init__(self, actividad, contenedor=None):
        self.actividad, self.contenedor = actividad, contenedor

    def __enter__(self):
        self.antes = rss_mb()
        return self.contenedor.__enter__() if self.contenedor is not None else self

    def __exit__(self, *exc):
        res = self.contenedor.__exit__(*exc) if self.contenedor is not None else False
        actividad = self.actividad() if callable(self.actividad) else self.actividad
        anotar(actividad, self.antes, rss_mb())
        return res


def render():
    """Bitácora en la barra lateral (solo Dios)."""
    import pandas as pd
    import streamlit as st
    with st.expander("Bitácora de memoria"):
        with _LOCK:
            resumen = [{"Actividad": k, "Veces": v["veces"], "Subió en total (MB)": round(v["subio"], 1),
                        "Mayor subida (MB)": round(v["max"], 1)} for k, v in _RESUMEN.items()]
            registros = list(_REGISTROS)[::-1]
        actual = rss_mb()
        if _INICIO["mb"] is not None and actual is not None:
            st.caption(f"Desde las {_INICIO['hora']:%H:%M}: de {_INICIO['mb']:,.0f} MB a {actual:,.0f} MB.")
        if not resumen:
            st.caption("Todavía no hay registros. Se llenan solos a medida que se usa la app.")
            return
        st.markdown("**Qué hace subir la memoria** (de más a menos)")
        st.dataframe(pd.DataFrame(resumen).sort_values("Subió en total (MB)", ascending=False),
                     hide_index=True, use_container_width=True)
        if registros:
            st.markdown(f"**Últimos movimientos de {NOTABLE_MB:.0f} MB o más**")
            st.dataframe(pd.DataFrame(registros[:30]), hide_index=True, use_container_width=True)
