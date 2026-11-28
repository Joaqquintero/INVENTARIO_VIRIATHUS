"""
Shopify: en cada colección marcada, las piezas DISPONIBLES primero y las AGOTADAS (vendidas) al fondo.

Cómo funciona:
  * En Herramientas › «Shopify: agotados al fondo» se marcan las colecciones.
  * Shopify solo deja reordenar a mano las colecciones con orden «Manual». Al marcar una, la página guarda el
    orden que tenía (p. ej. «Más nuevos primero»), la pasa a «Manual» y la acomoda: primero las disponibles
    en ese orden original, después las agotadas en ese mismo orden. Al desmarcarla, vuelve el orden original.
  * Mientras alguien usa la página, cada INTERVALO_SEG se revisa y se reacomoda sola (piezas que se vendieron,
    piezas nuevas). Solo se mandan a Shopify los movimientos necesarios; si ya está bien, no se toca nada.
  * La lista de colecciones marcadas vive en la tabla shopify_sync_estado (clave «orden_colecciones»).

Agotada = Shopify lleva el inventario de la pieza y tiene 0 o menos (lo que pone la sincronización al venderse).
"""
import datetime as dt
import json
import time

import pandas as pd
import streamlit as st

from shopify_borrador import _gql
from segundo_plano import TareaPeriodica
from sync_productos import _estado, _guardar_estado

INTERVALO_SEG = 900          # 15 minutos
CLAVE = "orden_colecciones"
MAX_MOVES = 250              # lo máximo que Shopify acepta por pedido

# Orden de la colección en Shopify -> (cómo pedirle los productos en ese orden, al revés)
ORDENES = {
    "MANUAL": ("MANUAL", False),
    "BEST_SELLING": ("BEST_SELLING", False),
    "ALPHA_ASC": ("TITLE", False), "ALPHA_DESC": ("TITLE", True),
    "PRICE_ASC": ("PRICE", False), "PRICE_DESC": ("PRICE", True),
    "CREATED": ("CREATED", False), "CREATED_DESC": ("CREATED", True),
}
NOMBRES = {"MANUAL": "Manual", "BEST_SELLING": "Más vendidos", "ALPHA_ASC": "Alfabético A-Z",
           "ALPHA_DESC": "Alfabético Z-A", "PRICE_ASC": "Precio: menor a mayor", "PRICE_DESC": "Precio: mayor a menor",
           "CREATED": "Más antiguos primero", "CREATED_DESC": "Más nuevos primero"}

Q_COLECCIONES = """
query($c: String) {
  collections(first: 100, after: $c, sortKey: TITLE) {
    nodes { id title sortOrder productsCount { count } }
    pageInfo { hasNextPage endCursor }
  }
}"""
Q_PRODUCTOS = """
query($id: ID!, $c: String, $k: ProductCollectionSortKeys, $r: Boolean) {
  collection(id: $id) {
    sortOrder
    products(first: 250, after: $c, sortKey: $k, reverse: $r) {
      nodes { id tracksInventory totalInventory }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""
M_ORDEN = """
mutation($input: CollectionInput!) {
  collectionUpdate(input: $input) { collection { id sortOrder } userErrors { field message } }
}"""
M_MOVER = """
mutation($id: ID!, $moves: [MoveInput!]!) {
  collectionReorderProducts(id: $id, moves: $moves) { job { id done } userErrors { field message } }
}"""
Q_JOB = "query($id: ID!) { job(id: $id) { done } }"


# ----------------------------------------------------------------------
# Lógica (sin pantalla)
# ----------------------------------------------------------------------
def leer_config(sb):
    v = _estado(sb, CLAVE)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


def guardar_config(sb, cfg):
    _guardar_estado(sb, CLAVE, json.dumps(cfg, ensure_ascii=False))


def agotada(p):
    return bool(p.get("tracksInventory")) and (p.get("totalInventory") or 0) <= 0


def colecciones(gql):
    todas, cursor = [], None
    while True:
        d = gql(Q_COLECCIONES, {"c": cursor})["collections"]
        todas += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return todas
        cursor = d["pageInfo"]["endCursor"]


def productos(gql, cid, orden):
    """(sortOrder actual de la colección, [(id, agotada)] en el `orden` pedido)."""
    clave, reves = ORDENES.get(orden, ("MANUAL", False))
    lista, cursor, actual = [], None, None
    while True:
        col = gql(Q_PRODUCTOS, {"id": cid, "c": cursor, "k": clave, "r": reves})["collection"]
        if col is None:
            raise RuntimeError("La colección ya no existe en Shopify.")
        actual = col["sortOrder"]
        d = col["products"]
        lista += [(p["id"], agotada(p)) for p in d["nodes"]]
        if not d["pageInfo"]["hasNextPage"]:
            return actual, lista
        cursor = d["pageInfo"]["endCursor"]


def _quietas(seq):
    """Índices de la subsecuencia creciente más larga de `seq`: las piezas que ya están en buen orden y no se tocan."""
    colas, previo, finales = [], [-1] * len(seq), []
    for i, v in enumerate(seq):
        lo, hi = 0, len(colas)
        while lo < hi:
            m = (lo + hi) // 2
            if seq[colas[m]] < v:
                lo = m + 1
            else:
                hi = m
        previo[i] = colas[lo - 1] if lo else -1
        if lo == len(colas):
            colas.append(i)
        else:
            colas[lo] = i
    i = colas[-1] if colas else -1
    while i != -1:
        finales.append(i)
        i = previo[i]
    return set(finales)


def _directo(actual, deseado):
    """Recorre el orden deseado y trae cada pieza a su lugar. Bueno cuando hay que reacomodar casi todo."""
    lista, moves = list(actual), []
    for i, pid in enumerate(deseado):
        if lista[i] != pid:
            lista.insert(i, lista.pop(lista.index(pid, i)))
            moves.append({"id": pid, "newPosition": str(i)})
    return moves


def movimientos(actual, deseado):
    """Los movimientos para pasar de `actual` a `deseado`: se calculan de dos formas y se usa la que mueve menos."""
    a, b = _directo(actual, deseado), _por_quietas(actual, deseado)
    return a if len(a) <= len(b) else b


def _por_quietas(actual, deseado):
    """Movimientos para pasar de `actual` a `deseado`, tocando solo las piezas fuera de lugar.
    Shopify los aplica en orden; cada uno pone la pieza en esa posición (desde 0) y corre las demás.
      1) Las piezas que ya están en buen orden entre sí (la secuencia más larga) no se tocan.
      2) Las demás se mandan al final, en el orden deseado.
      3) Se recorre el orden deseado y cada una de esas se pone en su lugar (si no quedó ya ahí)."""
    pos = {p: i for i, p in enumerate(deseado)}
    quietas = {actual[j] for j in _quietas([pos[p] for p in actual])}
    lista, moves = list(actual), []

    def mover(pid, i):
        lista.remove(pid)
        lista.insert(i, pid)
        moves.append({"id": pid, "newPosition": str(i)})

    for pid in deseado:
        if pid not in quietas:
            mover(pid, len(lista) - 1)
    for i, pid in enumerate(deseado):
        if lista[i] != pid:
            mover(pid, i)
    return moves


def _errores(res):
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "") for e in errs))


def _esperar(gql, job, segundos=40):
    if not job or job.get("done"):
        return
    fin = time.time() + segundos
    while time.time() < fin:
        time.sleep(1.5)
        if gql(Q_JOB, {"id": job["id"]})["job"]["done"]:
            return


def ordenar_coleccion(gql, cid, base):
    """Disponibles primero y agotadas al fondo, cada grupo en el orden `base`. Devuelve cuántas piezas movió."""
    estado, actual = productos(gql, cid, "MANUAL")
    if estado != "MANUAL":
        raise RuntimeError(f"En Shopify la cambiaron a «{NOMBRES.get(estado, estado)}». Desmárcala y vuelve a marcarla.")
    referencia = actual if ORDENES.get(base, ("MANUAL",))[0] == "MANUAL" else productos(gql, cid, base)[1]
    ids = [p for p, _ in actual]
    if sorted(ids) != sorted(p for p, _ in referencia):
        return 0                                # cambió mientras se leía: se acomoda en la próxima vuelta
    deseado = [p for p, a in referencia if not a] + [p for p, a in referencia if a]
    moves = movimientos(ids, deseado)
    for i in range(0, len(moves), MAX_MOVES):
        res = gql(M_MOVER, {"id": cid, "moves": moves[i:i + MAX_MOVES]})["collectionReorderProducts"]
        _errores(res)
        _esperar(gql, res.get("job"))
    return len(moves)


def _poner_orden(gql, cid, orden):
    _errores(gql(M_ORDEN, {"input": {"id": cid, "sortOrder": orden}})["collectionUpdate"])


def activar(sb, gql, col):
    """Guarda el orden que tenía, la pasa a Manual y la acomoda."""
    cfg = leer_config(sb)
    base = cfg.get(col["id"], {}).get("base") or col["sortOrder"]
    if col["sortOrder"] != "MANUAL":
        _poner_orden(gql, col["id"], "MANUAL")
    cfg[col["id"]] = {"titulo": col["title"], "base": base}
    guardar_config(sb, cfg)
    return ordenar_coleccion(gql, col["id"], base)


def desactivar(sb, gql, cid):
    """Le devuelve el orden que tenía antes y deja de acomodarla."""
    cfg = leer_config(sb)
    datos = cfg.pop(cid, None)
    if datos and datos.get("base") and datos["base"] != "MANUAL":
        _poner_orden(gql, cid, datos["base"])
    guardar_config(sb, cfg)


def ordenar_todas(sb, gql=_gql):
    """Acomoda todas las colecciones marcadas. Devuelve {titulo: piezas movidas | "error: ..."}."""
    resultado = {}
    for cid, datos in leer_config(sb).items():
        try:
            resultado[datos.get("titulo", cid)] = ordenar_coleccion(gql, cid, datos.get("base", "MANUAL"))
        except Exception as ex:
            resultado[datos.get("titulo", cid)] = f"error: {ex}"
    return resultado


def _revisar(_sb):
    hora = dt.datetime.now().strftime("%H:%M")
    try:
        res = ordenar_todas(_sb)
        errores = [f"{t}: {v[7:]}" for t, v in res.items() if isinstance(v, str)]
        if errores:
            return {"ok": False, "error": " · ".join(errores)[:400], "hora": hora}
        return {"ok": True, "movidas": sum(res.values()), "hora": hora}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:400], "hora": hora}


_TAREA = TareaPeriodica(_revisar, INTERVALO_SEG)


def ordenar_en_segundo_plano(sb):
    """Último resultado al instante; la revisión corre en segundo plano cada INTERVALO_SEG segundos."""
    return _TAREA.obtener(sb)


# ----------------------------------------------------------------------
# Pantalla (Herramientas)
# ----------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def _colecciones_cache():
    return colecciones(_gql)


def render_orden_colecciones(sb):
    ss = st.session_state
    st.subheader("Shopify: agotados al fondo",
                 help="En las colecciones marcadas, la tienda muestra primero las piezas disponibles y al final "
                      "las vendidas. Se reacomoda sola cada 15 minutos mientras alguien usa la página.")
    if ss.get("ord_msg"):
        st.success(ss.pop("ord_msg"))
    if not st.toggle("Ver colecciones de Shopify", key="ord_ver"):
        return
    try:
        cols = _colecciones_cache()
        cfg = leer_config(sb)
    except Exception as ex:
        st.error(f"No se pudieron leer las colecciones: {ex}")
        return
    if not cols:
        st.caption("No hay colecciones en Shopify.")
        return

    df = pd.DataFrame([{
        "AL FONDO": c["id"] in cfg,
        "COLECCIÓN": c["title"],
        "PRODUCTOS": (c.get("productsCount") or {}).get("count", 0),
        "ORDEN DE LAS DISPONIBLES": NOMBRES.get(cfg[c["id"]]["base"] if c["id"] in cfg else c["sortOrder"],
                                                c["sortOrder"]),
    } for c in cols])
    st.caption("Marca las colecciones donde quieres las vendidas al final. Las disponibles conservan el orden "
               "que ya tenía la colección. Al desmarcar una, vuelve a como estaba.")
    ed = st.data_editor(df, hide_index=True, use_container_width=True, num_rows="fixed", key="ord_tabla",
                        disabled=["COLECCIÓN", "PRODUCTOS", "ORDEN DE LAS DISPONIBLES"],
                        column_config={"AL FONDO": st.column_config.CheckboxColumn("AL FONDO", width="small")})

    marcar = [c for c, v in zip(cols, ed["AL FONDO"]) if v and c["id"] not in cfg]
    quitar = [c for c, v in zip(cols, ed["AL FONDO"]) if not v and c["id"] in cfg]
    c1, c2 = st.columns(2)
    if c1.button(f"Guardar cambios ({len(marcar) + len(quitar)})", type="primary",
                 disabled=not (marcar or quitar), key="ord_guardar"):
        hechos, fallas = [], []
        with st.spinner("Acomodando en Shopify…"):
            for c in marcar:
                try:
                    n = activar(sb, _gql, c)
                    hechos.append(f"«{c['title']}»: {n} pieza(s) movidas")
                except Exception as ex:
                    fallas.append(f"«{c['title']}»: {ex}")
            for c in quitar:
                try:
                    desactivar(sb, _gql, c["id"])
                    hechos.append(f"«{c['title']}»: vuelve a su orden original")
                except Exception as ex:
                    fallas.append(f"«{c['title']}»: {ex}")
        _colecciones_cache.clear()
        if fallas:
            st.error("No se pudo: " + " · ".join(fallas))
        if hechos:
            ss["ord_msg"] = "Listo. " + " · ".join(hechos)
            ss.pop("ord_tabla", None)
            if not fallas:
                st.rerun()
    if c2.button("Acomodar ahora", disabled=not cfg, key="ord_ahora",
                 help="Normalmente se hace sola cada 15 minutos."):
        with st.spinner("Acomodando en Shopify…"):
            res = ordenar_todas(sb)
        partes = [f"«{t}»: {v if isinstance(v, str) else f'{v} movida(s)'}" for t, v in res.items()]
        st.info(" · ".join(partes) or "No hay colecciones marcadas.")
