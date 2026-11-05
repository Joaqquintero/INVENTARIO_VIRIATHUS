"""
PENDIENTES POR PAGAR (Caudal).

Lo que la tienda le debe a alguien por piezas, y que se paga de a poco: solo las piezas que «se activan».

  * CONSIGNACIÓN (ficha → sección 2): mientras la pieza no se vende queda «en consignación» (a la vista, no se
    paga). Cuando se vende (por pedido, Shopify o a mano) pasa sola a «pendiente por pagar» y sale el aviso arriba
    de la app. Si la venta se cancela antes de pagar, vuelve a «en consignación».
  * Al pagar (Caudal → Pendientes por pagar) se eligen una o varias, la cuenta desde donde se pagó, y se registra
    el movimiento en rojo en esa cuenta. Si ese movimiento se anula en Caudal, el pendiente vuelve a estar por pagar.
  * «Cambiar costo» (aviso): corrige lo que se le paga al consignador, el costo de la ficha y, si se vendió en un
    pedido, el costo de ese pedido (así la utilidad queda bien). Queda en el registro de cambios.
  * COSTOS DE LA FICHA (compra, restauro, costo financiero, flete, otros) con la casilla «Pendiente por pagar»: no se
    descuentan de nadie al guardar; quedan aquí. Al pagarlos se descuentan de la cuenta elegida y la ficha queda con
    quién pagó, la fecha y el monto pagado. Si después cambia ese costo en la ficha, Caudal NO lo corrige solo: avisa.
  * APARTADOS: se juntan varios «por pagar» (solo vendidos, nunca «en consignación») en un sobre de una cuenta
    (p. ej. Caja · efectivo). Esa plata queda separada: la tarjeta de la cuenta muestra lo LIBRE. Se paga de a partes
    o todo junto, desde la cuenta del apartado; en Movimientos cada pago del apartado es una sola línea roja que se
    abre para ver las piezas (por dentro, un movimiento por pieza: la ficha cuadra y se puede anular una sola).
    Si una pieza deja de estar vendida (vuelve a «en consignación») o se quita de la ficha, sale sola del apartado.
  * PENDIENTES A MANO («Nuevo pendiente por pagar»): cualquier deuda (a quién, concepto, monto y, si aplica, el
    modelo). Se paga o se aparta igual que los demás; mientras no se pague se puede quitar.

Tabla (correr una vez en Supabase): ver PENDIENTES_SQL.
"""
import datetime as dt
import json
import unicodedata

import streamlit as st

import codigos
from caches import guardar_en_memoria
from segundo_plano import TareaPeriodica

TABLA = "caudal_pendientes"
EN_CONSIG, PENDIENTE, PAGADO, ANULADO = "en_consignacion", "pendiente", "pagado", "anulado"
NOMBRE_TIPO = {"consignacion": "Consignación", "compra": "Compra", "restauro": "Restauro",
               "financiero": "Costo financiero", "flete": "Flete", "otro": "Otro costo", "manual": "Pendiente"}
# tipo → (lista en la ficha, campo de fecha de pago del bloque)
LISTA_DE_TIPO = {"compra": ("COMPRAS", "fpago"), "restauro": ("RESTAURADORES", "fecha"),
                 "financiero": ("COSTOS_FINANCIEROS", "fecha"), "flete": ("FLETES", "fecha"),
                 "otro": ("OTROS_COSTOS_DETALLE", "fecha")}
# cuenta de Caudal → opción de «Quién pagó» en la ficha
QUIEN_DE_CUENTA = {"Caja": "1 - CAJA", "Ernesto": "2 - ERNESTO", "Viriato": "3 - VIRIATO", "Claudia": "4 - CLAUDIA",
                   "Mary": "5 - MARY", "Ensalada de Ideas": "6 - ENSALADA DE IDEAS"}
MESES_CORTOS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

PENDIENTES_SQL = """
create table if not exists caudal_pendientes (
  id bigserial primary key,
  creado timestamptz not null default now(),
  tipo text not null,                      -- consignacion (después: compra, restauro, flete…)
  modelo text,
  referencia text unique,                  -- consignación: «consig:<modelo>»
  proveedor text,                          -- consignador / proveedor (para agrupar)
  concepto text, detalle text,
  monto numeric not null default 0,
  estado text not null default 'pendiente',-- en_consignacion | pendiente | pagado | anulado
  fecha_recepcion text,
  precio_lista numeric, precio_venta numeric, vendido_en text, numero_pedido bigint,
  pagado_en date, pagado_por text, cuenta text, medio text, movimiento_id bigint,
  usuario text, actualizado timestamptz default now()
);
"""

APARTADOS = "caudal_apartados"
AP_ABIERTO, AP_PAGADO, AP_DESHECHO = "abierto", "pagado", "deshecho"
APARTADOS_SQL = """
create table if not exists caudal_apartados (
  id bigserial primary key,
  creado timestamptz not null default now(),
  nombre text not null,
  cuenta text not null, medio text not null,      -- de dónde se separa la plata (p. ej. Caja · efectivo)
  estado text not null default 'abierto',         -- abierto | pagado | deshecho
  usuario text, cerrado_en timestamptz, cerrado_por text
);
alter table caudal_pendientes add column if not exists apartado_id bigint;
alter table caudal_movimientos add column if not exists lote text;   -- «apartado:<id>:<pago>»: una línea en Movimientos
"""


def _num(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip()) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _parse(raw):
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _hoy():
    try:
        from cotizaciones import hoy_mx
        return hoy_mx()
    except Exception:
        return dt.date.today()


def _ahora():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def clave_nombre(t):
    """«Ana  García» y «ANA GARCIA» son la misma persona (para agrupar y sugerir)."""
    t = unicodedata.normalize("NFD", str(t or ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(t.upper().split())


def dinero(n):
    return ("−" if n < -0.004 else "") + f"${abs(n):,.2f}"


def _sin_apartado(fila):
    """Campos para sacar la fila de su apartado (solo si tiene uno: así funciona aunque falte la columna)."""
    return {"apartado_id": None} if fila and fila.get("apartado_id") else {}


def _mod(f):
    """«Mod 123 · » o nada (los pendientes a mano pueden no tener modelo)."""
    m = str(f.get("modelo") or "").strip()
    return f"Mod {m} · " if m and m != "None" else ""


def ref_consignacion(modelo):
    return f"consig:{str(modelo).strip()}"


def _leer_ref(sb, ref):
    r = sb.table(TABLA).select("*").eq("referencia", ref).limit(1).execute().data
    return r[0] if r else None


# ----------------------------------------------------------------------
# ¿Se vendió? ¿A cuánto?
# ----------------------------------------------------------------------
def venta_de(sb, modelos):
    """{modelo: {"precio": float|None, "donde": texto, "pedido": n|None}} de la venta de cada modelo:
    el pedido activo (no cancelado) más reciente que lo tiene (precio con descuento) o, si no, Shopify."""
    modelos = [str(m).strip() for m in modelos if str(m or "").strip()]
    out = {}
    if not modelos:
        return out
    try:
        its = (sb.table("pedido_items").select("numero_pedido,modelo,cantidad,total_linea,descuento_pct")
               .in_("modelo", modelos).execute().data or [])
        nums = sorted({i["numero_pedido"] for i in its})
        activos = set()
        if nums:
            activos = {p["numero_pedido"] for p in (sb.table("pedidos").select("numero_pedido,status,estado_pedido")
                                                    .in_("numero_pedido", nums).execute().data or [])
                       if p.get("status") != "Cancelado" and p.get("estado_pedido") != "Cancelado"}
        for i in sorted(its, key=lambda x: x["numero_pedido"]):
            if i["numero_pedido"] not in activos:
                continue
            cant = max(int(_num(i.get("cantidad"), 1.0)), 1)
            desc = _num(i.get("descuento_pct"))
            out[str(i["modelo"]).strip()] = {
                "precio": round(_num(i.get("total_linea")) / cant, 2), "pedido": i["numero_pedido"],
                "donde": f"pedido {i['numero_pedido']}" + (f", {desc:g}% desc." if desc else "")}
    except Exception:
        pass
    faltan = [m for m in modelos if m not in out]
    if faltan:
        try:
            ords = (sb.table("shopify_ordenes").select("nombre,marcados").eq("cancelada", False)
                    .order("creado", desc=True).limit(500).execute().data or [])
            for m in faltan:
                o = next((o for o in ords if m in (o.get("marcados") or [])), None)
                if o:
                    out[m] = {"precio": None, "pedido": None, "donde": f"Shopify {o.get('nombre')}"}
        except Exception:
            pass
    return out


# ----------------------------------------------------------------------
# Consignación ↔ ficha
# ----------------------------------------------------------------------
def sincronizar_consignacion(sb, modelo, datos, usuario):
    """Después de guardar la ficha: crea / actualiza / quita la consignación de la lista. Devuelve un aviso o None."""
    modelo = str(modelo).strip()
    ref = ref_consignacion(modelo)
    actual = _leer_ref(sb, ref)
    es = bool(datos.get("CONSIGNACION"))
    monto = round(_num(datos.get("COSTO_CONSIGNACION")), 2)
    prov = " ".join(str(datos.get("CONSIGNADOR") or "").split())
    nombre = " ".join(str(datos.get("NOMBRE") or "").split())
    aviso = None
    if not es:
        if actual and actual.get("estado") in (EN_CONSIG, PENDIENTE):
            sb.table(TABLA).update({"estado": ANULADO, "actualizado": _ahora(), **_sin_apartado(actual)}) \
                .eq("id", actual["id"]).execute()
            codigos.registrar(sb, usuario, "Quitó consignación", "producto", modelo, prov)
        elif actual and actual.get("estado") == PAGADO:
            aviso = (f"Esta consignación ya se le pagó a {actual.get('proveedor')}: quitarle la marca de consignación "
                     "no cambia ese pago.")
        _limpiar()
        return aviso
    datos_fila = {"tipo": "consignacion", "modelo": modelo, "referencia": ref, "proveedor": prov,
                  "concepto": f"Consignación · Mod {modelo}", "detalle": nombre, "monto": monto,
                  "fecha_recepcion": str(datos.get("FECHA_CONSIGNACION") or "") or None, "actualizado": _ahora()}
    if actual is None:
        sb.table(TABLA).insert({**datos_fila, "estado": EN_CONSIG, "usuario": usuario}).execute()
        codigos.registrar(sb, usuario, "Nueva consignación", "producto", modelo, f"{prov} · ${monto:,.2f}")
    elif actual.get("estado") == ANULADO:
        sb.table(TABLA).update({**datos_fila, "estado": EN_CONSIG}).eq("id", actual["id"]).execute()
    elif actual.get("estado") in (EN_CONSIG, PENDIENTE):
        if round(_num(actual.get("monto")), 2) != monto or (actual.get("proveedor") or "") != prov:
            codigos.registrar(sb, usuario, "Cambió consignación", "producto", modelo,
                              f"{actual.get('proveedor')} ${_num(actual.get('monto')):,.2f} → {prov} ${monto:,.2f}")
        sb.table(TABLA).update(datos_fila).eq("id", actual["id"]).execute()
    elif actual.get("estado") == PAGADO:
        if round(_num(actual.get("monto")), 2) != monto:
            aviso = (f"Esta consignación ya se le pagó a {actual.get('proveedor')} "
                     f"({dinero(_num(actual.get('monto')))}): el costo nuevo no cambia ese pago.")
    revisar_consignaciones(sb, solo=[ref])
    _limpiar()
    return aviso


def ref_ficha(modelo, tipo, n):
    """La misma referencia que usa Caudal para ese costo de la ficha («ficha:<modelo>:restauro:0»)."""
    return f"ficha:{str(modelo).strip()}:{tipo}:{n}"


def sincronizar_costos_ficha(sb, modelo, datos, usuario):
    """Después de guardar la ficha: los costos marcados «Pendiente por pagar» quedan en la lista (o se actualizan);
    los que se desmarcaron o se quitaron salen de la lista. Devuelve una lista de avisos."""
    modelo = str(modelo).strip()
    nombre = " ".join(str(datos.get("NOMBRE") or "").split())
    actuales = {f["referencia"]: f for f in (sb.table(TABLA).select("*").like("referencia", f"ficha:{modelo}:%")
                                             .execute().data or [])}
    vistos, avisos = set(), []
    for tipo, (lista_k, _) in LISTA_DE_TIPO.items():
        for n, b in enumerate(datos.get(lista_k) or []):
            ref = ref_ficha(modelo, tipo, n)
            costo = round(_num(b.get("costo")), 2)
            prev = actuales.get(ref)
            if b.get("pagado_monto") is not None:
                vistos.add(ref)
                if costo != round(_num(b.get("pagado_monto")), 2):
                    avisos.append(f"{NOMBRE_TIPO[tipo]} #{n + 1}: ya se pagó {dinero(_num(b.get('pagado_monto')))} desde "
                                  f"Pendientes; el costo nuevo ({dinero(costo)}) NO cambia Caudal. Si hubo una devolución "
                                  "o un pago extra, regístralo a mano en Caudal (Entrada o Gasto).")
                continue
            if not b.get("pend") or costo <= 0:
                continue
            vistos.add(ref)
            fila = {"tipo": tipo, "modelo": modelo, "referencia": ref, "proveedor": " ".join(str(b.get("prov") or "").split()),
                    "concepto": f"{NOMBRE_TIPO[tipo]} · Mod {modelo}", "detalle": nombre, "monto": costo,
                    "actualizado": _ahora()}
            if prev is None:
                sb.table(TABLA).insert({**fila, "estado": PENDIENTE, "usuario": usuario}).execute()
            elif prev.get("estado") in (PENDIENTE, ANULADO):
                sb.table(TABLA).update({**fila, "estado": PENDIENTE}).eq("id", prev["id"]).execute()
    for ref, f in actuales.items():
        if ref not in vistos and f.get("estado") == PENDIENTE:     # se desmarcó o se quitó: sale de la lista
            sb.table(TABLA).update({"estado": ANULADO, "actualizado": _ahora(), **_sin_apartado(f)}) \
                .eq("id", f["id"]).execute()
    _limpiar()
    return avisos


def _fecha_ficha(fecha):
    """«04/nov/26», como se escriben las fechas en la ficha."""
    return f"{fecha.day:02d}/{MESES_CORTOS[fecha.month - 1]}/{fecha:%y}"


def _ficha_bloque(sb, f, cambio):
    """Aplica `cambio(bloque)` al bloque de la ficha al que pertenece el pendiente `f` y guarda la ficha."""
    try:
        _, modelo, tipo, n = str(f["referencia"]).split(":")
        lista_k = LISTA_DE_TIPO[tipo][0]
        n = int(n)
    except (ValueError, KeyError):
        return
    r = sb.table("productos").select("datos_json").eq("MODELO", modelo).limit(1).execute().data
    if not r:
        return
    d = _parse(r[0].get("datos_json"))
    lista = d.get(lista_k) or []
    if n >= len(lista):
        return
    cambio(lista[n], tipo)
    d[lista_k] = lista
    sb.table("productos").update({"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", modelo).execute()


def revisar_consignaciones(sb, solo=None):
    """Pasa a «pendiente por pagar» las consignaciones cuya pieza ya se vendió, y regresa a «en consignación» las
    que se des-vendieron (venta cancelada) sin haberse pagado. `solo` = referencias a revisar (None = todas)."""
    q = (sb.table(TABLA).select("*").eq("tipo", "consignacion").in_("estado", [EN_CONSIG, PENDIENTE]))
    if solo:
        q = q.in_("referencia", list(solo))
    filas = q.execute().data or []
    if not filas:
        return {"ok": True, "cambios": 0, "hora": dt.datetime.now().strftime("%H:%M")}
    modelos = sorted({str(f["modelo"]) for f in filas if f.get("modelo")})
    fichas = {}
    for i in range(0, len(modelos), 100):
        for p in (sb.table("productos").select("MODELO,datos_json").in_("MODELO", modelos[i:i + 100])
                  .execute().data or []):
            fichas[str(p["MODELO"]).strip()] = _parse(p.get("datos_json"))
    vendidos = [m for m, d in fichas.items()
                if str(d.get("DISP", d.get("DISPONIBILIDAD", "")) or "").strip().lower() == "v"]
    ventas = venta_de(sb, vendidos)
    cambios = 0
    for f in filas:
        m = str(f.get("modelo") or "")
        d = fichas.get(m)
        vendido = m in vendidos
        if f["estado"] == EN_CONSIG and vendido:
            v = ventas.get(m) or {}
            sb.table(TABLA).update({
                "estado": PENDIENTE, "precio_lista": _num((d or {}).get("REDONDEO_SHOWROOM")),
                "precio_venta": v.get("precio"), "vendido_en": v.get("donde") or "vendido",
                "numero_pedido": v.get("pedido"), "actualizado": _ahora()}).eq("id", f["id"]).eq("estado", EN_CONSIG).execute()
            cambios += 1
        elif f["estado"] == PENDIENTE and d is not None and not vendido:
            sb.table(TABLA).update({"estado": EN_CONSIG, "precio_venta": None, "vendido_en": None,
                                    "numero_pedido": None, "actualizado": _ahora(), **_sin_apartado(f)}) \
                .eq("id", f["id"]).eq("estado", PENDIENTE).execute()
            cambios += 1
        elif f["estado"] == PENDIENTE and vendido:
            v = ventas.get(m) or {}
            if v and (v.get("donde") != f.get("vendido_en") or v.get("precio") != (
                    None if f.get("precio_venta") is None else _num(f.get("precio_venta")))):
                sb.table(TABLA).update({"precio_venta": v.get("precio"), "vendido_en": v.get("donde"),
                                        "numero_pedido": v.get("pedido"), "actualizado": _ahora()}).eq("id", f["id"]).execute()
                cambios += 1
    if cambios:
        _limpiar()
    return {"ok": True, "cambios": cambios, "hora": dt.datetime.now().strftime("%H:%M")}


# Revisión en segundo plano (como máximo cada 3 min, compartida entre sesiones)
TAREA = TareaPeriodica(revisar_consignaciones, 180)


# ----------------------------------------------------------------------
# Lectura
# ----------------------------------------------------------------------
@guardar_en_memoria(ttl=60)
def _abiertos_mem(_sb):
    return (_sb.table(TABLA).select("*").in_("estado", [EN_CONSIG, PENDIENTE]).order("id").limit(5000)
            .execute().data or [])


def abiertos(sb):
    try:
        return _abiertos_mem(sb)
    except Exception:
        return None                 # todavía no existe la tabla


def _limpiar():
    try:
        _abiertos_mem.clear()
        _consignadores_mem.clear()
    except Exception:
        pass


@guardar_en_memoria(ttl=300)
def _consignadores_mem(_sb):
    filas = (_sb.table(TABLA).select("proveedor").eq("tipo", "consignacion").neq("estado", ANULADO)
             .limit(5000).execute().data or [])
    nombres = {}
    for f in filas:
        p = " ".join(str(f.get("proveedor") or "").split())
        if p:
            nombres.setdefault(clave_nombre(p), p)
    return sorted(nombres.values())


def consignadores(sb):
    """Nombres de consignadores ya usados (para sugerir y no escribirlos distinto)."""
    try:
        return _consignadores_mem(sb)
    except Exception:
        return []


def sugerir(nombres, texto, max_res=5):
    t = clave_nombre(texto)
    if len(t) < 3:
        return []
    exactos = [n for n in nombres if clave_nombre(n) == t]
    if exactos:
        return exactos[:1]
    palabras = t.split()
    return [n for n in nombres if all(p in clave_nombre(n) for p in palabras)][:max_res]


# ----------------------------------------------------------------------
# Pagar / cambiar costo / deshacer un pago
# ----------------------------------------------------------------------
def pagar(sb, ids, cuenta, medio, fecha, usuario, apartado=None):
    """Paga los pendientes elegidos desde (cuenta, medio): registra cada movimiento en rojo en Caudal.
    Con `apartado` (fila de caudal_apartados) paga desde la cuenta del apartado y los movimientos llevan el mismo
    «lote» (una sola línea en Movimientos). Devuelve (n_pagados, total). Lanza ValueError si no hay nada que pagar."""
    import uuid
    import caudal
    if apartado:
        cuenta, medio = apartado["cuenta"], apartado["medio"]
    cuenta, medio = caudal.normalizar(cuenta, medio)
    filas = sb.table(TABLA).select("*").in_("id", list(ids)).eq("estado", PENDIENTE).execute().data or []
    if apartado:
        filas = [f for f in filas if f.get("apartado_id") == apartado["id"]]
    if not filas:
        raise ValueError("No hay nada por pagar en lo que elegiste (¿ya se pagó?).")
    fecha = (fecha or _hoy()).isoformat()
    extra, prefijo = {}, ""
    if apartado:
        extra = {"lote": f"apartado:{apartado['id']}:{uuid.uuid4().hex[:8]}"}
        prefijo = f"Apartado «{apartado.get('nombre')}» · "
    n, total = 0, 0.0
    for f in filas:
        r = (sb.table(TABLA).update({"estado": PAGADO, "pagado_en": fecha, "pagado_por": usuario, "cuenta": cuenta,
                                     "medio": medio, "actualizado": _ahora()})
             .eq("id", f["id"]).eq("estado", PENDIENTE).execute())
        if not r.data:
            continue                                   # otra persona lo pagó al mismo tiempo
        monto = round(_num(f.get("monto")), 2)
        de_ficha = f.get("tipo") in LISTA_DE_TIPO
        try:
            mov = sb.table("caudal_movimientos").insert({
                "fecha": fecha, "cuenta": cuenta, "medio": medio, "monto": -monto, "tipo": f.get("tipo"),
                "concepto": (f"{prefijo}Pago {NOMBRE_TIPO.get(f.get('tipo'), f.get('tipo'))} · "
                             + (_mod(f).rstrip(" ·") or str(f.get("detalle") or "")[:80])),
                "detalle": " · ".join(x for x in (f.get("proveedor"), f.get("detalle")) if x),
                # costo de la ficha: la misma referencia que usa Caudal, así la ficha y Caudal cuadran
                "origen": "pendiente", "referencia": f["referencia"] if de_ficha else f"pend:{f['id']}",
                "usuario": usuario, **extra}).execute().data
            sb.table(TABLA).update({"movimiento_id": (mov or [{}])[0].get("id")}).eq("id", f["id"]).execute()
            if de_ficha:
                fecha_d = dt.date.fromisoformat(fecha)

                def _pagado(b, tipo, monto=monto, fecha_d=fecha_d):
                    b["pend"] = False
                    b["pagado_monto"] = monto
                    b["pagado_info"] = f"{fecha_d:%d/%m/%Y} · {cuenta} · {medio} · {usuario}"
                    b["pagado_cuenta"], b["pagado_medio"] = cuenta, medio
                    b["qp"] = QUIEN_DE_CUENTA.get(cuenta, b.get("qp"))
                    b["fp"] = "1 - EFECTIVO" if medio == "efectivo" else "2 - TRANSFERENCIA"
                    b[LISTA_DE_TIPO[tipo][1]] = _fecha_ficha(fecha_d)
                _ficha_bloque(sb, f, _pagado)
        except Exception:
            sb.table(TABLA).update({"estado": PENDIENTE, "pagado_en": None, "pagado_por": None, "cuenta": None,
                                    "medio": None}).eq("id", f["id"]).execute()
            raise
        n += 1
        total += monto
    if apartado:
        _cerrar_si_pagado(sb, apartado["id"], usuario)
    codigos.registrar(sb, usuario, "Pagó pendientes", "caudal", f"{cuenta} · {medio}",
                      f"{prefijo}{n} pendiente(s) · ${total:,.2f}")
    _limpiar()
    return n, round(total, 2)


def deshacer_pago(sb, mov):
    """Se anuló en Caudal el movimiento de un pago: el pendiente vuelve a estar por pagar (y, si era un costo de la
    ficha, el bloque vuelve a «Pendiente por pagar»)."""
    ref = str(mov.get("referencia") or "")
    if ref.startswith("pend:"):
        filas = sb.table(TABLA).select("*").eq("id", int(ref.split(":", 1)[1])).eq("estado", PAGADO).execute().data
    else:
        filas = sb.table(TABLA).select("*").eq("movimiento_id", mov.get("id")).eq("estado", PAGADO).execute().data
    for f in filas or []:
        sb.table(TABLA).update({"estado": PENDIENTE, "pagado_en": None, "pagado_por": None, "cuenta": None,
                                "medio": None, "movimiento_id": None, "actualizado": _ahora()}).eq("id", f["id"]).execute()
        if f.get("apartado_id"):                 # era de un apartado: vuelve a estar apartada (y el apartado abierto)
            try:
                sb.table(APARTADOS).update({"estado": AP_ABIERTO, "cerrado_en": None, "cerrado_por": None}) \
                    .eq("id", f["apartado_id"]).eq("estado", AP_PAGADO).execute()
            except Exception:
                pass
        if f.get("tipo") in LISTA_DE_TIPO:
            def _otra_vez_pendiente(b, tipo):
                b["pend"] = True
                b.pop("pagado_monto", None)
                b.pop("pagado_info", None)
                b.pop("pagado_cuenta", None)
                b.pop("pagado_medio", None)
            _ficha_bloque(sb, f, _otra_vez_pendiente)
    _limpiar()


# ----------------------------------------------------------------------
# Pendientes a mano
# ----------------------------------------------------------------------
def crear_manual(sb, proveedor, concepto, monto, modelo, usuario):
    """Pendiente por pagar creado a mano (cualquier deuda). Devuelve la fila."""
    import uuid
    proveedor = " ".join(str(proveedor or "").split())
    concepto = " ".join(str(concepto or "").split())
    modelo = str(modelo or "").strip() or None
    monto = round(_num(monto), 2)
    if not proveedor:
        raise ValueError("Escribe a quién se le debe.")
    if not concepto:
        raise ValueError("Escribe el concepto (qué se le debe).")
    if monto <= 0:
        raise ValueError("El monto tiene que ser mayor a $0.")
    fila = sb.table(TABLA).insert({
        "tipo": "manual", "modelo": modelo, "referencia": f"manual:{uuid.uuid4().hex[:12]}", "proveedor": proveedor,
        "concepto": concepto, "detalle": concepto, "monto": monto, "estado": PENDIENTE, "usuario": usuario,
        "actualizado": _ahora()}).execute().data[0]
    codigos.registrar(sb, usuario, "Creó pendiente por pagar", "caudal", proveedor,
                      f"{concepto} · ${monto:,.2f}" + (f" · Mod {modelo}" if modelo else ""))
    _limpiar()
    return fila


def quitar_manual(sb, pend, usuario):
    if pend.get("tipo") != "manual":
        raise ValueError("Solo se quitan aquí los pendientes creados a mano.")
    r = (sb.table(TABLA).update({"estado": ANULADO, "actualizado": _ahora(), **_sin_apartado(pend)})
         .eq("id", pend["id"]).eq("estado", PENDIENTE).execute())
    if not r.data:
        raise ValueError("Ese pendiente ya no está por pagar (¿ya se pagó?).")
    codigos.registrar(sb, usuario, "Quitó pendiente por pagar", "caudal", pend.get("proveedor"),
                      f"{pend.get('detalle')} · ${_num(pend.get('monto')):,.2f}")
    _limpiar()


# ----------------------------------------------------------------------
# Apartados (plata separada en una cuenta para pagar varios pendientes)
# ----------------------------------------------------------------------
def apartados_abiertos(sb):
    """Apartados abiertos ([] si no hay; None si todavía no existe la tabla)."""
    try:
        return sb.table(APARTADOS).select("*").eq("estado", AP_ABIERTO).order("id").execute().data or []
    except Exception:
        return None


def piezas_de_apartados(filas, aps):
    """{id_apartado: [pendientes por pagar de ese apartado]} con las filas de `abiertos()`."""
    ids = {a["id"] for a in aps or []}
    out = {i: [] for i in ids}
    for f in filas or []:
        if f.get("estado") == PENDIENTE and f.get("apartado_id") in ids:
            out[f["apartado_id"]].append(f)
    return out


def apartado_por_cuenta(sb):
    """{(cuenta, medio): monto apartado} — lo que hay que restar para saber lo LIBRE de cada cuenta."""
    aps = apartados_abiertos(sb)
    if not aps:
        return {}
    piezas = piezas_de_apartados(abiertos(sb), aps)
    out = {}
    for a in aps:
        k = (a["cuenta"], a["medio"])
        out[k] = round(out.get(k, 0.0) + sum(_num(f.get("monto")) for f in piezas.get(a["id"], [])), 2)
    return out


def _libres_para_apartar(sb, ids):
    filas = sb.table(TABLA).select("*").in_("id", list(ids)).eq("estado", PENDIENTE).execute().data or []
    abiertos_ids = {a["id"] for a in (apartados_abiertos(sb) or [])}
    ocupadas = [f for f in filas if f.get("apartado_id") in abiertos_ids]
    if ocupadas:
        raise ValueError("Mod " + ", ".join(str(f.get("modelo")) for f in ocupadas) + " ya está en otro apartado.")
    if not filas:
        raise ValueError("Solo se aparta lo que ya está «por pagar» (vendido). Nada de lo elegido lo está.")
    return filas


def crear_apartado(sb, ids, nombre, cuenta, medio, usuario):
    """Nuevo apartado con los pendientes `ids` (solo «por pagar»). Devuelve (apartado, n, total)."""
    import caudal
    cuenta, medio = caudal.normalizar(cuenta, medio)
    filas = _libres_para_apartar(sb, ids)
    nombre = " ".join(str(nombre or "").split()) or f"Apartado {_hoy():%d/%m/%Y}"
    ap = sb.table(APARTADOS).insert({"nombre": nombre, "cuenta": cuenta, "medio": medio, "estado": AP_ABIERTO,
                                     "usuario": usuario}).execute().data[0]
    for f in filas:
        sb.table(TABLA).update({"apartado_id": ap["id"], "actualizado": _ahora()}).eq("id", f["id"]).execute()
    total = round(sum(_num(f.get("monto")) for f in filas), 2)
    codigos.registrar(sb, usuario, "Creó apartado", "caudal", nombre,
                      f"{cuenta} · {medio} · {len(filas)} pieza(s) · ${total:,.2f}")
    _limpiar()
    return ap, len(filas), total


def agregar_a_apartado(sb, apartado, ids, usuario):
    filas = _libres_para_apartar(sb, ids)
    for f in filas:
        sb.table(TABLA).update({"apartado_id": apartado["id"], "actualizado": _ahora()}).eq("id", f["id"]).execute()
    total = round(sum(_num(f.get("monto")) for f in filas), 2)
    codigos.registrar(sb, usuario, "Agregó al apartado", "caudal", apartado.get("nombre"),
                      f"{len(filas)} pieza(s) · ${total:,.2f}")
    _limpiar()
    return len(filas), total


def sacar_de_apartado(sb, pend, usuario):
    sb.table(TABLA).update({"apartado_id": None, "actualizado": _ahora()}).eq("id", pend["id"]) \
        .eq("estado", PENDIENTE).execute()
    codigos.registrar(sb, usuario, "Sacó del apartado", "caudal", str(pend.get("apartado_id")),
                      f"Mod {pend.get('modelo')} · ${_num(pend.get('monto')):,.2f}")
    _limpiar()


def deshacer_apartado(sb, apartado, usuario):
    """Libera la plata: lo que faltaba pagar vuelve a pendientes sueltos (lo ya pagado queda pagado)."""
    sb.table(TABLA).update({"apartado_id": None, "actualizado": _ahora()}).eq("apartado_id", apartado["id"]) \
        .eq("estado", PENDIENTE).execute()
    sb.table(APARTADOS).update({"estado": AP_DESHECHO, "cerrado_en": _ahora(), "cerrado_por": usuario}) \
        .eq("id", apartado["id"]).execute()
    codigos.registrar(sb, usuario, "Deshizo apartado", "caudal", apartado.get("nombre"),
                      f"{apartado.get('cuenta')} · {apartado.get('medio')}")
    _limpiar()


def _cerrar_si_pagado(sb, ap_id, usuario):
    """Si ya no le queda nada por pagar, el apartado queda «pagado»."""
    falta = sb.table(TABLA).select("id").eq("apartado_id", ap_id).eq("estado", PENDIENTE).limit(1).execute().data
    if not falta:
        sb.table(APARTADOS).update({"estado": AP_PAGADO, "cerrado_en": _ahora(), "cerrado_por": usuario}) \
            .eq("id", ap_id).eq("estado", AP_ABIERTO).execute()


def cambiar_costo(sb, pend, nuevo, usuario):
    """Cambia lo que se le paga al consignador. También corrige el costo de la ficha (COSTO_CONSIGNACION y
    TOTAL_COSTO) y, si se vendió en un pedido, el costo de ese renglón y del pedido (la utilidad queda bien).
    Devuelve el texto de lo que se hizo."""
    nuevo = round(_num(nuevo), 2)
    if nuevo < 0:
        raise ValueError("El costo no puede ser negativo.")
    actual = sb.table(TABLA).select("*").eq("id", pend["id"]).limit(1).execute().data
    if not actual or actual[0].get("estado") not in (EN_CONSIG, PENDIENTE):
        raise ValueError("Esa consignación ya no está pendiente (¿ya se pagó?).")
    actual = actual[0]
    viejo = round(_num(actual.get("monto")), 2)
    delta = round(nuevo - viejo, 2)
    if not delta:
        raise ValueError("Es el mismo costo.")
    modelo = str(actual.get("modelo"))
    hechos = [f"pendiente {dinero(viejo)} → {dinero(nuevo)}"]
    sb.table(TABLA).update({"monto": nuevo, "actualizado": _ahora()}).eq("id", actual["id"]).execute()
    r = sb.table("productos").select("datos_json").eq("MODELO", modelo).limit(1).execute().data
    if r:
        d = _parse(r[0].get("datos_json"))
        d["COSTO_CONSIGNACION"] = nuevo
        d["TOTAL_COSTO"] = round(_num(d.get("TOTAL_COSTO")) + delta, 2)
        sb.table("productos").update({"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", modelo).execute()
        hechos.append("ficha")
    num = actual.get("numero_pedido")
    if num:
        its = (sb.table("pedido_items").select("id,cantidad,costo_unitario").eq("numero_pedido", num)
               .eq("modelo", modelo).execute().data or [])
        if its:
            it = its[0]
            cant = max(int(_num(it.get("cantidad"), 1.0)), 1)
            sb.table("pedido_items").update({"costo_unitario": round(_num(it.get("costo_unitario")) + delta / cant, 2)}) \
                .eq("id", it["id"]).execute()
            ped = sb.table("pedidos").select("costo_total").eq("numero_pedido", num).execute().data
            if ped:
                sb.table("pedidos").update({"costo_total": round(_num(ped[0].get("costo_total")) + delta, 2)}) \
                    .eq("numero_pedido", num).execute()
            hechos.append(f"pedido {num}")
    texto = f"Mod {modelo} ({actual.get('proveedor')}): " + ", ".join(hechos)
    codigos.registrar(sb, usuario, "Cambió costo de consignación", "producto", modelo, texto)
    _limpiar()
    try:
        import caches
        caches.limpiar()
    except Exception:
        pass
    return texto


# ----------------------------------------------------------------------
# Pantallas
# ----------------------------------------------------------------------
def _ir_a_pendientes():
    ss = st.session_state
    ss["nav_principal"] = ss["nav_principal__ultimo"] = "Caudal"
    ss["nav_caudal"] = ss["nav_caudal__ultimo"] = "Pendientes por pagar"


def render_aviso(sb, user):
    """Aviso arriba de la app: consignaciones vendidas por pagar (con «Cambiar costo»)."""
    try:
        TAREA.obtener(sb)                       # revisa ventas en segundo plano (no hace esperar)
    except Exception:
        pass
    filas = abiertos(sb)
    if not filas:
        return
    pend = [f for f in filas if f.get("tipo") == "consignacion" and f.get("estado") == PENDIENTE]
    if not pend:
        return
    total = sum(_num(f.get("monto")) for f in pend)
    st.warning(f"💰 {len(pend)} consignación(es) vendida(s) por pagar · {dinero(total)}")
    with st.expander("Ver consignaciones por pagar"):
        st.button("Consignaciones pendientes por pagar →", key="pend_ir", on_click=_ir_a_pendientes,
                  help="Abre Caudal → Pendientes por pagar para pagarlas.")
        for f in pend[:40]:
            venta = (dinero(_num(f.get("precio_venta"))) if f.get("precio_venta") is not None else "—")
            c1, c2 = st.columns([5, 2])
            c1.markdown(f"**Consignación a pagar: {f.get('proveedor') or '—'}**  \n"
                        f"Mod {f.get('modelo')} · {f.get('detalle') or ''}  \n"
                        f"Precio (redondeo showroom) **{dinero(_num(f.get('precio_lista')))}** · "
                        f"Costo base **{dinero(_num(f.get('monto')))}** · "
                        f"Se vendió en **{venta}** ({f.get('vendido_en') or 'vendido'})")
            pop = getattr(c2, "popover", None)
            caja = pop("Cambiar costo", use_container_width=True) if pop else c2.expander("Cambiar costo")
            with caja:
                nuevo = st.number_input("Nuevo costo base ($)", min_value=0.0, value=float(_num(f.get("monto"))),
                                        step=100.0, format="%.2f", key=f"pend_cc_{f['id']}")
                if st.button("Guardar costo", key=f"pend_cc_ok_{f['id']}", type="primary"):
                    try:
                        texto = cambiar_costo(sb, f, nuevo, user.get("nombre") or user.get("role"))
                        st.session_state["pend_aviso_msg"] = f"Costo cambiado: {texto}"
                        st.rerun()
                    except ValueError as ex:
                        st.error(str(ex))
                    except Exception as ex:
                        st.error(f"No se pudo cambiar: {ex}")
        if len(pend) > 40:
            st.caption(f"Se muestran 40 de {len(pend)}.")
    msg = st.session_state.pop("pend_aviso_msg", None)
    if msg:
        st.success(msg)


def _limpiar_seleccion():
    ss = st.session_state
    for k in list(ss.keys()):
        if str(k).startswith(("pend_sel_", "pend_todo_", "ap_sel_", "ap_todo_")):
            ss.pop(k, None)
    for k in ("pend_cuenta", "pend_ap_nombre", "pend_ap_cuenta", "pend_ap_existente"):
        ss.pop(k, None)


def _ok(msg):
    _limpiar_seleccion()
    st.session_state["pend_msgs"] = [("ok", msg)]
    st.rerun()


def _render_nuevo(sb, q_v, q_c):
    """➕ Nuevo pendiente por pagar (a mano)."""
    ss = st.session_state
    with st.expander("➕ Nuevo pendiente por pagar", expanded=bool(ss.get("pend_nv_abierto"))):
        st.caption("Para cualquier deuda que no sale sola de una ficha (p. ej. un servicio, un préstamo, una pieza "
                   "sin ficha). Después se paga o se aparta como los demás.")
        n1, n2 = st.columns(2)
        prov = n1.text_input("¿A quién se le debe? *", key="pend_nv_prov", placeholder="Nombre")
        sug = [x for x in sugerir(consignadores(sb), prov) if clave_nombre(x) != clave_nombre(prov)]
        if sug:
            n1.caption("¿Es " + " / ".join(f"**{x}**" for x in sug) + "? Escríbelo igual para que se agrupe.")
        monto = n2.number_input("Monto ($) *", min_value=0.0, step=500.0, format="%.2f", key="pend_nv_monto")
        n3, n4 = st.columns([3, 1])
        concepto = n3.text_input("Concepto *", key="pend_nv_concepto", placeholder="Qué se le debe")
        modelo = n4.text_input("Modelo (opcional)", key="pend_nv_modelo")
        if st.button("Crear pendiente", type="primary", key="pend_nv_ok"):
            ss["pend_nv_abierto"] = True
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    crear_manual(sb, prov, concepto, monto, modelo, quien)
                except ValueError as ex:
                    st.error(str(ex))
                except Exception as ex:
                    st.error(f"No se pudo crear: {ex}")
                else:
                    for k in ("pend_nv_prov", "pend_nv_monto", "pend_nv_concepto", "pend_nv_modelo", "pend_nv_abierto"):
                        ss.pop(k, None)
                    _ok(f"Pendiente creado: {prov.strip()} · {concepto.strip()} · {dinero(_num(monto))}.")


def _render_apartados(sb, aps, piezas, q_v, q_c):
    """Cada apartado abierto: sus piezas por pagar, pagar (todo o parte), sacar una pieza, deshacer."""
    ss = st.session_state
    st.subheader("Apartados")
    st.caption("Plata separada en una cuenta para pagar estas piezas. En la tarjeta de la cuenta ya se ve como "
               "apartada (no libre). Se puede pagar todo junto o de a partes.")
    for a in aps:
        ps = piezas.get(a["id"], [])
        total = sum(_num(f.get("monto")) for f in ps)
        titulo = f"📦 {a['nombre']} — {a['cuenta']} · {a['medio']} — {dinero(total)} ({len(ps)} por pagar)"
        with st.expander(titulo, expanded=len(aps) <= 3):
            if not ps:
                st.caption("Este apartado se quedó sin piezas por pagar (se sacaron o dejaron de estar vendidas).")
            elif st.checkbox("Elegir todas", key=f"ap_todo_{a['id']}"):
                for f in ps:
                    ss[f"ap_sel_{f['id']}"] = True
            elegidas = []
            for f in ps:
                c1, c2 = st.columns([6, 1])
                venta = (f" · se vendió en {dinero(_num(f.get('precio_venta')))}"
                         if f.get("precio_venta") is not None else "")
                if c1.checkbox(f"{_mod(f)}{NOMBRE_TIPO.get(f.get('tipo'), f.get('tipo'))} · "
                               f"{f.get('proveedor') or ''} · {f.get('detalle') or ''} — "
                               f"**{dinero(_num(f.get('monto')))}**{venta}", key=f"ap_sel_{f['id']}"):
                    elegidas.append(f)
                if c2.button("Sacar", key=f"ap_sacar_{f['id']}", help="Quitar esta pieza del apartado (vuelve a "
                                                                      "pendientes sueltos y libera su plata)."):
                    quien = codigos.confirmar(sb, q_v, q_c)
                    if quien:
                        sacar_de_apartado(sb, f, quien)
                        _ok(f"{_mod(f) or (f.get('detalle') or 'La pieza') + ' · '}salió del apartado «{a['nombre']}».")
            b1, b2, b3 = st.columns([2, 2, 2])
            fecha = b1.date_input("Fecha de pago", value=_hoy(), key=f"ap_fecha_{a['id']}", format="DD/MM/YYYY")
            tot_sel = sum(_num(f.get("monto")) for f in elegidas)
            b2.write("")
            if b2.button(f"Pagar {len(elegidas)} · {dinero(tot_sel)}" if elegidas else "Pagar",
                         type="primary", key=f"ap_pagar_{a['id']}", disabled=not elegidas,
                         use_container_width=True, help=None if elegidas else "Marca lo que vas a pagar."):
                quien = codigos.confirmar(sb, q_v, q_c)
                if quien:
                    try:
                        n, tot = pagar(sb, [f["id"] for f in elegidas], None, None, fecha, quien, apartado=a)
                    except ValueError as ex:
                        st.error(str(ex))
                    except Exception as ex:
                        st.error(f"No se pudo pagar: {ex}")
                    else:
                        resto = len(ps) - n
                        _ok(f"Pagado desde el apartado «{a['nombre']}»: {n} pieza(s) por {dinero(tot)}. Ya aparece "
                            "en rojo en Movimientos." + (f" Quedan {resto} por pagar en el apartado." if resto
                                                         else " El apartado quedó pagado."))
            b3.write("")
            with b3.popover("Deshacer apartado", use_container_width=True):
                st.caption("Libera la plata. Lo que falta pagar vuelve a pendientes sueltos; lo ya pagado queda pagado.")
                if st.button("Sí, deshacer", key=f"ap_deshacer_{a['id']}"):
                    quien = codigos.confirmar(sb, q_v, q_c)
                    if quien:
                        deshacer_apartado(sb, a, quien)
                        _ok(f"Apartado «{a['nombre']}» deshecho: la plata quedó libre en {a['cuenta']}.")
    st.divider()


def render_lista(sb, user, q_v, q_c, saldos=None):
    """Caudal → Pendientes por pagar: apartados arriba; abajo agrupado por consignador / proveedor, con
    «Pagar seleccionados» o «Apartar». `saldos` = saldos de Caudal (para avisar si no alcanza lo libre)."""
    import caudal
    ss = st.session_state
    try:
        revisar_consignaciones(sb)               # al momento: que la lista esté al día
        _limpiar()
        filas = _abiertos_mem(sb)
    except Exception as ex:
        st.error(f"No se pudo leer la lista de pendientes. ¿Ya corriste en Supabase el SQL de pendientes? ({ex})")
        with st.expander("SQL para crear la tabla"):
            st.code(PENDIENTES_SQL, language="sql")
        return
    for m in ss.pop("pend_msgs", []):
        (st.success if m[0] == "ok" else st.warning)(m[1])

    aps = apartados_abiertos(sb)
    hay_apartados = aps is not None
    aps = aps or []
    piezas = piezas_de_apartados(filas, aps)
    en_apartado = {f["id"] for ps in piezas.values() for f in ps}
    sueltas = [f for f in filas if f["id"] not in en_apartado]

    grupos = {}
    for f in sueltas:
        g = grupos.setdefault(clave_nombre(f.get("proveedor")) or "(SIN NOMBRE)",
                              {"nombre": f.get("proveedor") or "(sin nombre)", "pend": [], "consig": []})
        (g["pend"] if f["estado"] == PENDIENTE else g["consig"]).append(f)
    total_pend = sum(_num(f.get("monto")) for f in sueltas if f["estado"] == PENDIENTE)
    total_ap = sum(_num(f.get("monto")) for ps in piezas.values() for f in ps)
    total_consig = sum(_num(f.get("monto")) for f in filas if f["estado"] == EN_CONSIG)
    m1, m2, m3 = st.columns(3)
    m1.metric("POR PAGAR (sin apartar)", dinero(total_pend),
              help="Consignaciones ya vendidas y costos de las fichas marcados «Pendiente por pagar» que no están "
                   "en un apartado.")
    m2.metric("APARTADO", dinero(total_ap), help="Por pagar con la plata ya separada en un apartado.")
    m3.metric("EN CONSIGNACIÓN (aún sin vender)", dinero(total_consig),
              help="Lo que se le debería a los consignadores si se vendieran las piezas que siguen aquí.")
    _render_nuevo(sb, q_v, q_c)
    if aps:
        _render_apartados(sb, aps, piezas, q_v, q_c)
    if not grupos:
        if not aps:
            st.info("No hay pendientes. Aquí aparecen las consignaciones (ficha → sección 2), los costos de la ficha "
                    "marcados «Pendiente por pagar» y los que se crean a mano.")
        return

    elegidos = []
    for clave, g in sorted(grupos.items(), key=lambda kv: (-sum(_num(f.get("monto")) for f in kv[1]["pend"]), kv[0])):
        debe = sum(_num(f.get("monto")) for f in g["pend"])
        titulo = (f"{g['nombre']} — debe {dinero(debe)} ({len(g['pend'])} por pagar)"
                  + (f" · {len(g['consig'])} en consignación" if g["consig"] else ""))
        with st.expander(titulo, expanded=bool(g["pend"]) and len(grupos) <= 6):
            if g["pend"] and st.checkbox("Elegir todas las por pagar de este grupo", key=f"pend_todo_{clave}"):
                for f in g["pend"]:
                    ss[f"pend_sel_{f['id']}"] = True
            for f in g["pend"]:
                venta = (f" · se vendió en {dinero(_num(f.get('precio_venta')))}"
                         if f.get("precio_venta") is not None else "")
                donde = f" ({f.get('vendido_en') or 'vendido'})" if f.get("tipo") == "consignacion" else ""
                fila = st.columns([6, 1]) if f.get("tipo") == "manual" else [st]
                if fila[0].checkbox(f"{_mod(f)}{NOMBRE_TIPO.get(f.get('tipo'), f.get('tipo'))} · "
                                    f"{f.get('detalle') or ''} — **{dinero(_num(f.get('monto')))}**{venta}{donde}",
                                    key=f"pend_sel_{f['id']}"):
                    elegidos.append(f)
                if f.get("tipo") == "manual" and fila[1].button("Quitar", key=f"pend_quitar_{f['id']}",
                                                                 help="Borrar este pendiente (queda en el registro)."):
                    quien = codigos.confirmar(sb, q_v, q_c)
                    if quien:
                        try:
                            quitar_manual(sb, f, quien)
                        except ValueError as ex:
                            st.error(str(ex))
                        else:
                            _ok(f"Se quitó el pendiente «{f.get('detalle')}» de {f.get('proveedor')}.")
            if g["consig"]:
                st.caption("En consignación (todavía no se venden; no se pueden pagar ni apartar):")
                for f in g["consig"]:
                    st.caption(f"· Mod {f.get('modelo')} · {f.get('detalle') or ''} — costo base "
                               f"{dinero(_num(f.get('monto')))}"
                               + (f" · recibida {f.get('fecha_recepcion')}" if f.get("fecha_recepcion") else ""))

    st.divider()
    total_sel = sum(_num(f.get("monto")) for f in elegidos)
    if not elegidos:
        st.subheader("Pagar o apartar")
        st.caption("Marca arriba lo que vas a pagar o apartar.")
        return
    st.markdown(f"**{len(elegidos)}** seleccionado(s) · total **{dinero(total_sel)}**")
    accion = st.radio("¿Qué hago con lo seleccionado?", ["Pagar ahora", "Apartar la plata"], horizontal=True,
                      key="pend_accion")
    if accion == "Pagar ahora":
        p1, p2 = st.columns(2)
        desde = p1.selectbox("¿Desde qué cuenta se pagó?", caudal._opciones_cuenta(), index=None, key="pend_cuenta",
                             placeholder="Elegir cuenta…")
        fecha = p2.date_input("Fecha de pago", value=_hoy(), key="pend_fecha", format="DD/MM/YYYY")
        if st.button(f"Pagar {len(elegidos)} · {dinero(total_sel)}", type="primary", key="pend_pagar",
                     disabled=desde is None, help=None if desde else "Elige la cuenta."):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    n, tot = pagar(sb, [f["id"] for f in elegidos], *caudal._partir(desde), fecha, quien)
                except ValueError as ex:
                    st.error(str(ex))
                    return
                except Exception as ex:
                    st.error(f"No se pudo pagar: {ex}")
                    return
                _ok(f"Pagado: {n} pendiente(s) por {dinero(tot)} desde {desde}. Ya aparece en rojo en Movimientos.")
        return

    # Apartar
    if not hay_apartados:
        st.error("Falta crear en Supabase la tabla de apartados. Corre este SQL una vez:")
        st.code(APARTADOS_SQL, language="sql")
        return
    modo = st.radio("Apartado", ["Nuevo apartado", "Agregar a uno que ya existe"] if aps else ["Nuevo apartado"],
                    horizontal=True, key="pend_ap_modo", label_visibility="collapsed")
    apartados_por_cuenta = {}
    for a in aps:
        k = (a["cuenta"], a["medio"])
        apartados_por_cuenta[k] = apartados_por_cuenta.get(k, 0.0) + sum(
            _num(f.get("monto")) for f in piezas.get(a["id"], []))

    def _aviso_libre(cuenta, medio):
        if saldos is None or cuenta not in saldos:
            return
        libre = saldos[cuenta].get(medio, 0.0) - apartados_por_cuenta.get((cuenta, medio), 0.0)
        if total_sel > libre + 0.004:
            st.warning(f"⚠️ En {cuenta} · {medio} hay {dinero(libre)} libres y vas a apartar {dinero(total_sel)}: "
                       "no alcanza. Puedes apartar igual (quedará en rojo lo libre).")
        else:
            st.caption(f"Libre en {cuenta} · {medio}: {dinero(libre)} → después de apartar: "
                       f"{dinero(libre - total_sel)}")

    if modo == "Nuevo apartado":
        a1, a2 = st.columns(2)
        nombre = a1.text_input("Nombre del apartado", key="pend_ap_nombre",
                               placeholder="Consignaciones de diciembre")
        desde = a2.selectbox("¿De qué cuenta se separa la plata?", caudal._opciones_cuenta(), index=None,
                             key="pend_ap_cuenta", placeholder="Elegir cuenta…")
        if desde:
            _aviso_libre(*caudal._partir(desde))
        if st.button(f"Apartar {len(elegidos)} · {dinero(total_sel)}", type="primary", key="pend_apartar",
                     disabled=desde is None, help=None if desde else "Elige la cuenta."):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    ap, n, tot = crear_apartado(sb, [f["id"] for f in elegidos], nombre, *caudal._partir(desde), quien)
                except ValueError as ex:
                    st.error(str(ex))
                    return
                except Exception as ex:
                    st.error(f"No se pudo apartar: {ex}")
                    return
                _ok(f"Apartado «{ap['nombre']}»: {n} pieza(s) por {dinero(tot)} separados de {desde}.")
    else:
        etiquetas = {a["id"]: f"{a['nombre']} · {a['cuenta']} · {a['medio']}" for a in aps}
        sel = st.selectbox("Apartado", list(etiquetas), format_func=etiquetas.get, index=None,
                           key="pend_ap_existente", placeholder="Elegir apartado…")
        if sel is not None:
            a = next(x for x in aps if x["id"] == sel)
            _aviso_libre(a["cuenta"], a["medio"])
        if st.button(f"Agregar {len(elegidos)} · {dinero(total_sel)} al apartado", type="primary",
                     key="pend_ap_agregar", disabled=sel is None):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                a = next(x for x in aps if x["id"] == sel)
                try:
                    n, tot = agregar_a_apartado(sb, a, [f["id"] for f in elegidos], quien)
                except ValueError as ex:
                    st.error(str(ex))
                    return
                except Exception as ex:
                    st.error(f"No se pudo agregar: {ex}")
                    return
                _ok(f"Agregadas {n} pieza(s) por {dinero(tot)} al apartado «{a['nombre']}».")
