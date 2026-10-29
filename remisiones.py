"""
Remisiones.

Dos tipos, en una sola lista:

  * DE PEDIDO: se genera desde el detalle del pedido («Generar remisión»). Toma cliente y productos del pedido;
    cada objeto tiene foto, nombre, descripción, observaciones, estado (Pendiente, En restauro, Listo para
    entregar, Entregado, Devuelto) y fecha de entrega. Cuando todo está entregado, el pedido pasa solo a
    «Pagado y entregado» (o «Entregado pendiente de pago» si tiene saldo). Si cambian los productos del pedido,
    la remisión agrega los nuevos y avisa de los que se quitaron. El PDF no lleva precios.

  * ABIERTA: cuenta abierta con un cliente que se lleva, aparta, paga o devuelve piezas de a poco.
      - Apartado / Se lo llevó · pendiente de pago / Se lo llevó · pagado → RESERVADO en el inventario (sin vencer).
      - Devuelto → vuelve a DISPONIBLE.
      - Los pagos se registran tal cual llegan; «Pasar a pedido lo pagado» arma un pedido con las piezas pagadas
        (quedan VENDIDAS) y lo que sobra queda como saldo a favor para la próxima vez.
      - Precio por pieza con la misma regla de autorización (precios.py). El PDF lleva precios y totales.

Tablas (ver REMISIONES_SQL más abajo).
"""
import datetime as dt
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

import streamlit as st

import codigos
import precios
import reservas
from permisos import roles as _roles
from navegacion import descargar

ROLES_REMISIONES = _roles("remisiones")
MAX_DESC = 700

ESTADOS_PEDIDO = ("Pendiente", "En restauro", "Listo para entregar", "Entregado", "Devuelto")
ENTREGADO, DEVUELTO = "Entregado", "Devuelto"
APARTADO = "Apartado"
LLEVO_PEND = "Se lo llevó · pendiente de pago"
LLEVO_PAG = "Se lo llevó · pagado"
ESTADOS_ABIERTA = (APARTADO, LLEVO_PEND, LLEVO_PAG, DEVUELTO)

REMISIONES_SQL = """
create table if not exists remisiones (
  id bigserial primary key,
  creado timestamptz not null default now(),
  tipo text not null default 'pedido',          -- 'pedido' | 'abierta'
  numero_pedido bigint,                          -- solo las de pedido
  cliente text, vendedor text, fecha date,
  estado text not null default 'Abierta',        -- 'Abierta' | 'Cerrada'
  created_by text
);
create table if not exists remision_items (
  id bigserial primary key,
  remision_id bigint not null references remisiones(id) on delete cascade,
  orden int default 0,
  modelo text, es_sn boolean default false,
  nombre text, descripcion text, foto_url text, observaciones text,
  estado text, fecha_entrega date,
  cantidad int default 1,
  precio_unitario numeric default 0, descuento_pct numeric default 0,
  costo_unitario numeric default 0, utilidad_bruta_inv numeric default 0,
  reservado int default 0,                       -- abierta: unidades descontadas al apartar
  numero_pedido bigint                           -- abierta: pedido al que pasó la pieza pagada
);
create table if not exists remision_pagos (
  id bigserial primary key,
  remision_id bigint not null references remisiones(id) on delete cascade,
  creado timestamptz not null default now(),
  fecha date, monto numeric not null, forma_pago text, banco_caja text,
  aplicado numeric not null default 0,           -- lo que ya se usó para pagar pedidos
  usuario text
);
"""


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _num(v, d=0.0):
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _hoy():
    from cotizaciones import hoy_mx
    return hoy_mx()


def _nn(v):
    v = (v or "").strip() if isinstance(v, str) else v
    return v or None


def _fecha(v):
    if not v:
        return None
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _info(sb, modelo):
    """Inventario: nombre, descripción, foto (si existe), precio, costo, utilidad y disponibilidad. None si no existe."""
    from ventas import _parse
    from cotizaciones import _url_foto_inventario, _foto_existe
    r = sb.table("productos").select("MODELO,datos_json").eq("MODELO", str(modelo)).limit(1).execute()
    if not r.data:
        return None
    d = _parse(r.data[0].get("datos_json"))
    m = str(r.data[0]["MODELO"])
    url = _url_foto_inventario(sb, m)
    try:
        hay_foto = _foto_existe(url)
    except Exception:
        hay_foto = False
    return {"modelo": m, "nombre": " ".join(str(d.get("NOMBRE", "") or "").split()) or "Sin nombre",
            "descripcion": str(d.get("DESCRIPCION", "") or "").strip(),
            "foto_url": url if hay_foto else None,
            "precio": _num(d.get("REDONDEO_SHOWROOM")),
            "costo": _num(d.get("TOTAL_COSTO", d.get("COSTO_REGISTRADO"))),
            "utilidad_bruta": _num(d.get("UTILIDAD_BRUTA", d.get("UTILIDAD_SHOWROOM"))),
            "disp": reservas.estado_disp(d)}


def _subir_foto(sb, rid, datos):
    from cotizaciones import _foto_a_jpeg, _url_foto_inventario
    ruta = f"remisiones/{rid}/{uuid.uuid4().hex[:10]}.jpg"
    sb.storage.from_("imagenes").upload(path=ruta, file=_foto_a_jpeg(datos),
                                        file_options={"content-type": "image/jpeg", "upsert": "true"})
    return f"{_url_foto_inventario(sb, 'x').rsplit('/', 1)[0]}/{ruta}"


def _linea(it):
    """Total del renglón (con descuento), como en los pedidos."""
    from ventas import calcular
    return calcular([{"precio_unitario": _num(it.get("precio_unitario")), "cantidad": int(_num(it.get("cantidad"), 1)),
                      "descuento_pct": _num(it.get("descuento_pct")), "costo_unitario": 0.0}],
                    0.0, False, 0.0)["lineas"][0]


def folio(rem):
    return f"Pedido {rem['numero_pedido']}" if rem.get("tipo") == "pedido" else f"R-{rem['id']}"


# ----------------------------------------------------------------------
# Lectura
# ----------------------------------------------------------------------
def leer(sb, rid):
    rem = sb.table("remisiones").select("*").eq("id", rid).execute().data
    if not rem:
        return None, [], []
    items = sb.table("remision_items").select("*").eq("remision_id", rid).order("orden").order("id").execute().data or []
    pagos = []
    if rem[0].get("tipo") == "abierta":
        pagos = sb.table("remision_pagos").select("*").eq("remision_id", rid).order("id").execute().data or []
    return rem[0], items, pagos


def remision_de_pedido(sb, numero):
    r = (sb.table("remisiones").select("id").eq("tipo", "pedido").eq("numero_pedido", int(numero))
         .limit(1).execute().data)
    return r[0]["id"] if r else None


def saldo_favor(pagos):
    return round(sum(_num(p.get("monto")) - _num(p.get("aplicado")) for p in pagos), 2)


def resumen_abierta(items, pagos):
    """Totales de una remisión abierta."""
    r = {"llevo": 0, "llevo_pag": 0.0, "llevo_pend": 0.0, "apartado": 0.0, "n_apartado": 0, "n_devuelto": 0,
         "n_pag": 0, "n_pend": 0, "pagado_sin_pedido": 0.0, "n_pag_sin_pedido": 0}
    for it in items:
        e, t = it.get("estado"), _linea(it)
        if e == LLEVO_PAG:
            r["llevo"] += 1
            r["n_pag"] += 1
            r["llevo_pag"] += t
            if not it.get("numero_pedido"):
                r["pagado_sin_pedido"] += t
                r["n_pag_sin_pedido"] += 1
        elif e == LLEVO_PEND:
            r["llevo"] += 1
            r["n_pend"] += 1
            r["llevo_pend"] += t
        elif e == APARTADO:
            r["n_apartado"] += 1
            r["apartado"] += t
        elif e == DEVUELTO:
            r["n_devuelto"] += 1
    r["saldo_favor"] = saldo_favor(pagos)
    r["debe"] = round(r["llevo_pend"] + r["pagado_sin_pedido"] - r["saldo_favor"], 2)
    return r


def reservado_en_remision(sb, modelo):
    """Cliente de la remisión abierta que tiene reservado el modelo ("" si ninguna)."""
    try:
        its = (sb.table("remision_items").select("remision_id,estado,numero_pedido")
               .eq("modelo", str(modelo)).execute().data or [])
        ids = list({i["remision_id"] for i in its if i.get("estado") in (APARTADO, LLEVO_PEND, LLEVO_PAG)
                    and not i.get("numero_pedido")})
        if not ids:
            return ""
        rems = (sb.table("remisiones").select("cliente").in_("id", ids).eq("tipo", "abierta")
                .eq("estado", "Abierta").limit(1).execute().data or [])
        return str(rems[0].get("cliente") or "").strip() if rems else ""
    except Exception:
        return ""


# ----------------------------------------------------------------------
# Remisión de pedido
# ----------------------------------------------------------------------
def _clave(modelo, nombre):
    return f"m:{modelo}" if modelo else f"s:{str(nombre or '').strip().lower()}"


def _fila_de_pedido(sb, rid, orden, it):
    modelo = str(it.get("modelo") or "").strip() or None
    info = _info(sb, modelo) if modelo else None
    return {"remision_id": rid, "orden": orden, "modelo": modelo, "es_sn": not modelo,
            "nombre": (it.get("descripcion") or (info or {}).get("nombre") or "Sin nombre"),
            "descripcion": (info or {}).get("descripcion") or "", "foto_url": (info or {}).get("foto_url"),
            "cantidad": int(_num(it.get("cantidad"), 1)), "precio_unitario": _num(it.get("precio_unitario")),
            "descuento_pct": _num(it.get("descuento_pct")), "estado": "Pendiente"}


def crear_de_pedido(sb, numero, usuario):
    """Crea la remisión del pedido (si ya existe devuelve la misma). Devuelve el id."""
    existe = remision_de_pedido(sb, numero)
    if existe:
        return existe
    ped = sb.table("pedidos").select("*").eq("numero_pedido", int(numero)).execute().data[0]
    items = sb.table("pedido_items").select("*").eq("numero_pedido", int(numero)).order("id").execute().data or []
    vends = sb.table("pedido_vendedores").select("vendedor").eq("numero_pedido", int(numero)).execute().data or []
    rem = sb.table("remisiones").insert({
        "tipo": "pedido", "estado": "Abierta", "numero_pedido": int(numero), "cliente": ped.get("cliente"),
        "vendedor": " / ".join(v["vendedor"] for v in vends) or None, "fecha": _hoy().isoformat(),
        "created_by": usuario}).execute().data[0]
    if items:
        sb.table("remision_items").insert([_fila_de_pedido(sb, rem["id"], n, it) for n, it in enumerate(items)]).execute()
    codigos.registrar(sb, usuario, "Generó remisión", "pedido", numero, f"{len(items)} objeto(s)")
    return rem["id"]


def sincronizar_con_pedido(sb, rem, items):
    """Agrega a la remisión los productos nuevos del pedido y quita los que ya no están. Devuelve (agregados, quitados)."""
    ped_items = (sb.table("pedido_items").select("*").eq("numero_pedido", int(rem["numero_pedido"]))
                 .order("id").execute().data or [])
    claves_ped = {_clave(i.get("modelo"), i.get("descripcion")): i for i in ped_items}
    claves_rem = {_clave(i.get("modelo"), i.get("nombre")): i for i in items}
    # Un S/N cuyo nombre se cambió en la remisión ya no coincide por nombre: se empareja por orden con los S/N sobrantes
    sn_ped = [k for k in claves_ped if k.startswith("s:") and k not in claves_rem]
    sn_rem = [k for k in claves_rem if k.startswith("s:") and k not in claves_ped]
    for kp, kr in zip(sn_ped, sn_rem):
        claves_rem[kp] = claves_rem.pop(kr)
    nuevos = [i for k, i in claves_ped.items() if k not in claves_rem]
    sobran = [i for k, i in claves_rem.items() if k not in claves_ped]
    if nuevos:
        base = max([_num(i.get("orden")) for i in items] + [-1]) + 1
        sb.table("remision_items").insert([_fila_de_pedido(sb, rem["id"], int(base) + n, it)
                                           for n, it in enumerate(nuevos)]).execute()
    if sobran:
        sb.table("remision_items").delete().in_("id", [i["id"] for i in sobran]).execute()
    return ([i.get("modelo") or i.get("descripcion") for i in nuevos],
            [(f"Modelo {i['modelo']}" if i.get("modelo") else i.get("nombre")) for i in sobran])


def actualizar_status_pedido(sb, rem, items, usuario):
    """Si todos los objetos están entregados (o devueltos), cambia las observaciones del pedido.
    Devuelve (mensaje o None, avisos)."""
    avisos = []
    if any(i.get("estado") == DEVUELTO for i in items):
        avisos.append("Hay objetos marcados como DEVUELTO: si ya no van en el pedido, quítalos en «Editar productos».")
    if not items or not all(i.get("estado") in (ENTREGADO, DEVUELTO) for i in items) \
            or not any(i.get("estado") == ENTREGADO for i in items):
        return None, avisos
    num = int(rem["numero_pedido"])
    ped = sb.table("pedidos").select("estado_pedido,status,total,anticipo,saldo").eq("numero_pedido", num).execute().data
    if not ped:
        return None, avisos
    ped = ped[0]
    if ped.get("estado_pedido") == "Cancelado" or ped.get("status") == "Cancelado":
        return None, avisos
    saldo = _num(ped.get("saldo"), _num(ped.get("total")) - _num(ped.get("anticipo")))
    objetivo = "pagado y entregado" if saldo <= 0.01 else "entregado pendiente de pago"
    from ventas import catalogo
    opcion = next((e for e in catalogo(sb, "estado_pedido") if str(e).strip().lower() == objetivo), None)
    if not opcion:
        avisos.append(f"Todo está entregado, pero no encontré la opción «{objetivo}» en las observaciones del pedido.")
        return None, avisos
    if ped.get("estado_pedido") == opcion:
        return None, avisos
    cambios = {"estado_pedido": opcion}
    if objetivo == "pagado y entregado":
        cambios["status"] = "Pagado"
    sb.table("pedidos").update(cambios).eq("numero_pedido", num).execute()
    codigos.registrar(sb, usuario, "Cambió observaciones (remisión)", "pedido", num,
                      f"{ped.get('estado_pedido')} → {opcion} (todo entregado)")
    return f"Todos los objetos están entregados: el pedido {num} pasó a «{opcion}».", avisos


# ----------------------------------------------------------------------
# Guardar cambios de los renglones (los dos tipos)
# ----------------------------------------------------------------------
def guardar_items(sb, rem, items, cambios, fotos, usuario, permitir_precio=False, autorizo=None):
    """cambios = {id: {nombre, descripcion, observaciones, estado, fecha_entrega, precio_unitario, descuento_pct}}
    fotos = {id: bytes}. En las abiertas aplica el efecto en el inventario de cada cambio de estado.
    Devuelve (avisos, cambios_precio). Lanza ValueError sin cambiar nada si algo no es válido."""
    abierta = rem.get("tipo") == "abierta"
    por_id = {i["id"]: i for i in items}
    cambios_precio, avisos = [], []

    # 1) Validar precios (solo abiertas, piezas que no pasaron a pedido)
    if abierta:
        for iid, f in cambios.items():
            it = por_id[iid]
            if it.get("numero_pedido"):
                continue
            if not permitir_precio:
                f["precio_unitario"] = _num(it.get("precio_unitario"))
            c = precios.cambio(it.get("modelo"), f.get("nombre") or it.get("nombre"), it.get("precio_unitario"),
                               it.get("descuento_pct"), f.get("precio_unitario"), f.get("descuento_pct"))
            if c:
                cambios_precio.append(c)
        if cambios_precio and not autorizo:
            raise ValueError("Hay precios o descuentos de más de 10% que necesitan autorización: marca la casilla y "
                             "elige quién autorizó.")

    # 2) Abiertas: lo que vuelve de DEVUELTO se aparta otra vez (si no se puede, no se cambia nada)
    reapartados = []
    if abierta:
        for iid, f in cambios.items():
            it = por_id[iid]
            if it.get("numero_pedido") or it.get("estado") != DEVUELTO or f.get("estado") in (None, DEVUELTO):
                continue
            ok, msg, aviso, desc = reservas.reservar(sb, it["modelo"], int(_num(it.get("cantidad"), 1)))
            if not ok:
                for iid2, d2 in reapartados:
                    reservas.liberar(sb, por_id[iid2]["modelo"], d2)
                from ventas import texto_reservado
                raise ValueError(f"No se puede volver a apartar: "
                                 f"{texto_reservado(sb, it['modelo']) if 'reservado' in msg else msg}")
            reapartados.append((iid, desc))
            if aviso:
                avisos.append(aviso)
    reap = dict(reapartados)

    # 3) Escribir
    for iid, f in cambios.items():
        it = por_id[iid]
        datos = {"nombre": _nn(f.get("nombre")) or it.get("nombre"), "descripcion": _nn(f.get("descripcion")),
                 "observaciones": _nn(f.get("observaciones"))}
        if not (abierta and it.get("numero_pedido")):            # lo que ya pasó a pedido no cambia de estado
            if f.get("estado"):
                datos["estado"] = f["estado"]
            if abierta:
                datos["precio_unitario"] = round(_num(f.get("precio_unitario")), 2)
                datos["descuento_pct"] = min(max(_num(f.get("descuento_pct")), 0.0), 100.0)
        if "fecha_entrega" in f:
            fe = f.get("fecha_entrega")
            datos["fecha_entrega"] = fe.isoformat() if hasattr(fe, "isoformat") else (fe or None)
        if fotos.get(iid):
            datos["foto_url"] = _subir_foto(sb, rem["id"], fotos[iid])
        if abierta and not it.get("numero_pedido"):
            nuevo, viejo = datos.get("estado", it.get("estado")), it.get("estado")
            if viejo != DEVUELTO and nuevo == DEVUELTO and it.get("modelo"):
                a = reservas.liberar(sb, it["modelo"], int(_num(it.get("reservado"))))
                if a:
                    avisos.append(a)
                datos["reservado"] = 0
            elif iid in reap:
                datos["reservado"] = int(reap[iid])
        sb.table("remision_items").update(datos).eq("id", iid).execute()
    return avisos, cambios_precio


# ----------------------------------------------------------------------
# Remisión abierta
# ----------------------------------------------------------------------
def crear_abierta(sb, cliente, vendedor, fecha, usuario):
    rem = sb.table("remisiones").insert({
        "tipo": "abierta", "estado": "Abierta", "cliente": cliente.strip(), "vendedor": vendedor or None,
        "fecha": (fecha or _hoy()).isoformat(), "created_by": usuario}).execute().data[0]
    codigos.registrar(sb, usuario, "Creó remisión abierta", "remision", f"R-{rem['id']}", cliente.strip())
    return rem["id"]


def agregar_pieza(sb, rem, items, modelo, usuario):
    """Agrega un modelo a la remisión abierta y lo aparta. Devuelve avisos. Lanza ValueError."""
    from ventas import texto_reservado
    modelo = str(modelo).strip()
    if modelo.endswith(".0"):
        modelo = modelo[:-2]
    if any(str(i.get("modelo")) == modelo and i.get("estado") != DEVUELTO for i in items):
        raise ValueError(f"El modelo {modelo} ya está en esta remisión.")
    info = _info(sb, modelo)
    if info is None:
        raise ValueError(f"El modelo {modelo} no existe en el inventario.")
    if info["disp"] == "v":
        raise ValueError(f"El modelo {modelo} figura como VENDIDO.")
    if info["disp"] == "r":
        raise ValueError(texto_reservado(sb, modelo) + ".")
    ok, msg, aviso, desc = reservas.reservar(sb, modelo, 1)
    if not ok:
        raise ValueError(msg)
    try:
        sb.table("remision_items").insert({
            "remision_id": rem["id"], "orden": len(items), "modelo": info["modelo"], "es_sn": False,
            "nombre": info["nombre"], "descripcion": info["descripcion"], "foto_url": info["foto_url"],
            "estado": APARTADO, "cantidad": 1, "precio_unitario": info["precio"], "descuento_pct": 0,
            "costo_unitario": info["costo"], "utilidad_bruta_inv": info["utilidad_bruta"],
            "reservado": int(desc)}).execute()
    except Exception:
        reservas.liberar(sb, modelo, desc)
        raise
    codigos.registrar(sb, usuario, "Agregó pieza a remisión", "remision", f"R-{rem['id']}", modelo)
    avisos = [aviso] if aviso else []
    if info["precio"] <= 0:
        avisos.append(f"El modelo {modelo} no tiene precio en el inventario: ponérselo necesita autorización.")
    return avisos


def quitar_pieza(sb, rem, it, usuario):
    if it.get("numero_pedido"):
        raise ValueError("Esa pieza ya pasó a un pedido: no se puede quitar.")
    aviso = None
    if it.get("estado") != DEVUELTO and it.get("modelo"):
        aviso = reservas.liberar(sb, it["modelo"], int(_num(it.get("reservado"))))
    sb.table("remision_items").delete().eq("id", it["id"]).execute()
    codigos.registrar(sb, usuario, "Quitó pieza de remisión", "remision", f"R-{rem['id']}", it.get("modelo") or "")
    return aviso


def registrar_pago(sb, rem, monto, forma, banco, fecha, usuario):
    if _num(monto) <= 0:
        raise ValueError("El monto debe ser mayor que 0.")
    if not forma:
        raise ValueError("Elige la forma de pago.")
    sb.table("remision_pagos").insert({
        "remision_id": rem["id"], "monto": round(_num(monto), 2), "forma_pago": forma, "banco_caja": banco,
        "fecha": (fecha or _hoy()).isoformat(), "usuario": usuario}).execute()
    codigos.registrar(sb, usuario, "Registró pago en remisión", "remision", f"R-{rem['id']}",
                      f"${_num(monto):,.2f} {forma}")


def pasar_a_pedido(sb, rem, items, pagos, vendedores, categoria, usuario):
    """Arma un pedido con las piezas «Se lo llevó · pagado» que aún no tienen pedido, pagado con los pagos de la
    remisión (los más viejos primero). Lo que sobra queda como saldo a favor. Devuelve (numero, avisos)."""
    from ventas import calcular, guardar_pedido, catalogo
    sel = [i for i in items if i.get("estado") == LLEVO_PAG and not i.get("numero_pedido")]
    if not sel:
        raise ValueError("No hay piezas «Se lo llevó · pagado» pendientes de pasar a pedido.")
    if any(_num(i.get("precio_unitario")) <= 0 for i in sel):
        raise ValueError("Hay piezas pagadas sin precio: ponles precio (con autorización) antes de pasarlas a pedido.")
    if not vendedores:
        raise ValueError("Elige al menos un vendedor.")
    if not categoria:
        raise ValueError("Elige la categoría de venta.")
    its = [{"es_sn": False, "modelo": i["modelo"], "descripcion": i.get("nombre") or "",
            "cantidad": int(_num(i.get("cantidad"), 1)), "precio_unitario": _num(i.get("precio_unitario")),
            "costo_unitario": _num(i.get("costo_unitario")), "utilidad_bruta_inv": _num(i.get("utilidad_bruta_inv")),
            "descuento_pct": _num(i.get("descuento_pct"))} for i in sel]
    calc = calcular(its, 0.0, False, 0.0, "Pagado")
    total = calc["total"]
    disponible = saldo_favor(pagos)
    if total > disponible + 0.01:
        raise ValueError(f"Las piezas pagadas suman ${total:,.2f} y los pagos registrados disponibles son "
                         f"${disponible:,.2f}. Registra el pago primero.")
    # Repartir el total entre los pagos (los más viejos primero)
    falta, usos, pagos_ped = round(total, 2), [], []
    for p in sorted(pagos, key=lambda x: x["id"]):
        resto = round(_num(p.get("monto")) - _num(p.get("aplicado")), 2)
        if resto <= 0 or falta <= 0:
            continue
        toma = round(min(resto, falta), 2)
        usos.append((p, toma))
        pagos_ped.append({"forma_pago": p.get("forma_pago"), "banco_caja": p.get("banco_caja"), "monto": toma,
                          "fecha_pago": p.get("fecha")})
        falta = round(falta - toma, 2)
    estados = catalogo(sb, "estado_pedido")
    estado = next((e for e in estados if str(e).strip().lower() == "pagado y entregado"), "Pagado y entregado")
    hoy = _hoy().isoformat()
    datos = {"fecha": hoy, "cliente": rem.get("cliente"), "categoria_venta": categoria, "estado_pedido": estado,
             "status": "Pagado", "observaciones": None, "requiere_entrega": False, "requiere_facturacion": False,
             "con_iva": False, "descuento_pct": 0.0, "costo_total": calc["costo"], "subtotal": calc["subtotal"],
             "iva": calc["iva"], "total": total, "anticipo": total,
             "fecha_pago": min([p["fecha_pago"] for p in pagos_ped if p.get("fecha_pago")] or [hoy]),
             "created_by": usuario, "origen": "app"}
    numero = guardar_pedido(sb, datos, its, calc["lineas"], vendedores, pagos_ped)
    avisos = []
    for p, toma in usos:
        sb.table("remision_pagos").update({"aplicado": round(_num(p.get("aplicado")) + toma, 2)}).eq("id", p["id"]).execute()
    for i in sel:
        sb.table("remision_items").update({"numero_pedido": numero, "reservado": 0}).eq("id", i["id"]).execute()
        a = reservas.consumir(sb, i["modelo"])            # reservado → vendido
        if a:
            avisos.append(a)
    codigos.registrar(sb, usuario, "Pedido creado (de remisión)", "pedido", numero,
                      f"{rem.get('cliente')} · R-{rem['id']} · total ${total:,.2f}")
    return numero, avisos


def borrar(sb, rem, items, pagos, usuario):
    """Borra la remisión. En una abierta, las piezas que seguían apartadas o por pagar vuelven a DISPONIBLE;
    lo que ya pasó a pedido se queda vendido (el pedido no se toca). Devuelve avisos."""
    avisos = []
    if rem.get("tipo") == "abierta":
        for it in items:
            if it.get("modelo") and not it.get("numero_pedido") and it.get("estado") != DEVUELTO:
                a = reservas.liberar(sb, it["modelo"], int(_num(it.get("reservado"))))
                if a:
                    avisos.append(a)
    sb.table("remision_items").delete().eq("remision_id", rem["id"]).execute()
    sb.table("remision_pagos").delete().eq("remision_id", rem["id"]).execute()
    sb.table("remisiones").delete().eq("id", rem["id"]).execute()
    codigos.registrar(sb, usuario, "Borró remisión", "remision", folio(rem),
                      f"{rem.get('cliente') or ''} · {len(items)} renglón(es)"
                      + (f" · pagos ${sum(_num(p.get('monto')) for p in pagos):,.2f}" if pagos else ""))
    return avisos


def cerrar(sb, rem, items, usuario):
    pend = [i for i in items if i.get("estado") in (APARTADO, LLEVO_PEND)
            or (i.get("estado") == LLEVO_PAG and not i.get("numero_pedido"))]
    if pend:
        raise ValueError("Todavía hay piezas apartadas, por pagar o pagadas sin pedido: no se puede cerrar.")
    sb.table("remisiones").update({"estado": "Cerrada"}).eq("id", rem["id"]).execute()
    codigos.registrar(sb, usuario, "Cerró remisión", "remision", f"R-{rem['id']}", rem.get("cliente") or "")


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------
def generar_pdf(rem, items, pagos=()):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from pedido_pdf import ASSETS, DIRECCION, WEB, CORREO, WHATSAPP, INSTAGRAM, FACEBOOK, _imagen
    from cotizacion_pdf import _cargar_foto

    abierta = rem.get("tipo") == "abierta"

    def est(n, **kw):
        base = dict(fontName="Times-Roman", fontSize=10, leading=12)
        base.update(kw)
        return ParagraphStyle(n, **base)

    ST_LBL = est("r_lbl", fontName="Times-Bold", fontSize=10.5)
    ST_VAL = est("r_val", fontSize=10.5)
    ST_TIT = est("r_tit", fontName="Times-Bold", fontSize=18, leading=22, alignment=TA_RIGHT)
    ST_TH = est("r_th", fontName="Times-Bold", fontSize=9.5, textColor=colors.white, alignment=TA_CENTER)
    ST_NUM = est("r_num", fontName="Times-Bold", fontSize=11, alignment=TA_CENTER)
    ST_NOM = est("r_nom", fontName="Times-Bold", fontSize=11, leading=13)
    ST_MOD = est("r_mod", fontSize=9, leading=11, textColor=colors.HexColor("#555555"))
    ST_DESC = est("r_desc", fontSize=9, leading=11)
    ST_NOF = est("r_nof", fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor("#888888"))
    ST_OBS = est("r_obs", fontSize=9.5, leading=12)
    ST_EST = est("r_est", fontName="Times-Bold", fontSize=9, leading=11, alignment=TA_CENTER)
    ST_PRE = est("r_pre", fontSize=10, alignment=TA_RIGHT)
    ST_TOT = est("r_tot", fontSize=10.5, alignment=TA_RIGHT)
    ST_TOTB = est("r_totb", fontName="Times-Bold", fontSize=12, alignment=TA_RIGHT)
    ST_FIR = est("r_fir", fontName="Times-Bold", fontSize=11, alignment=TA_CENTER)
    ST_FIR2 = est("r_fir2", fontSize=10, alignment=TA_CENTER)
    MARGEN = 36
    ANCHO = letter[0] - 2 * MARGEN
    FOTO_W, FOTO_H = 90, 104
    NEGRO, BEIGE = colors.black, colors.HexColor("#C4BD97")

    def dinero(x):
        return f"${_num(x):,.2f}"

    def campo(etiqueta, valor, w_et, w_val):
        t = Table([[Paragraph(escape(etiqueta), ST_LBL), Paragraph(escape(str(valor or "")), ST_VAL)]],
                  colWidths=[w_et, w_val])
        t.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return t

    with ThreadPoolExecutor(max_workers=3) as ex:          # 3 a la vez: menos memoria de pico
        fotos = list(ex.map(_cargar_foto, items))

    story = []
    logo = _imagen("logo_viriathus.png", ancho=300)
    if logo is not None:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 10)]
    f = _fecha(rem.get("fecha"))
    izq = Table([[campo("Cliente:", rem.get("cliente"), 52, 270)],
                 [campo("Fecha:", f.strftime("%d/%m/%Y") if f else "", 52, 120)]], colWidths=[322])
    der = Table([[Paragraph("REMISIÓN", ST_TIT)], [Paragraph(escape(folio(rem)), ST_TIT)]], colWidths=[ANCHO - 340])
    for t in (izq, der):
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    cab = Table([[izq, "", der]], colWidths=[322, 18, ANCHO - 340])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [cab, Spacer(1, 14)]

    w_est = 92
    w_pre = 74 if abierta else 0
    w_prod = ANCHO - 26 - (FOTO_W + 10) - w_est - w_pre
    cab_t = [Paragraph("#", ST_TH), Paragraph("FOTO", ST_TH), Paragraph("PRODUCTO", ST_TH), Paragraph("ESTADO", ST_TH)]
    if abierta:
        cab_t.append(Paragraph("PRECIO", ST_TH))
    filas = [cab_t]
    for i, it in enumerate(items):
        if fotos[i]:
            w, h = ImageReader(io.BytesIO(fotos[i])).getSize()
            e = min(FOTO_W / w, FOTO_H / h)
            celda_foto = Image(io.BytesIO(fotos[i]), width=w * e, height=h * e)
        else:
            celda_foto = Paragraph("Sin foto", ST_NOF)
        desc = it.get("descripcion") or ""
        if len(desc) > MAX_DESC:
            desc = desc[:MAX_DESC].rsplit(" ", 1)[0] + "…"
        texto = [Paragraph(escape(it.get("nombre") or ""), ST_NOM),
                 Paragraph(escape(f"Modelo {it['modelo']}" if it.get("modelo") else "S/N"), ST_MOD), Spacer(1, 4)]
        if desc:
            texto.append(Paragraph(escape(desc).replace("\n", "<br/>"), ST_DESC))
        if (it.get("observaciones") or "").strip():
            texto += [Spacer(1, 4), Paragraph("<b>Observaciones:</b> " +
                                              escape(it["observaciones"].strip()).replace("\n", "<br/>"), ST_OBS)]
        estado = escape(it.get("estado") or "")
        fe = _fecha(it.get("fecha_entrega"))
        if fe and not abierta:
            estado += f"<br/><font size='8'>{fe.strftime('%d/%m/%Y')}</font>"
        if abierta and it.get("numero_pedido"):
            estado += f"<br/><font size='8'>Pedido {it['numero_pedido']}</font>"
        fila = [Paragraph(str(i + 1), ST_NUM), celda_foto, texto, Paragraph(estado, ST_EST)]
        if abierta:
            pre = dinero(_linea(it))
            if _num(it.get("descuento_pct")) > 0:
                pre += f"<br/><font size='8'>desc. {_num(it.get('descuento_pct')):g}%</font>"
            fila.append(Paragraph(pre, ST_PRE))
        filas.append(fila)
    anchos = [26, FOTO_W + 10, w_prod, w_est] + ([w_pre] if abierta else [])
    tabla = Table(filas, colWidths=anchos, repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NEGRO), ("GRID", (0, 0), (-1, -1), 0.5, NEGRO),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("VALIGN", (2, 1), (2, -1), "TOP"),
        ("ALIGN", (1, 1), (1, -1), "CENTER"),
        ("TOPPADDING", (0, 1), (-1, -1), 6), ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("LEFTPADDING", (2, 1), (2, -1), 8),
    ]))
    story.append(tabla)

    final = []
    if abierta:
        r = resumen_abierta(items, pagos)
        pagado_total = round(sum(_num(p.get("monto")) for p in pagos), 2)
        lineas = [(f"Se llevó · pagado ({r['n_pag']})", dinero(r["llevo_pag"]), False),
                  (f"Se llevó · pendiente de pago ({r['n_pend']})", dinero(r["llevo_pend"]), False),
                  (f"Apartado ({r['n_apartado']})", dinero(r["apartado"]), False),
                  ("Pagos recibidos", dinero(pagado_total), False)]
        if r["debe"] > 0.005:
            lineas.append(("TOTAL POR PAGAR", dinero(r["debe"]), True))
        elif r["debe"] < -0.005:
            lineas.append(("SALDO A FAVOR DEL CLIENTE", dinero(-r["debe"]), True))
        else:
            lineas.append(("TOTAL POR PAGAR", dinero(0), True))
        tt = Table([[Paragraph(escape(a), ST_TOTB if b else ST_TOT), Paragraph(v, ST_TOTB if b else ST_TOT)]
                    for a, v, b in lineas], colWidths=[220, 100])
        tt.setStyle(TableStyle([("LINEABOVE", (0, len(lineas) - 1), (-1, len(lineas) - 1), 0.8, NEGRO),
                                ("BACKGROUND", (0, len(lineas) - 1), (-1, len(lineas) - 1), BEIGE),
                                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        tt.hAlign = "RIGHT"
        final += [Spacer(1, 10), tt]
    if final:
        story.append(KeepTogether(final))

    # Firmas al pie de CADA hoja (en lugar del membrete con dirección y redes)
    ABAJO = 120                                  # alto reservado para las firmas

    def pie(c, doc):
        c.saveState()
        if doc.page > 1:
            c.setFont("Times-Roman", 8)
            c.drawRightString(letter[0] - MARGEN, letter[1] - 24,
                              f"Remisión {folio(rem)} — {rem.get('cliente') or ''} — hoja {doc.page}")
        W = 220
        izq_x, der_x = MARGEN, letter[0] - MARGEN - W
        y_tit, y_linea, y_pie = 92, 50, 36
        for x, titulo, debajo in ((izq_x, "Atentamente", "VIRIATHUS"), (der_x, "Recibió", "Nombre y firma")):
            c.setFont("Times-Bold", 11)
            c.drawCentredString(x + W / 2, y_tit, titulo)
            c.setLineWidth(0.6)
            c.line(x, y_linea, x + W, y_linea)
            c.setFont("Times-Roman", 10)
            c.drawCentredString(x + W / 2, y_pie, debajo)
        c.restoreState()

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=letter, leftMargin=MARGEN, rightMargin=MARGEN, topMargin=30,
                          bottomMargin=ABAJO, title=f"Remisión {folio(rem)}", author="Viriathus")
    marco = Frame(MARGEN, ABAJO, ANCHO, letter[1] - 30 - ABAJO, leftPadding=0, rightPadding=0, topPadding=0,
                  bottomPadding=0, id="f")
    doc.addPageTemplates([PageTemplate(id="p", frames=[marco], onPage=pie)])
    doc.build(story)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Pantallas
# ----------------------------------------------------------------------
def boton_remision_pedido(sb, ped, user):
    """Botón en el detalle del pedido: «Generar remisión» / «Ver remisión»."""
    if user["role"] not in ROLES_REMISIONES:
        return
    try:
        rid = remision_de_pedido(sb, ped["numero_pedido"])
    except Exception:
        st.caption("Remisiones no disponibles: falta correr en Supabase el SQL de remisiones.")
        return
    if st.button("📋 Ver remisión" if rid else "📋 Generar remisión", key=f"rm_ped_{ped['numero_pedido']}"):
        try:
            rid = rid or crear_de_pedido(sb, ped["numero_pedido"], user.get("nombre") or user.get("role"))
        except Exception as ex:
            st.error(f"No se pudo generar la remisión: {ex}")
            return
        st.session_state["rm_sel"] = rid
        st.session_state["nav_ventas_ir"] = "Remisiones"
        st.rerun()


def _items_de_varias(sb, ids):
    out = {}
    for i in range(0, len(ids), 200):
        for f in (sb.table("remision_items").select("remision_id,estado,numero_pedido,precio_unitario,cantidad,descuento_pct")
                  .in_("remision_id", ids[i:i + 200]).execute().data or []):
            out.setdefault(f["remision_id"], []).append(f)
    return out


def _lista(sb, user):
    ss = st.session_state
    f1, f2, f3 = st.columns([2, 2, 3])
    tipo = f1.selectbox("Tipo", ["Todas", "De pedido", "Abiertas"], key="rm_f_tipo")
    ver = f2.selectbox("Mostrar", ["Con pendientes", "Todas"], key="rm_f_ver")
    busq = f3.text_input("Buscar cliente o N° de pedido", key="rm_f_busq").strip()
    q = sb.table("remisiones").select("*").order("id", desc=True).limit(300)
    if tipo == "De pedido":
        q = q.eq("tipo", "pedido")
    elif tipo == "Abiertas":
        q = q.eq("tipo", "abierta")
    if busq:
        q = q.eq("numero_pedido", int(busq)) if busq.isdigit() else q.ilike("cliente", f"%{busq.replace('%', '')}%")
    try:
        rems = q.execute().data or []
    except Exception as ex:
        st.error(f"No se pudieron leer las remisiones. ¿Ya corriste en Supabase el SQL de remisiones? ({ex})")
        return
    its = _items_de_varias(sb, [r["id"] for r in rems])
    filas = []
    for r in rems:
        it = its.get(r["id"], [])
        if r["tipo"] == "pedido":
            ent = sum(1 for i in it if i.get("estado") in (ENTREGADO, DEVUELTO))
            pendiente = ent < len(it) or not it
            avance = f"{ent} de {len(it)} entregados"
        else:
            res = resumen_abierta(it, [])
            pendiente = r.get("estado") == "Abierta"
            avance = (f"{res['n_apartado']} apartadas · {res['n_pend']} por pagar · {res['n_pag']} pagadas"
                      + ("" if pendiente else " · CERRADA"))
        if ver == "Con pendientes" and not pendiente:
            continue
        filas.append({"id": r["id"], "FOLIO": folio(r), "TIPO": "De pedido" if r["tipo"] == "pedido" else "Abierta",
                      "CLIENTE": r.get("cliente") or "", "FECHA": r.get("fecha") or "", "AVANCE": avance})
    if not filas:
        st.info("No hay remisiones para mostrar." + (" Las de pedido se generan desde el detalle del pedido."
                                                     if not rems else ""))
        return
    import pandas as pd
    st.dataframe(pd.DataFrame(filas).drop(columns=["id"]), use_container_width=True, hide_index=True)
    etiquetas = {f["id"]: f"{f['FOLIO']} — {f['CLIENTE']}" for f in filas}
    sel = st.selectbox("Abrir remisión", list(etiquetas), format_func=etiquetas.get, index=None,
                       placeholder="Elegir…", key="rm_f_sel")
    if sel:
        ss["rm_sel"] = sel
        ss.pop("rm_f_sel", None)
        st.rerun()


def _nueva_abierta(sb, user):
    from ventas import catalogo
    st.caption("Para un cliente que se lleva, aparta, paga o devuelve piezas de a poco. Las piezas quedan RESERVADAS "
               "hasta que las cambies tú; lo pagado se pasa a pedido.")
    c1, c2, c3 = st.columns([3, 2, 1])
    cliente = c1.text_input("CLIENTE *", key="rm_n_cliente")
    vendedor = c2.selectbox("VENDEDOR *", catalogo(sb, "vendedor"), index=None, key="rm_n_vend", placeholder="Elegir…")
    fecha = c3.date_input("FECHA", value=_hoy(), key="rm_n_fecha", format="DD/MM/YYYY")
    q_v, q_c = codigos.pedir_identidad(sb, "rm_n_id")
    if st.button("Crear remisión abierta", type="primary", key="rm_n_crear"):
        if not cliente.strip() or not vendedor:
            st.error("Falta el cliente o el vendedor.")
            return
        quien = codigos.confirmar(sb, q_v, q_c)
        if not quien:
            return
        try:
            rid = crear_abierta(sb, cliente, vendedor, fecha, quien)
        except Exception as ex:
            st.error(f"No se pudo crear: {ex}. ¿Ya corriste en Supabase el SQL de remisiones?")
            return
        for k in ("rm_n_cliente", "rm_n_vend"):
            st.session_state.pop(k, None)
        st.session_state["rm_sel"] = rid
        st.rerun()


def _editor_items(sb, rem, items, k):
    """Dibuja los renglones. Devuelve (cambios, fotos, quitar_id)."""
    abierta = rem.get("tipo") == "abierta"
    precio_hab = precios.habilitado(k("precio")) if abierta else False
    vf = st.session_state.get(k("fver"), 0)          # sube al guardar: vacía los campos de foto
    cambios, fotos, quitar = {}, {}, None
    for n, it in enumerate(items, 1):
        iid = it["id"]
        bloqueado = abierta and bool(it.get("numero_pedido"))
        with st.container(border=True):
            t1, t2 = st.columns([6, 1])
            titulo = f"**#{n} · {'Modelo ' + str(it['modelo']) if it.get('modelo') else 'S/N'}**"
            if bloqueado:
                titulo += f" · ✅ pasó al pedido {it['numero_pedido']}"
            t1.markdown(titulo)
            if abierta and not bloqueado and t2.button("Quitar", key=k(f"q_{iid}")):
                quitar = iid
            a, b, c = st.columns([1, 4, 2])
            sub = b.file_uploader("Foto" + ("" if it.get("foto_url") else " — ⚠️ no tiene foto, sube una"),
                                  type=["jpg", "jpeg", "png", "webp"], key=k(f"f_{iid}_{vf}"),
                                  help="Solo para la remisión; no cambia la foto del inventario.")
            if sub is not None:
                fotos[iid] = sub.getvalue()
                a.image(fotos[iid], width=90)
            elif it.get("foto_url"):
                a.image(it["foto_url"], width=90)
            else:
                a.warning("Sin foto")
            nombre = b.text_input("Nombre", value=it.get("nombre") or "", key=k(f"n_{iid}"))
            descripcion = b.text_area("Descripción", value=it.get("descripcion") or "", key=k(f"d_{iid}"), height=80)
            obs = b.text_input("Observaciones", value=it.get("observaciones") or "", key=k(f"o_{iid}"),
                               placeholder="Opcional")
            opciones = ESTADOS_ABIERTA if abierta else ESTADOS_PEDIDO
            actual = it.get("estado") if it.get("estado") in opciones else opciones[0]
            estado = c.selectbox("Estado", opciones, index=opciones.index(actual), key=k(f"e_{iid}"),
                                 disabled=bloqueado)
            f = {"nombre": nombre, "descripcion": descripcion, "observaciones": obs, "estado": estado}
            if not abierta:
                fe = c.date_input("Fecha de entrega", value=_fecha(it.get("fecha_entrega")), key=k(f"fe_{iid}"),
                                  format="DD/MM/YYYY")
                f["fecha_entrega"] = fe
            else:
                if bloqueado or not precio_hab:
                    pu = _num(it.get("precio_unitario"))
                    c.number_input("Precio ($) 🔒", value=pu, format="%.2f", disabled=True, key=k(f"pb_{iid}"))
                    tope = max(precios.DESC_LIBRE, _num(it.get("descuento_pct")))
                    dsc = c.number_input(f"Desc. % (hasta {tope:g})", min_value=0.0, max_value=float(tope),
                                         value=float(_num(it.get("descuento_pct"))), step=5.0, format="%.1f",
                                         key=k(f"ds_{iid}"), disabled=bloqueado)
                else:
                    pu = c.number_input("Precio ($)", min_value=0.0, value=_num(it.get("precio_unitario")), step=50.0,
                                        format="%.2f", key=k(f"p_{iid}"))
                    dsc = c.number_input("Desc. %", min_value=0.0, max_value=100.0,
                                         value=float(_num(it.get("descuento_pct"))), step=5.0, format="%.1f",
                                         key=k(f"dh_{iid}"))
                f["precio_unitario"], f["descuento_pct"] = pu, dsc
                c.markdown(f"**Total: ${_linea({**it, 'precio_unitario': pu, 'descuento_pct': dsc}):,.2f}**")
            # ¿cambió algo?
            viejo = {"nombre": it.get("nombre") or "", "descripcion": it.get("descripcion") or "",
                     "observaciones": it.get("observaciones") or "", "estado": actual}
            if not abierta:
                viejo["fecha_entrega"] = _fecha(it.get("fecha_entrega"))
            else:
                viejo["precio_unitario"] = _num(it.get("precio_unitario"))
                viejo["descuento_pct"] = _num(it.get("descuento_pct"))
            def norm(v):
                if isinstance(v, str):
                    return v.replace("\r\n", "\n").strip() or None
                return v or None
            if any(norm(f.get(x)) != norm(viejo.get(x)) for x in viejo) or iid in fotos:
                cambios[iid] = f
    return cambios, fotos, quitar


def _detalle(sb, user, rid):
    ss = st.session_state
    if st.button("← Volver a la lista", key="rm_volver"):
        ss.pop("rm_sel", None)
        st.rerun()
    for m in ss.pop("rm_msgs", []):
        (st.success if m[0] == "ok" else st.warning)(m[1])
    try:
        rem, items, pagos = leer(sb, rid)
    except Exception as ex:
        st.error(f"No se pudo leer la remisión: {ex}")
        return
    if rem is None:
        st.warning("Esa remisión ya no existe.")
        ss.pop("rm_sel", None)
        return
    abierta = rem.get("tipo") == "abierta"
    k = lambda n: f"rm{rid}_{n}"

    if not abierta:
        try:
            agregados, quitados = sincronizar_con_pedido(sb, rem, items)
        except Exception as ex:
            agregados, quitados = [], []
            st.caption(f"No se pudo revisar el pedido: {ex}")
        if agregados or quitados:
            if agregados:
                st.info("Se agregaron a la remisión productos nuevos del pedido: " + ", ".join(map(str, agregados)))
            if quitados:
                st.warning("Se quitaron de la remisión productos que ya no están en el pedido: " + ", ".join(quitados))
            rem, items, pagos = leer(sb, rid)

    st.markdown(f"### Remisión {folio(rem)} — {rem.get('cliente') or ''}")
    st.caption(f"{'Remisión abierta' if abierta else 'Remisión del pedido'} · Fecha: {rem.get('fecha') or '—'} · "
               f"Vendedor: {rem.get('vendedor') or '—'}" + (" · CERRADA" if rem.get("estado") == "Cerrada" else ""))
    if abierta:
        r = resumen_abierta(items, pagos)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Se llevó", f"{r['llevo']} pieza(s)", f"{r['n_pag']} pagadas · {r['n_pend']} por pagar",
                  delta_color="off")
        m2.metric("Por pagar", f"${max(r['debe'], 0):,.2f}")
        m3.metric("Saldo a favor", f"${max(-r['debe'], 0):,.2f}")
        m4.metric("Apartado", f"${r['apartado']:,.2f}", f"{r['n_apartado']} pieza(s)", delta_color="off")

    st.markdown("**Para guardar cambios, identifícate:**")
    q_v, q_c = codigos.pedir_identidad(sb, k("id"))

    if abierta and rem.get("estado") == "Abierta":
        with st.form(k("form_mod"), clear_on_submit=True):
            m1, m2 = st.columns([4, 1])
            modelo_txt = m1.text_input("Agregar pieza (MODELO)")
            m2.write("")
            m2.write("")
            agregar = m2.form_submit_button("Agregar y apartar", use_container_width=True)
        if agregar and modelo_txt.strip():
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    av = agregar_pieza(sb, rem, items, modelo_txt, quien)
                    ss["rm_msgs"] = [("ok", f"Modelo {modelo_txt.strip()} agregado y apartado.")] + [("w", a) for a in av]
                    st.rerun()
                except ValueError as ex:
                    st.error(str(ex))
                except Exception as ex:
                    st.error(f"No se pudo agregar: {ex}")

    if not items:
        st.info("Esta remisión todavía no tiene piezas." if abierta else "El pedido no tiene productos.")
    cambios, fotos, quitar = _editor_items(sb, rem, items, k)
    if quitar:
        quien = codigos.confirmar(sb, q_v, q_c)
        if quien:
            it = next(i for i in items if i["id"] == quitar)
            try:
                a = quitar_pieza(sb, rem, it, quien)
                ss["rm_msgs"] = [("ok", f"Pieza {it.get('modelo')} quitada" + (" (vuelve a disponible)."
                                  if it.get("estado") != DEVUELTO else "."))] + ([("w", a)] if a else [])
                st.rerun()
            except Exception as ex:
                st.error(str(ex))

    aut = None
    hab = False
    if abierta and items:
        hab, aut = precios.control(k("precio"))
    if cambios:
        st.info(f"Hay cambios sin guardar en {len(cambios)} renglón(es).")
    if st.button("Guardar cambios", type="primary", key=k("guardar"), disabled=not cambios):
        quien = codigos.confirmar(sb, q_v, q_c)
        if quien:
            try:
                avisos, cambios_p = guardar_items(sb, rem, items, cambios, fotos, quien, hab, aut)
            except ValueError as ex:
                st.error(str(ex))
            except Exception as ex:
                st.error(f"No se pudo guardar: {ex}")
            else:
                msgs = [("ok", "Remisión guardada.")] + [("w", a) for a in avisos]
                if cambios_p:
                    a = precios.registrar(sb, f"Remisión {folio(rem)}", cambios_p, aut, quien)
                    if a:
                        msgs.append(("w", a))
                codigos.registrar(sb, quien, "Editó remisión", "remision", folio(rem), f"{len(cambios)} renglón(es)")
                if not abierta:
                    _, items2, _ = leer(sb, rid)
                    m, av = actualizar_status_pedido(sb, rem, items2, quien)
                    if m:
                        msgs.append(("ok", m))
                    msgs += [("w", a) for a in av]
                ss[k("fver")] = ss.get(k("fver"), 0) + 1    # que las fotos subidas no se vuelvan a subir
                ss["rm_msgs"] = msgs
                st.rerun()

    # ---- PDF ----
    st.divider()
    firma = hash(repr((sorted(rem.items()), [sorted(i.items()) for i in items], [sorted(p.items()) for p in pagos])))
    g = ss.get("rm_pdf")
    if g and (g.get("rid") != rid or g.get("firma") != firma):
        ss.pop("rm_pdf", None)
        g = None
    p1, p2 = st.columns(2)
    if p1.button("📄 Preparar PDF", key=k("pdf"), disabled=bool(cambios),
                 help="Guarda los cambios antes de preparar el PDF." if cambios else None):
        with st.spinner("Armando el PDF (descarga las fotos)…"):
            try:
                ss["rm_pdf"] = {"rid": rid, "firma": firma, "pdf": generar_pdf(rem, items, pagos)}
                g = ss["rm_pdf"]
            except Exception as ex:
                st.error(f"No se pudo generar el PDF: {ex}")
            finally:
                try:                                   # devolver al sistema la memoria de las fotos
                    from segundo_plano import liberar_memoria
                    liberar_memoria()
                except Exception:
                    pass
    if g:
        descargar(p2, "📥 Descargar PDF", g["pdf"], file_name=f"remision_{folio(rem).replace(' ', '_')}.pdf",
                           mime="application/pdf", type="primary", key=k("dl"))

    if abierta:
        _pagos_y_pedido(sb, rem, items, pagos, q_v, q_c, k)
    _borrar_ui(sb, rem, items, pagos, q_v, q_c, k)


def _borrar_ui(sb, rem, items, pagos, q_v, q_c, k):
    ss = st.session_state
    st.divider()
    with st.expander("🗑 Borrar remisión"):
        abierta = rem.get("tipo") == "abierta"
        if abierta:
            libres = [i for i in items if i.get("modelo") and not i.get("numero_pedido") and i.get("estado") != DEVUELTO]
            pasadas = sorted({i["numero_pedido"] for i in items if i.get("numero_pedido")})
            st.caption(f"Se borra la remisión con sus pagos. {len(libres)} pieza(s) apartada(s) o por pagar vuelven a "
                       "DISPONIBLE." + (f" Los pedidos ya creados ({', '.join(map(str, pasadas))}) no se tocan."
                                        if pasadas else ""))
            sf = saldo_favor(pagos)
            if sf > 0.005:
                st.warning(f"Tiene **${sf:,.2f} de saldo a favor** del cliente: al borrar se pierde ese registro.")
        else:
            st.caption("Se borra la remisión (fotos, estados y observaciones). El pedido no cambia; puedes volver a "
                       "generarla desde el pedido.")
        seguro = st.checkbox("Sí, quiero borrar esta remisión", key=k("borrar_ok"))
        if st.button("Borrar remisión", key=k("borrar"), disabled=not seguro):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    av = borrar(sb, rem, items, pagos, quien)
                except Exception as ex:
                    st.error(f"No se pudo borrar: {ex}")
                    return
                ss.pop("rm_sel", None)
                ss.pop("rm_pdf", None)
                ss["rm_lista_msgs"] = [("ok", f"Remisión {folio(rem)} borrada.")] + [("w", a) for a in av]
                st.rerun()


def _pagos_y_pedido(sb, rem, items, pagos, q_v, q_c, k):
    from ventas import catalogo
    ss = st.session_state
    r = resumen_abierta(items, pagos)
    st.divider()
    st.subheader("Pagos")
    if pagos:
        import pandas as pd
        st.dataframe(pd.DataFrame([{"FECHA": p.get("fecha"), "MONTO": _num(p.get("monto")),
                                    "FORMA DE PAGO": p.get("forma_pago"), "BANCO / CAJA": p.get("banco_caja"),
                                    "USADO EN PEDIDOS": _num(p.get("aplicado"))} for p in pagos]),
                     use_container_width=True, hide_index=True,
                     column_config={"MONTO": st.column_config.NumberColumn(format="$%.2f"),
                                    "USADO EN PEDIDOS": st.column_config.NumberColumn(format="$%.2f")})
    st.caption(f"Saldo a favor disponible: **${r['saldo_favor']:,.2f}**")
    if rem.get("estado") == "Abierta":
        with st.expander("Registrar un pago"):
            c1, c2, c3, c4 = st.columns([2, 2, 3, 2])
            monto = c1.number_input("MONTO ($)", min_value=0.0, step=500.0, format="%.2f", key=k("pg_monto"))
            forma = c2.selectbox("FORMA DE PAGO", catalogo(sb, "forma_pago"), index=None, key=k("pg_forma"),
                                 placeholder="Elegir…")
            banco = c3.selectbox("BANCO / CAJA", catalogo(sb, "banco_caja"), index=None, key=k("pg_banco"),
                                 placeholder="Elegir…")
            fecha = c4.date_input("FECHA", value=_hoy(), key=k("pg_fecha"), format="DD/MM/YYYY")
            if st.button("Registrar pago", key=k("pg_ok")):
                quien = codigos.confirmar(sb, q_v, q_c)
                if quien:
                    try:
                        registrar_pago(sb, rem, monto, forma, banco, fecha, quien)
                        for x in ("pg_monto", "pg_forma", "pg_banco"):
                            ss.pop(k(x), None)
                        ss["rm_msgs"] = [("ok", f"Pago de ${monto:,.2f} registrado.")]
                        st.rerun()
                    except Exception as ex:
                        st.error(str(ex))

    if r["n_pag_sin_pedido"]:
        st.subheader("Pasar a pedido lo pagado")
        st.caption(f"{r['n_pag_sin_pedido']} pieza(s) «Se lo llevó · pagado» por ${r['pagado_sin_pedido']:,.2f}. "
                   f"Pagos disponibles: ${r['saldo_favor']:,.2f}.")
        c1, c2 = st.columns(2)
        vends = catalogo(sb, "vendedor")
        defecto = [v for v in [str(rem.get("vendedor") or "").strip()] if v in vends]
        vendedores = c1.multiselect("VENDEDOR/ES *", vends, default=defecto, key=k("pp_vend"))
        categoria = c2.selectbox("CATEGORÍA DE VENTA *", catalogo(sb, "categoria_venta"), index=None,
                                 key=k("pp_cat"), placeholder="Elegir…")
        if st.button("Pasar a pedido", type="primary", key=k("pp_ok")):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    numero, av = pasar_a_pedido(sb, rem, items, pagos, vendedores, categoria, quien)
                    sobra = round(r["saldo_favor"] - r["pagado_sin_pedido"], 2)
                    ss["rm_msgs"] = [("ok", f"Pedido N° {numero} creado con {r['n_pag_sin_pedido']} pieza(s)."
                                      + (f" Queda saldo a favor de ${sobra:,.2f}." if sobra > 0.005 else ""))] \
                        + [("w", a) for a in av]
                    st.rerun()
                except ValueError as ex:
                    st.error(str(ex))
                except Exception as ex:
                    st.error(f"No se pudo crear el pedido: {ex}")

    if rem.get("estado") == "Abierta":
        st.divider()
        if st.button("Cerrar remisión", key=k("cerrar"),
                     help="Cuando ya no quedan piezas apartadas, por pagar ni pagadas sin pedido."):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    cerrar(sb, rem, items, quien)
                    ss["rm_msgs"] = [("ok", "Remisión cerrada.")] + (
                        [("w", f"Le quedó saldo a favor de ${r['saldo_favor']:,.2f}.")] if r["saldo_favor"] > 0.005 else [])
                    st.rerun()
                except ValueError as ex:
                    st.error(str(ex))


def tab_remisiones(sb, user):
    ss = st.session_state
    if ss.get("rm_sel"):
        _detalle(sb, user, ss["rm_sel"])
        return
    for m in ss.pop("rm_lista_msgs", []):
        (st.success if m[0] == "ok" else st.warning)(m[1])
    vista = st.radio("Ver", ["Lista", "Nueva remisión abierta"], horizontal=True, key="rm_vista",
                     label_visibility="collapsed")
    if vista == "Lista":
        st.caption("Las remisiones de pedido se generan desde el detalle del pedido («📋 Generar remisión»).")
        _lista(sb, user)
    else:
        _nueva_abierta(sb, user)
