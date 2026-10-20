"""
Sincronización de NOMBRE, DESCRIPCIÓN y PRECIO entre el inventario y Shopify (por SKU = MODELO).

Idea ("tres vías"): se guarda lo último que ambos lados tenían igual (tabla shopify_producto_sync).
Para cada campo:
  * cambió solo en Shopify     -> se trae al inventario;
  * cambió solo en el inventario -> se manda a Shopify;
  * cambió en los dos y es distinto -> CONFLICTO: no se toca nada y queda para que elijas;
  * nunca se pasa un valor vacío (ni un precio en 0) de un lado al otro.
Sin lógica de pantalla: la usan shopify_productos.py (app) y se puede probar sola.
"""
import datetime as dt
import json
import re

from sync_shopify import _num, _parse  # noqa: F401  (utilidades compartidas)
from shopify_descripciones import html_a_texto

CAMPOS = ("nombre", "desc", "precio")
ETIQUETA = {"nombre": "nombre", "desc": "descripción", "precio": "precio"}
CLAVE_INV = {"nombre": "NOMBRE", "desc": "DESCRIPCION", "precio": "REDONDEO_SHOWROOM"}
COL_SNAP = {"nombre": "nombre", "desc": "descripcion", "precio": "precio"}

Q_PRODUCTOS = """
query($q: String, $cursor: String) {
  products(first: 50, after: $cursor, query: $q) {
    nodes { id title updatedAt descriptionHtml variants(first: 3) { nodes { sku price } } }
    pageInfo { hasNextPage endCursor }
  }
}"""


def _norm_texto(t):
    return re.sub(r"\s+", " ", str(t or "")).strip()


def _n(campo, v):
    """Valor normalizado para comparar."""
    return round(_num(v), 2) if campo == "precio" else _norm_texto(v)


def _vacio(campo, v):
    return v <= 0 if campo == "precio" else not v


def valores_inventario(d):
    return {"nombre": str(d.get("NOMBRE", "") or "").strip(), "desc": str(d.get("DESCRIPCION", "") or ""),
            "precio": _num(d.get("REDONDEO_SHOWROOM"))}


# ----------------------------------------------------------------------
# Lectura de Shopify
# ----------------------------------------------------------------------
def leer_shopify(gql, desde_iso=None):
    """{sku: {"nombre","desc","precio"}} de los productos de Shopify (todos, o los cambiados desde `desde_iso`)."""
    res, cursor = {}, None
    q = f"updated_at:>={desde_iso}" if desde_iso else None
    while True:
        d = gql(Q_PRODUCTOS, {"q": q, "cursor": cursor})["products"]
        for p in d["nodes"]:
            for v in p["variants"]["nodes"]:
                sku = str(v.get("sku") or "").strip()
                if sku:
                    res[sku] = {"nombre": str(p["title"] or "").strip(), "desc": html_a_texto(p["descriptionHtml"]),
                                "precio": _num(v.get("price"))}
        if not d["pageInfo"]["hasNextPage"]:
            return res
        cursor = d["pageInfo"]["endCursor"]


# ----------------------------------------------------------------------
# Decisión por campo
# ----------------------------------------------------------------------
def decidir(snap, inv, sh, conflicto_previo=None):
    """Devuelve {campo: ("snap"|"traer"|"enviar"|"conflicto", valor)} solo para campos con algo que hacer.
    snap: fila de shopify_producto_sync o None. inv / sh: valores de cada lado."""
    acciones = {}
    conflicto_previo = conflicto_previo or {}
    for c in CAMPOS:
        if c in conflicto_previo:
            continue
        i, h = _n(c, inv[c]), _n(c, sh[c])
        s = snap.get(COL_SNAP[c]) if snap else None
        s = None if s is None else _n(c, s)
        if s is None:                                   # sin historial
            if i == h:
                acciones[c] = ("snap", h)
            elif _vacio(c, i) and not _vacio(c, h):
                acciones[c] = ("traer", sh[c])
            elif _vacio(c, h) and not _vacio(c, i):
                acciones[c] = ("enviar", inv[c])
            elif not _vacio(c, i) and not _vacio(c, h):
                acciones[c] = ("conflicto", {"inv": inv[c], "shopify": sh[c]})
            continue
        cambio_inv, cambio_sh = i != s, h != s
        if cambio_inv and cambio_sh:
            acciones[c] = ("snap", h) if i == h else ("conflicto", {"inv": inv[c], "shopify": sh[c]})
        elif cambio_sh and not _vacio(c, h):
            acciones[c] = ("traer", sh[c])
        elif cambio_inv and not _vacio(c, i):
            acciones[c] = ("enviar", inv[c])
    return acciones


# ----------------------------------------------------------------------
# Escritura
# ----------------------------------------------------------------------
def _ahora():
    return dt.datetime.now().strftime("%d/%m %H:%M")


def _fmt(c, v):
    return f"${_num(v):,.2f}" if c == "precio" else (_norm_texto(v)[:40] + ("…" if len(_norm_texto(v)) > 40 else ""))


def registrar_snapshot(sb, modelo, valores, ultimo_cambio=None):
    """Guarda como 'lo último en común' los campos indicados ({"nombre":..., "desc":..., "precio":...})."""
    fila = {"modelo": str(modelo), "actualizado": dt.datetime.now(dt.timezone.utc).isoformat()}
    for c, v in valores.items():
        fila[COL_SNAP[c]] = round(_num(v), 2) if c == "precio" else _norm_texto(v)
    if ultimo_cambio:
        fila["ultimo_cambio"] = ultimo_cambio
    sb.table("shopify_producto_sync").upsert(fila, on_conflict="modelo").execute()


def _guardar_inventario(sb, modelo, d):
    sb.table("productos").update({"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", modelo).execute()


def aplicar(sb, empujar, modelo, d, snap, inv, sh, acciones):
    """Ejecuta las acciones de un producto. d = datos_json (dict) del inventario, se modifica y guarda.
    empujar(modelo, pdata, campos) -> (ok, mensaje). Devuelve {"traidos":n,"enviados":n,"conflictos":n,"errores":[...]}."""
    r = {"traidos": 0, "enviados": 0, "conflictos": 0, "errores": []}
    nuevos, cambios, conflicto = {}, [], dict((snap or {}).get("conflicto") or {})
    # 1) traer: Shopify -> inventario
    traer = {c: v for c, (a, v) in acciones.items() if a == "traer"}
    for c, v in traer.items():
        clave = CLAVE_INV[c]
        if c != "precio" and _norm_texto(d.get(clave)):
            d[clave + "_ANTERIOR"] = d.get(clave)
        d[clave] = v
        nuevos[c] = v
        cambios.append(f"{ETIQUETA[c]}: Shopify→inventario ({_fmt(c, inv[c])} → {_fmt(c, v)})")
    if traer:
        _guardar_inventario(sb, modelo, d)
        r["traidos"] += len(traer)
    # 2) enviar: inventario -> Shopify
    enviar = {c: v for c, (a, v) in acciones.items() if a == "enviar"}
    if enviar:
        ok, msg = empujar(modelo, d, set(enviar))
        if ok:
            for c, v in enviar.items():
                nuevos[c] = v
                cambios.append(f"{ETIQUETA[c]}: inventario→Shopify ({_fmt(c, sh[c])} → {_fmt(c, v)})")
            r["enviados"] += len(enviar)
        else:
            r["errores"].append(f"{modelo}: no se pudo enviar a Shopify ({msg})")
    # 3) acuerdos y conflictos
    for c, (a, v) in acciones.items():
        if a == "snap":
            nuevos[c] = v
        elif a == "conflicto":
            conflicto[c] = v
            r["conflictos"] += 1
            cambios.append(f"{ETIQUETA[c]}: CONFLICTO (inventario {_fmt(c, v['inv'])} / Shopify {_fmt(c, v['shopify'])})")
    if nuevos or r["conflictos"]:
        fila = {"modelo": modelo, "conflicto": conflicto or None,
                "actualizado": dt.datetime.now(dt.timezone.utc).isoformat()}
        for c, v in nuevos.items():
            fila[COL_SNAP[c]] = round(_num(v), 2) if c == "precio" else _norm_texto(v)
        if cambios:
            fila["ultimo_cambio"] = f"{_ahora()} · " + "; ".join(cambios)
        sb.table("shopify_producto_sync").upsert(fila, on_conflict="modelo").execute()
    return r


# ----------------------------------------------------------------------
# Corrida completa / incremental
# ----------------------------------------------------------------------
def _estado(sb, clave):
    r = sb.table("shopify_sync_estado").select("valor").eq("clave", clave).limit(1).execute().data
    return r[0]["valor"] if r else None


def _guardar_estado(sb, clave, valor):
    sb.table("shopify_sync_estado").upsert({"clave": clave, "valor": valor}, on_conflict="clave").execute()


def procesar(sb, gql, empujar, shopify, progreso=None):
    resumen = {"revisados": 0, "traidos": 0, "enviados": 0, "conflictos": 0, "errores": []}
    skus = list(shopify)
    for i in range(0, len(skus), 100):
        trozo = skus[i:i + 100]
        inventario = {str(f["MODELO"]).strip(): f for f in
                      sb.table("productos").select("MODELO,datos_json").in_("MODELO", trozo).execute().data}
        snaps = {f["modelo"]: f for f in
                 sb.table("shopify_producto_sync").select("*").in_("modelo", trozo).execute().data}
        for sku in trozo:
            if sku not in inventario:
                continue
            resumen["revisados"] += 1
            try:
                d = _parse(inventario[sku].get("datos_json"))
                inv, sh, snap = valores_inventario(d), shopify[sku], snaps.get(sku)
                acciones = decidir(snap, inv, sh, (snap or {}).get("conflicto"))
                if acciones:
                    r = aplicar(sb, empujar, sku, d, snap, inv, sh, acciones)
                    for k in ("traidos", "enviados", "conflictos"):
                        resumen[k] += r[k]
                    resumen["errores"] += r["errores"]
            except Exception as ex:
                resumen["errores"].append(f"{sku}: {ex}")
        if progreso:
            progreso(min(i + 100, len(skus)), len(skus))
    return resumen


def sincronizar_productos(sb, gql, empujar, completo=False, progreso=None):
    """completo=False: solo productos que cambiaron en Shopify desde la última revisión (rápido).
    completo=True: compara TODOS los productos de Shopify con el inventario (más lento; para empezar o revisar todo).
    La primera vez (sin revisión previa) la incremental solo marca el punto de partida."""
    inicio = dt.datetime.now(dt.timezone.utc)
    if completo:
        shopify = leer_shopify(gql)
    else:
        previo = _estado(sb, "productos_desde")
        if not previo:
            _guardar_estado(sb, "productos_desde", inicio.strftime("%Y-%m-%dT%H:%M:%SZ"))
            return {"inicial": True, "revisados": 0, "traidos": 0, "enviados": 0, "conflictos": 0, "errores": []}
        desde = (dt.datetime.strptime(previo, "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        shopify = leer_shopify(gql, desde)
    resumen = procesar(sb, gql, empujar, shopify, progreso)
    if not resumen["errores"] or completo:
        _guardar_estado(sb, "productos_desde", inicio.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return resumen


def sincronizar_un_producto(sb, gql, empujar, modelo):
    """Revisa un solo producto (por SKU) contra Shopify. Devuelve el resumen o None si no está en Shopify."""
    q = f'sku:"{modelo}"'
    res, cursor = {}, None
    d = gql("""query($q: String!) { productVariants(first: 5, query: $q) { nodes { sku price
              product { title descriptionHtml } } } }""", {"q": q})["productVariants"]["nodes"]
    for v in d:
        if str(v.get("sku") or "").strip() == modelo:
            p = v["product"]
            res[modelo] = {"nombre": str(p["title"] or "").strip(), "desc": html_a_texto(p["descriptionHtml"]),
                           "precio": _num(v.get("price"))}
    if not res:
        return None
    return procesar(sb, gql, empujar, res)


def resolver_conflicto(sb, empujar, modelo, campo, usar):
    """usar = 'shopify' (se trae al inventario) o 'inventario' (se manda a Shopify). Devuelve (ok, mensaje)."""
    snap = (sb.table("shopify_producto_sync").select("*").eq("modelo", modelo).limit(1).execute().data or [None])[0]
    if not snap or campo not in (snap.get("conflicto") or {}):
        return False, "Ese conflicto ya no existe."
    c = snap["conflicto"][campo]
    r = sb.table("productos").select("datos_json").eq("MODELO", modelo).limit(1).execute().data
    if not r:
        return False, "El producto no existe en el inventario."
    d = _parse(r[0].get("datos_json"))
    if usar == "shopify":
        clave = CLAVE_INV[campo]
        if campo != "precio" and _norm_texto(d.get(clave)):
            d[clave + "_ANTERIOR"] = d.get(clave)
        d[clave] = c["shopify"]
        _guardar_inventario(sb, modelo, d)
        valor, texto = c["shopify"], "Shopify→inventario"
    else:
        ok, msg = empujar(modelo, d, {campo})
        if not ok:
            return False, f"No se pudo enviar a Shopify: {msg}"
        valor, texto = valores_inventario(d)[campo], "inventario→Shopify"
    restante = {k: v for k, v in snap["conflicto"].items() if k != campo}
    fila = {"modelo": modelo, "conflicto": restante or None, COL_SNAP[campo]: (
        round(_num(valor), 2) if campo == "precio" else _norm_texto(valor)),
        "ultimo_cambio": f"{_ahora()} · {ETIQUETA[campo]}: conflicto resuelto, {texto}",
        "actualizado": dt.datetime.now(dt.timezone.utc).isoformat()}
    sb.table("shopify_producto_sync").upsert(fila, on_conflict="modelo").execute()
    return True, "Listo."