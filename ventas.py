"""
Módulo de VENTAS / PEDIDOS para la app de inventario.

Se usa desde app.py con:   render_ventas(supabase, user)

Requiere las tablas creadas con ventas_schema.sql
(catalogos, pedidos, pedido_items, pedido_vendedores, pedido_pagos y las vistas v_comisiones*).
"""
import json
import math
import re
import unicodedata
import uuid
import datetime as dt

import pandas as pd
import streamlit as st

import codigos
import precios
from caches import guardar_en_memoria
from permisos import roles
from navegacion import descargar

IVA_FACTOR = 1.16
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# Quién puede ver costos/utilidad, cancelar pedidos y ver comisiones
# (se definen en permisos.py)
ROLES_COSTOS = roles("ver_costos")
ROLES_CANCELAR = roles("cancelar_pedidos")
ROLES_EDITAR = roles("editar_pedidos")      # quién puede cambiar status / observaciones
ROLES_COMISIONES = roles("comisiones")
ROLES_RESUMEN = roles("resumen_mensual")


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _num(v, default=0.0):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)))
    except ValueError:
        return default


def _parse(raw):
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _nn(texto):
    """Texto vacío -> None (para guardar NULL en la base)."""
    t = (texto or "").strip()
    return t or None


def _fecha_iso(d):
    return d.isoformat() if d else None


def redondear_peso(x):
    """Redondeo a peso entero (0.5 hacia arriba)."""
    return float(math.floor(x + 0.5))


@st.cache_data(ttl=60, show_spinner=False)
def catalogo(_sb, tipo):
    r = (_sb.table("catalogos").select("valor").eq("tipo", tipo)
         .eq("activo", True).order("orden").execute())
    return [x["valor"] for x in r.data]


# ----------------------------------------------------------------------
# Clientes ya registrados (para sugerir y no duplicar)
# ----------------------------------------------------------------------
CLIENTES_IGNORADOS = {"CANCELADO", "SIN NOMBRE"}   # nombres que nunca se sugieren como cliente


def _norm(texto):
    """MAYÚSCULAS, sin acentos y sin espacios repetidos: 'José  García' -> 'JOSE GARCIA'."""
    t = unicodedata.normalize("NFD", str(texto or ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t.upper()).strip()


@st.cache_data(ttl=300, show_spinner=False)
def _clientes_registrados(_sb):
    """Clientes distintos de la tabla pedidos: {nombre normalizado: {nombre, pedidos, ultima}}."""
    clientes, desde = {}, 0
    while True:
        filas = (_sb.table("pedidos").select("cliente,fecha").order("fecha", desc=True)
                 .range(desde, desde + 999).execute().data)
        for r in filas:
            nombre = (r.get("cliente") or "").strip()
            nn = _norm(nombre)
            if not nn or nn in CLIENTES_IGNORADOS:
                continue
            # Las filas vienen de la más reciente a la más antigua: la primera fija nombre y última compra
            g = clientes.setdefault(nn, {"nombre": nombre, "pedidos": 0, "ultima": r.get("fecha")})
            g["pedidos"] += 1
        if len(filas) < 1000:
            break
        desde += 1000
    return clientes


def sugerir_clientes(sb, texto, max_res=6):
    """Clientes registrados que contienen todas las palabras escritas (mín. 3 letras de texto)."""
    n = _norm(texto)
    if len(n) < 3:
        return []
    palabras = n.split(" ")
    hallados = [g for nn, g in _clientes_registrados(sb).items() if all(p in nn for p in palabras)]
    hallados.sort(key=lambda g: (-g["pedidos"], g["nombre"]))
    return hallados[:max_res]


def _usar_cliente(key, nombre):
    st.session_state[key] = nombre


# ----------------------------------------------------------------------
# Cálculos del pedido (función pura, fácil de probar)
# ----------------------------------------------------------------------
CONCEPTOS_FACTURA = ["Accesorios de decoración", "Mobiliario de oficina", "Desglosar productos"]
SALDO_CERO = ("Pagado", "Obsequio", "Cancelado")   # con estos status no hay saldo por cobrar


def es_pagado(status, estado):
    """True si el pedido cuenta como pagado: status 'Pagado' o una observación que empieza con
    'Pagado' (p. ej. 'Pagado y entregado', 'Pagado pendiente de entrega'). Sin importar mayúsculas."""
    st_ = str(status or "").strip().lower()
    es_ = str(estado or "").strip().lower()
    if st_ == "cancelado" or es_ == "cancelado":
        return False
    return st_ == "pagado" or es_.startswith("pagado")


def es_entregado(estado):
    """La comisión solo se da cuando el objeto ya fue entregado: observaciones 'Pagado y entregado'
    o 'Entregado pendiente de pago'. 'Pagado pendiente de entrega', 'Enviado' y 'Pendiente de pago
    y entrega' todavía no cuentan."""
    return "entregado" in str(estado or "").strip().lower()


def anticipo_efectivo(anticipo, pagos, total):
    """Lo ya cobrado del pedido: lo mayor entre el anticipo capturado y la suma de los pagos
    (con un solo pago en 0 se asume el anticipo o, si no hay, el total). Nunca pasa del total."""
    montos = [float(p["monto"]) for p in pagos]
    cobrado = sum(montos)
    if len(montos) == 1 and montos[0] == 0:
        cobrado = anticipo if anticipo > 0 else total
    return round(min(max(anticipo or 0.0, cobrado), total), 2)


def calcular(items, descuento_pct, con_iva, anticipo, status=None, envio=0.0):
    """Cada renglón puede traer su propio "descuento_pct"; `descuento_pct` (general) se mantiene
    solo para pedidos antiguos y se combina con el del renglón.
    `envio` (con IVA incluido, como los precios) se suma al total que paga el cliente, pero el subtotal,
    el IVA, la utilidad y la comisión salen solo de los productos."""
    fg = 1.0 - (descuento_pct or 0.0) / 100.0
    lineas, bruto = [], 0.0
    for it in items:
        b = it["precio_unitario"] * it["cantidad"]
        bruto += b
        di = _num(it.get("descuento_pct"))
        factor = (1.0 - di / 100.0) * fg
        # Con descuento se redondea por renglón (como en el pedido en papel)
        lineas.append(redondear_peso(b * factor) if (di or descuento_pct) else round(b, 2))
    envio = round(max(_num(envio), 0.0), 2)
    total_prod = round(sum(lineas), 2)
    total = round(total_prod + envio, 2)
    costo = round(sum(it["costo_unitario"] * it["cantidad"] for it in items), 2)
    util_inv = round(sum(it.get("utilidad_bruta_inv", 0.0) * it["cantidad"] for it in items), 2)
    if con_iva:
        subtotal = round(total_prod / IVA_FACTOR, 2)
        iva = round(total_prod - subtotal, 2)
    else:
        subtotal, iva = total_prod, 0.0
    return {
        "lineas": lineas, "bruto": round(bruto, 2), "total": total, "subtotal": subtotal,
        "total_productos": total_prod, "envio": envio,
        "iva": iva, "costo": costo, "utilidad": round(subtotal - costo, 2),
        "comision": round(subtotal * 0.02, 2),
        "saldo": 0.0 if status in SALDO_CERO else round(total - (anticipo or 0.0), 2),
        "utilidad_bruta_inv": util_inv,
    }


# ----------------------------------------------------------------------
# Inventario: buscar modelo / marcar vendido / liberar
# ----------------------------------------------------------------------
def buscar_modelo(sb, modelo):
    """Devuelve dict con datos del inventario o None si no existe."""
    r = (sb.table("productos").select("MODELO,datos_json")
         .eq("MODELO", modelo).limit(1).execute())
    if not r.data:
        return None
    d = _parse(r.data[0].get("datos_json"))
    _disp = str(d.get("DISP", d.get("DISPONIBILIDAD", ""))).strip().lower()
    vendido = (_disp == "v")
    return {
        "reservado": (_disp == "r"),
        "modelo": str(r.data[0]["MODELO"]),
        "nombre": str(d.get("NOMBRE", "")).strip(),
        "precio": _num(d.get("REDONDEO_SHOWROOM")),
        "costo": _num(d.get("TOTAL_COSTO", d.get("COSTO_REGISTRADO"))),
        "utilidad_bruta": _num(d.get("UTILIDAD_BRUTA", d.get("UTILIDAD_SHOWROOM"))),
        "vendido": vendido,
        "stock": _num(d.get("CANTIDAD"), 1.0),
    }


def reservado_por(sb, modelo):
    """Nombre del vendedor de la cotización vigente que tiene apartado el modelo ("" si no se encuentra)."""
    try:
        its = (sb.table("cotizacion_items").select("cotizacion_id")
               .eq("modelo", str(modelo)).eq("apartado", True).execute().data or [])
        ids = list({i["cotizacion_id"] for i in its})
        if not ids:
            return ""
        cots = (sb.table("cotizaciones").select("vendedor").in_("id", ids)
                .eq("estado", "Vigente").limit(1).execute().data or [])
        return str(cots[0].get("vendedor") or "").split("/")[0].strip() if cots else ""
    except Exception:
        return ""


def etiqueta_reserva(sb, modelo):
    """«en la remisión abierta de JUAN» / «por una cotización de MARINA» / «por una cotización»."""
    try:
        from remisiones import reservado_en_remision
        cliente = reservado_en_remision(sb, modelo)
    except Exception:
        cliente = ""
    if cliente:
        return f"en la remisión abierta de {cliente}"
    quien = reservado_por(sb, modelo)
    return "por una cotización" + (f" de {quien}" if quien else "")


def texto_reservado(sb, modelo):
    """«El modelo X ya está reservado por una cotización de NOMBRE» (o «en la remisión abierta de CLIENTE»)."""
    return f"El modelo {modelo} ya está reservado {etiqueta_reserva(sb, modelo)}"


def _sync_shopify_disponibilidad(modelo, vendido):
    """Refleja vendido/disponible en Shopify (agotado / 1). Devuelve un aviso solo si falla."""
    try:
        from shopify_borrador import marcar_disponibilidad_shopify
    except Exception:
        return None          # el módulo de Shopify no está disponible: no se sincroniza
    estado, msg = marcar_disponibilidad_shopify(modelo, vendido)
    if estado == "error":
        return (f"Modelo {modelo}: el inventario se actualizó, pero no se pudo reflejar en Shopify ({msg}). "
                "Usa Sincronizar con Shopify en la ficha del producto.")
    return None


def _venta_shopify_de(sb, modelo):
    """Nombre del pedido de Shopify (p. ej. #1001) que marcó este modelo como vendido, o None."""
    try:
        filas = (sb.table("shopify_ordenes").select("nombre,marcados").eq("cancelada", False)
                 .order("creado", desc=True).limit(200).execute().data)
    except Exception:
        return None
    for f in filas:
        if modelo in (f.get("marcados") or []):
            return f.get("nombre")
    return None


def marcar_disponibilidad(sb, modelo, vendido):
    """Marca el modelo como vendido ('v') o disponible (''). Devuelve texto de aviso o None."""
    r = sb.table("productos").select("datos_json").eq("MODELO", modelo).limit(1).execute()
    if not r.data:
        return f"Modelo {modelo}: no se encontró en inventario."
    d = _parse(r.data[0].get("datos_json"))
    if vendido and _num(d.get("CANTIDAD"), 1.0) > 1:
        return f"Modelo {modelo}: tiene cantidad > 1 en inventario, no se marcó como vendido."
    d["DISPONIBILIDAD"] = "v" if vendido else ""
    d["DISP"] = "v" if vendido else ""
    sb.table("productos").update(
        {"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", modelo).execute()
    return _sync_shopify_disponibilidad(modelo, vendido)


def liberar_modelo(sb, m, numero):
    """Devuelve a 'disponible' un modelo que sale del pedido `numero`, salvo que esté también en
    otro pedido que siga activo. Devuelve una lista de avisos."""
    otros = (sb.table("pedido_items").select("numero_pedido")
             .eq("modelo", m).neq("numero_pedido", numero).execute().data)
    nums = [o["numero_pedido"] for o in otros]
    activos = []
    if nums:
        peds = (sb.table("pedidos").select("numero_pedido,estado_pedido,status")
                .in_("numero_pedido", nums).execute().data)
        activos = [p for p in peds if p.get("estado_pedido") != "Cancelado"
                   and p.get("status") != "Cancelado"]
    if activos:
        return [f"Modelo {m}: sigue en el pedido activo {activos[0]['numero_pedido']}, se dejó como vendido."]
    aviso = marcar_disponibilidad(sb, m, False)
    return [aviso] if aviso else []


def liberar_modelos(sb, numero):
    """Al cancelar: devuelve a 'disponible' los modelos del pedido, salvo que estén
    también en otro pedido que siga activo."""
    avisos = []
    items = sb.table("pedido_items").select("modelo").eq("numero_pedido", numero).execute().data
    for it in items:
        m = it.get("modelo")
        if m:
            avisos += liberar_modelo(sb, m, numero)
    return avisos


def reactivar_modelos(sb, numero):
    """Al reactivar un pedido cancelado: vuelve a marcar sus modelos como vendidos."""
    avisos = []
    items = sb.table("pedido_items").select("modelo").eq("numero_pedido", numero).execute().data
    for it in items:
        m = it.get("modelo")
        if not m:
            continue
        info = buscar_modelo(sb, m)
        if info is None:
            avisos.append(f"Modelo {m}: no se encontró en inventario.")
            continue
        if info["vendido"]:
            avisos.append(f"Modelo {m}: ya figuraba como vendido (¿en otro pedido?). Revísalo.")
        aviso = marcar_disponibilidad(sb, m, True)
        if aviso:
            avisos.append(aviso)
    return avisos


def actualizar_pedido(sb, ped, estado, status, notas, pago=None, puede_cancelar=False):
    """Cambia OBSERVACIONES (estado), status y notas de un pedido ya guardado.
    pago = {forma_pago, banco_caja, monto, fecha_pago} (opcional): registra un cobro del saldo.
    Devuelve (mensaje, avisos). Lanza ValueError si el cambio no es válido."""
    numero = ped["numero_pedido"]
    estaba = ped.get("estado_pedido") == "Cancelado" or ped.get("status") == "Cancelado"
    cancela = estado == "Cancelado" or status == "Cancelado"
    if not estado or not status:
        raise ValueError("Elige las observaciones (estado del pedido) y el status.")
    if cancela:
        estado = status = "Cancelado"
    if cancela != estaba and not puede_cancelar:
        raise ValueError("Solo Administrador o Inventario pueden cancelar o reactivar un pedido.")
    if cancela and pago:
        raise ValueError("Un pedido cancelado no puede registrar pagos.")

    total = _num(ped.get("total"))
    anticipo = _num(ped.get("anticipo"))
    cambios = {"estado_pedido": estado, "status": status, "observaciones": _nn(notas)}

    pago_id = None
    if pago and _num(pago.get("monto")) > 0:
        monto = round(float(pago["monto"]), 2)
        if not pago.get("forma_pago"):
            raise ValueError("Elige la forma de pago del cobro.")
        if monto > round(total - anticipo, 2) + 0.01:
            raise ValueError(f"El pago (${monto:,.2f}) es mayor que el saldo (${total - anticipo:,.2f}).")
        res = sb.table("pedido_pagos").insert({
            "numero_pedido": numero, "forma_pago": pago["forma_pago"], "banco_caja": pago.get("banco_caja"),
            "monto": monto, "fecha_pago": pago.get("fecha_pago")}).execute()
        pago_id = (res.data or [{}])[0].get("id")
        cambios["anticipo"] = round(min(total, anticipo + monto), 2)
        if pago.get("fecha_pago") and not ped.get("fecha_pago"):
            cambios["fecha_pago"] = pago["fecha_pago"]

    try:
        sb.table("pedidos").update(cambios).eq("numero_pedido", numero).execute()
    except Exception:
        if pago_id is not None:                      # que no quede un cobro huérfano
            sb.table("pedido_pagos").delete().eq("id", pago_id).execute()
        raise

    avisos = []
    if cancela and not estaba:
        avisos += liberar_modelos(sb, numero)
    elif estaba and not cancela:
        avisos += reactivar_modelos(sb, numero)

    # Comprobación: con status Pagado/Obsequio/Cancelado el saldo debe quedar en 0
    if status in SALDO_CERO:
        rb = sb.table("pedidos").select("saldo").eq("numero_pedido", numero).execute().data
        if rb and _num(rb[0].get("saldo")) > 0.005:
            avisos.append("El saldo no quedó en 0: falta correr en Supabase el ALTER TABLE del saldo "
                          "(está en ventas_schema.sql, sección pedidos).")
    return f"Pedido {numero} actualizado.", avisos


def editar_items_pedido(sb, ped, filas, tomar_inventario=True, aceptar_vendidos=False, usuario="", envio=None,
                        permitir_precio=False, autorizo=None):
    """Reemplaza los renglones de un pedido ya guardado (modelo, descripción, cantidad, precio, costo) y
    recalcula subtotal / IVA / total / costo. Ajusta el inventario: los modelos que salen vuelven a
    disponible y los que entran se marcan vendidos.
    filas = [{"modelo","descripcion","cantidad","precio_unitario","costo_unitario","descuento_pct","orig": renglón anterior o None}]
    Sin `permitir_precio`, el precio y el descuento de los modelos se quedan como estaban (o los del inventario si
    el modelo es nuevo). Con permiso, si cambian hace falta `autorizo`.
    Devuelve (mensaje, avisos, cambios_precio). Lanza ValueError si algo no es válido."""
    numero = ped["numero_pedido"]
    if ped.get("estado_pedido") == "Cancelado" or ped.get("status") == "Cancelado":
        raise ValueError("Un pedido cancelado no se puede editar. Reactívalo primero.")
    if not filas:
        raise ValueError("El pedido debe tener al menos un renglón.")
    viejos = sb.table("pedido_items").select("*").eq("numero_pedido", numero).order("id").execute().data
    modelos_viejos = {str(i["modelo"]) for i in viejos if i.get("modelo")}

    items, vistos, cambios_precio = [], set(), []
    for n, f in enumerate(filas, 1):
        modelo = _nn(f.get("modelo"))
        orig = f.get("orig")
        cantidad = int(_num(f.get("cantidad"), 1))
        precio = round(_num(f.get("precio_unitario")), 2)
        # Si el editor no mostró el costo (rol vendedor), se conserva el que ya tenía el renglón
        costo = round(_num(f["costo_unitario"] if "costo_unitario" in f else (orig or {}).get("costo_unitario")), 2)
        if cantidad < 1:
            raise ValueError(f"Renglón {n}: la cantidad debe ser al menos 1.")
        if precio < 0 or costo < 0:
            raise ValueError(f"Renglón {n}: el precio y el costo no pueden ser negativos.")
        util_inv = _num((orig or {}).get("utilidad_bruta_inv"))
        desc_pct = min(max(_num(f.get("descuento_pct")), 0.0), 100.0)
        if modelo:
            if modelo in vistos:
                raise ValueError(f"El modelo {modelo} está repetido en el pedido.")
            vistos.add(modelo)
            es_nuevo_en_pedido = modelo not in modelos_viejos
            cambio_modelo = orig is None or str(orig.get("modelo") or "") != modelo
            info = buscar_modelo(sb, modelo) if (es_nuevo_en_pedido or cambio_modelo) else None
            if es_nuevo_en_pedido:
                if info is None:
                    raise ValueError(f"Renglón {n}: el modelo {modelo} no existe en el inventario.")
                if info.get("reservado") and not aceptar_vendidos:
                    raise ValueError(f"Renglón {n}: {texto_reservado(sb, modelo)}. "
                                     "Conviértela a pedido desde Cotizaciones, o marca «Aceptar modelos que ya figuran como vendidos».")
                if info["vendido"] and not aceptar_vendidos:
                    raise ValueError(f"Renglón {n}: el modelo {modelo} ya figura como vendido. "
                                     "Marca «Aceptar modelos que ya figuran como vendidos» si es correcto.")
            if info is not None and tomar_inventario and cambio_modelo:
                precio_tocado = precio > 0 if orig is None else round(precio, 2) != round(_num(orig.get("precio_unitario")), 2)
                costo_tocado = costo > 0 if orig is None else round(costo, 2) != round(_num(orig.get("costo_unitario")), 2)
                if not precio_tocado:
                    precio = round(info["precio"], 2)
                if not costo_tocado:
                    costo = round(info["costo"], 2)
                util_inv = info["utilidad_bruta"]
        # Precio de referencia: el que ya tenía el renglón; el del inventario si el modelo es otro; 0 si es un S/N nuevo
        if orig is not None and str(orig.get("modelo") or "") == (modelo or ""):
            ref_p, ref_d = _num(orig.get("precio_unitario")), _num(orig.get("descuento_pct"))
        elif modelo:
            info_ref = info if (modelo and info is not None) else buscar_modelo(sb, modelo)
            ref_p, ref_d = (_num(info_ref["precio"]) if info_ref else 0.0), 0.0
        else:
            ref_p, ref_d = 0.0, 0.0
            if not permitir_precio:
                raise ValueError(f"Renglón {n}: para agregar un S/N hay que ponerle precio, y eso necesita autorización: "
                                 "marca la casilla «Cambiar precio…» y elige quién autorizó.")
        if not permitir_precio:
            precio = round(ref_p, 2)          # sin la casilla el precio no cambia
        c = precios.cambio(modelo, f.get("descripcion"), ref_p, ref_d, precio, desc_pct)
        if c:
            cambios_precio.append(c)
        items.append({"es_sn": not modelo, "modelo": modelo, "descripcion": _nn(f.get("descripcion")),
                      "cantidad": cantidad, "precio_unitario": precio, "costo_unitario": costo,
                      "utilidad_bruta_inv": util_inv, "descuento_pct": desc_pct})

    if cambios_precio and not autorizo:
        raise ValueError("Hay precios o descuentos de más de 10% que necesitan autorización: marca la casilla y elige quién autorizó.")
    envio_ant = round(_num(ped.get("envio")), 2)
    envio = envio_ant if envio is None else round(max(_num(envio), 0.0), 2)
    if envio and "envio" not in ped:
        raise ValueError("Falta la columna «envio» en la tabla pedidos de Supabase. Pide que corran el SQL del envío.")
    calc = calcular(items, _num(ped.get("descuento_pct")), bool(ped.get("con_iva")),
                    _num(ped.get("anticipo")), ped.get("status"), envio)
    nota = (f"Total ${_num(ped.get('total')):,.2f} → ${calc['total']:,.2f}"
            + (f" (envío ${envio_ant:,.2f} → ${envio:,.2f})" if envio != envio_ant else "") + ".")
    # El cambio queda en el registro de cambios; las observaciones son solo las notas del vendedor
    nuevos_datos = {"subtotal": calc["subtotal"], "iva": calc["iva"], "total": calc["total"],
                    "costo_total": calc["costo"]}
    if "envio" in ped:
        nuevos_datos["envio"] = envio
    anteriores = {k: ped.get(k) for k in nuevos_datos}

    sb.table("pedidos").update(nuevos_datos).eq("numero_pedido", numero).execute()
    try:
        sb.table("pedido_items").delete().eq("numero_pedido", numero).execute()
        sb.table("pedido_items").insert([{
            "numero_pedido": numero, "modelo": it["modelo"] or None, "es_sn": it["es_sn"],
            "descripcion": it["descripcion"], "cantidad": it["cantidad"],
            "precio_unitario": it["precio_unitario"], "total_linea": calc["lineas"][i],
            "costo_unitario": it["costo_unitario"], "utilidad_bruta_inv": it["utilidad_bruta_inv"],
            "descuento_pct": it["descuento_pct"],
        } for i, it in enumerate(items)]).execute()
    except Exception:
        # volver a como estaba
        try:
            sb.table("pedido_items").delete().eq("numero_pedido", numero).execute()
            sb.table("pedido_items").insert([{k: v for k, v in i.items() if k != "id"} for i in viejos]).execute()
            sb.table("pedidos").update(anteriores).eq("numero_pedido", numero).execute()
        except Exception:
            pass
        raise

    avisos = []
    nuevos_modelos = {it["modelo"] for it in items if it["modelo"]}
    for m in sorted(modelos_viejos - nuevos_modelos):
        avisos += liberar_modelo(sb, m, numero)
    for m in sorted(nuevos_modelos - modelos_viejos):
        aviso = marcar_disponibilidad(sb, m, True)
        if aviso:
            avisos.append(aviso)
    if ped.get("status") not in SALDO_CERO and calc["total"] < _num(ped.get("anticipo")) - 0.005:
        avisos.append(f"El nuevo total (${calc['total']:,.2f}) es menor que lo ya cobrado "
                      f"(${_num(ped.get('anticipo')):,.2f}): hay un saldo a favor del cliente. Revisa los pagos.")
    if ped.get("status") in SALDO_CERO and ped.get("status") != "Cancelado" and abs(calc["total"] - _num(ped.get("total"))) > 0.005:
        avisos.append("El pedido figura como pagado y su total cambió: revisa que los pagos registrados cuadren con el nuevo total.")
    return f"Pedido {numero}: productos actualizados. {nota}", avisos, cambios_precio


def actualizar_facturacion(sb, numero, efectivo, con_iva, sin_iva, no_factura, calcular_sin_iva=True):
    """Guarda efectivo / facturado con IVA / facturado sin IVA / N° de factura de un pedido."""
    efectivo, con_iva, sin_iva = (round(max(0.0, _num(x)), 2) for x in (efectivo, con_iva, sin_iva))
    if calcular_sin_iva:
        sin_iva = round(con_iva / 1.16, 2)
    sb.table("pedidos").update({
        "efectivo": efectivo, "facturado_con_iva": con_iva, "facturado_sin_iva": sin_iva,
        "no_factura": _nn(no_factura)}).eq("numero_pedido", numero).execute()
    return f"Facturación del pedido {numero} guardada."


# ----------------------------------------------------------------------
# PDF del pedido
# ----------------------------------------------------------------------
def _telefono_whatsapp(tel):
    """Deja el teléfono listo para wa.me (solo dígitos y con lada de país; 10 dígitos → México +52)."""
    d = re.sub(r"\D", "", str(tel or ""))
    if len(d) == 10:
        return "52" + d
    if len(d) == 12 and d.startswith("52"):
        return d
    if len(d) == 13 and d.startswith("521"):
        return "52" + d[3:]
    return d if len(d) >= 11 else ""


def botones_pedido(pdf, pdf_doble, nombre_archivo, telefono, mensaje, clave):
    """Tres botones solo con icono: descargar, imprimir (original + copia cliente) y WhatsApp (enviar).
    Imprimir abre el diálogo de impresión del navegador. Enviar usa «Compartir» del dispositivo (celular, Mac/Windows
    con WhatsApp); si no existe, descarga el PDF y abre WhatsApp con el mensaje listo para que lo adjuntes."""
    import base64
    import json as _json
    import streamlit.components.v1 as components
    b1 = base64.b64encode(pdf).decode()
    b2 = base64.b64encode(pdf_doble).decode() if pdf_doble else ""
    wa = f"https://wa.me/{_telefono_whatsapp(telefono)}?text=" + __import__("urllib.parse").parse.quote(mensaje)
    ico_desc = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v3h16v-3"/></svg>'
    ico_imp = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V3h12v6M6 18H4v-7h16v7h-2M6 14h12v7H6z"/></svg>'
    ico_wa = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-12.4 7.4L3 21l2.1-5.5A8.4 8.4 0 1 1 21 11.5z"/><path d="M9 9.5c.3 2 2.5 4.2 5 5l1.2-1.2-1.700-1-.8.7c-.9-.4-1.700-1.200-2.100-2.100l.7-.8-1-1.700z" fill="currentColor" stroke="none"/></svg>'
    components.html(f"""
<style>
 .fila{{display:flex;gap:10px}}
 .ib{{width:52px;height:44px;display:flex;align-items:center;justify-content:center;border:1px solid #bbb;
      border-radius:.5rem;background:#fff;color:#222;cursor:pointer}}
 .ib:hover{{background:#f3f3f3}} #n{{font:12px sans-serif;color:#666;margin-top:6px}}
</style>
<div class="fila">
 <button class="ib" id="d{clave}" title="Descargar">{ico_desc}</button>
 <button class="ib" id="p{clave}" title="Imprimir (original + copia cliente)">{ico_imp}</button>
 <button class="ib" id="w{clave}" title="Enviar por WhatsApp" style="color:#1a9e4b">{ico_wa}</button>
</div><div id="n"></div>
<script>
const nombre = {_json.dumps(nombre_archivo)}, wa = {_json.dumps(wa)}, texto = {_json.dumps(mensaje)};
function aBlob(b64) {{
  const bin = atob(b64), arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], {{type: "application/pdf"}});
}}
function bajar(blob) {{
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = nombre;
  document.body.appendChild(a); a.click(); a.remove();
}}
const unaCopia = aBlob("{b1}");
document.getElementById("d{clave}").onclick = () => bajar(unaCopia);
document.getElementById("p{clave}").onclick = () => {{
  const url = URL.createObjectURL("{b2}" ? aBlob("{b2}") : unaCopia);
  const f = document.createElement("iframe");
  f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0"; f.src = url;
  f.onload = () => {{ try {{ f.contentWindow.focus(); f.contentWindow.print(); }} catch (e) {{ window.open(url, "_blank"); }} }};
  document.body.appendChild(f);
}};
document.getElementById("w{clave}").onclick = async () => {{
  const file = new File([unaCopia], nombre, {{type: "application/pdf"}});
  try {{
    if (navigator.canShare && navigator.canShare({{files: [file]}})) {{
      await navigator.share({{files: [file], text: texto}}); return;
    }}
  }} catch (e) {{ if (e && e.name === "AbortError") return; }}
  bajar(unaCopia);
  window.open(wa, "_blank");
  document.getElementById("n").textContent = "Se descargó el PDF: adjúntalo en el chat de WhatsApp que se abrió.";
}};
</script>""", height=76)


COPIAS_IMPRESION = ["ORIGINAL", "COPIA CLIENTE"]


def _generar_pdf(ped, items, vendedores, doble=False):
    """Genera el PDF con pedido_pdf.py (requiere: pip install reportlab)."""
    from pedido_pdf import generar_pdf_pedido
    return generar_pdf_pedido(ped, items, vendedores, copias=COPIAS_IMPRESION if doble else None)


def pdf_de_pedido(sb, numero, doble=False):
    """Lee el pedido ya guardado y devuelve los bytes del PDF."""
    ped = sb.table("pedidos").select("*").eq("numero_pedido", numero).execute().data[0]
    items = sb.table("pedido_items").select("*").eq("numero_pedido", numero).order("id").execute().data
    vends = sb.table("pedido_vendedores").select("vendedor").eq("numero_pedido", numero).execute().data
    return _generar_pdf(ped, items, [v["vendedor"] for v in vends], doble)


# ----------------------------------------------------------------------
# Guardado
# ----------------------------------------------------------------------
def guardar_pedido(sb, datos, items, lineas, vendedores, pagos):
    """Inserta pedido + detalle + vendedores + pagos. Si algo falla, borra el pedido."""
    res = sb.table("pedidos").insert(datos).execute()
    numero = res.data[0]["numero_pedido"]
    try:
        sb.table("pedido_items").insert([{
            "numero_pedido": numero,
            "modelo": None if it["es_sn"] else it["modelo"],
            "es_sn": it["es_sn"],
            "descripcion": it["descripcion"],
            "cantidad": int(it["cantidad"]),
            "precio_unitario": it["precio_unitario"],
            "total_linea": lineas[i],
            "costo_unitario": it["costo_unitario"],
            "utilidad_bruta_inv": it.get("utilidad_bruta_inv", 0.0),
            "descuento_pct": _num(it.get("descuento_pct")),
        } for i, it in enumerate(items)]).execute()
        sb.table("pedido_vendedores").insert(
            [{"numero_pedido": numero, "vendedor": v} for v in vendedores]).execute()
        if pagos:
            sb.table("pedido_pagos").insert(
                [{**p, "numero_pedido": numero} for p in pagos]).execute()
    except Exception:
        sb.table("pedidos").delete().eq("numero_pedido", numero).execute()
        raise
    return numero


# ----------------------------------------------------------------------
# Widgets auxiliares
# ----------------------------------------------------------------------
def _select_con_otro(label, opciones, key, index=None):
    sel = st.selectbox(label, list(opciones) + ["Otro…"], index=index, key=key,
                       placeholder="Elegir…")
    if sel == "Otro…":
        return _nn(st.text_input(f"{label} (escribe cuál)", key=f"{key}_otro"))
    return sel


def _estado_a_status(clave_estado, clave_status):
    """Si las observaciones son de un pedido pagado («Pagado y entregado»…), el status pasa solo a «Pagado»."""
    st_ = st.session_state
    if str(st_.get(clave_estado) or "").strip().lower().startswith("pagado"):
        st_[clave_status] = "Pagado"


def _reset_formulario():
    ss = st.session_state
    ss["vt_ver"] = ss.get("vt_ver", 0) + 1
    ss["vt_items"] = []
    ss["vt_npagos"] = 1


# ----------------------------------------------------------------------
# PESTAÑA: NUEVO PEDIDO
# ----------------------------------------------------------------------
def _mostrar_ultimo(prefijo):
    """Mensaje de «pedido guardado» + descarga del PDF. `prefijo` evita llaves repetidas si se muestra en dos pestañas."""
    ss = st.session_state
    ult = ss.get("vt_ultimo")
    if not ult:
        return
    st.success(ult["msg"])
    for aviso in ult["avisos"]:
        st.warning(aviso)
    b1, b2 = st.columns([3, 1])
    if ult.get("pdf"):
        with b1:
            botones_pedido(ult["pdf"], ult.get("pdf2"), f"pedido_{ult['numero']}.pdf", ult.get("telefono"),
                           f"Hola {ult.get('cliente') or ''}, te comparto tu pedido N° {ult['numero']} de Viriathus.",
                           f"u{prefijo}{ult['numero']}")
    if b2.button("Ocultar", key=f"vt_hide_{prefijo}_{ult['numero']}"):
        ss.pop("vt_ultimo", None)
        st.rerun()


def iniciar_conversion(sb, cot, items_cot):
    """Prepara el formulario de «Nuevo pedido» con los datos de una cotización (se usa desde Cotizaciones)."""
    ss = st.session_state
    _reset_formulario()                       # versión nueva = campos limpios
    ver = ss["vt_ver"]
    ss[f"vt{ver}_cliente"] = cot.get("cliente") or ""
    ss[f"vt{ver}_tel"] = cot.get("telefono") or ""
    ss[f"vt{ver}_email"] = cot.get("email") or ""
    try:
        cat = catalogo(sb, "vendedor")
        vs = [v.strip() for v in str(cot.get("vendedor") or "").split("/") if v.strip()]
        ss[f"vt{ver}_vend"] = [v for v in vs if v in cat]
    except Exception:
        pass
    ss["vt_items"] = [{
        "id": uuid.uuid4().hex[:8], "es_sn": bool(i.get("es_sn")), "modelo": i.get("modelo"),
        "descripcion": i.get("descripcion") or "", "cantidad": int(_num(i.get("cantidad"), 1)),
        "precio_unitario": _num(i.get("precio_unitario")), "costo_unitario": _num(i.get("costo_unitario")),
        "utilidad_bruta_inv": _num(i.get("utilidad_bruta_inv")),
        "descuento_pct": _num(i.get("descuento_pct")),
        "precio_lista": _num(i.get("precio_unitario")), "desc_lista": _num(i.get("descuento_pct")),
    } for i in items_cot]
    ss["vt_desde_cot"] = {"id": cot["id"], "codigo": cot.get("codigo"),
                          "apartados": [i["modelo"] for i in items_cot if i.get("apartado") and i.get("modelo")]}


def cancelar_conversion():
    st.session_state.pop("vt_desde_cot", None)
    _reset_formulario()


CAMPOS_ENTREGA = ("requiere_entrega", "domicilio", "atencion", "fecha_entrega", "horario_entrega",
                  "telefono_entrega", "email_entrega", "notas_entrega")
CAMPOS_FACTURA = ("requiere_facturacion", "uso_cfdi", "concepto", "telefono_facturacion", "email_facturacion",
                  "notas_facturacion")


def _editar_entrega_facturacion(sb, ped, sel):
    """Expander del detalle: agregar, quitar o corregir los datos de entrega y de facturación de un pedido guardado."""
    def v(c):
        return ped.get(c) or ""

    def fecha(c):
        try:
            return dt.date.fromisoformat(str(ped.get(c))[:10]) if ped.get(c) else None
        except ValueError:
            return None

    k = lambda n: f"vp_ef_{n}_{sel}"
    with st.expander("Datos de entrega y facturación"):
        st.caption("Agrega, corrige o quita los datos de entrega y de facturación. Al marcar facturación, el pedido "
                   "aparece en «Por facturar».")
        req_ent = st.checkbox("Este pedido lleva DATOS DE ENTREGA", value=bool(ped.get("requiere_entrega")),
                              key=k("req_ent"))
        nuevos = {"requiere_entrega": bool(req_ent)}
        if req_ent:
            with st.container(border=True):
                e1, e2 = st.columns(2)
                nuevos["domicilio"] = e1.text_area("DOMICILIO", value=v("domicilio"), key=k("dom"), height=90)
                nuevos["atencion"] = e2.text_input("ATENCIÓN", value=v("atencion"), key=k("aten"))
                e3, e4 = st.columns(2)
                nuevos["fecha_entrega"] = _fecha_iso(e3.date_input("FECHA DE ENTREGA", value=fecha("fecha_entrega"),
                                                                   key=k("fent"), format="DD/MM/YYYY"))
                nuevos["horario_entrega"] = e4.text_input("HORARIO DE ENTREGA", value=v("horario_entrega"), key=k("hent"))
                e5, e6 = st.columns(2)
                nuevos["telefono_entrega"] = e5.text_input("TELÉFONO (de quien recibe)", value=v("telefono_entrega"),
                                                           key=k("tel_ent"))
                nuevos["email_entrega"] = e6.text_input("E-MAIL (de quien recibe)", value=v("email_entrega"),
                                                        key=k("mail_ent"))
                nuevos["notas_entrega"] = st.text_input("NOTAS DE ENTREGA", value=v("notas_entrega"), key=k("notas_ent"))

        facturado = bool(ped.get("no_factura"))
        req_fac = st.checkbox("Este pedido lleva DATOS DE FACTURACIÓN", value=bool(ped.get("requiere_facturacion")),
                              key=k("req_fac"), disabled=facturado and bool(ped.get("requiere_facturacion")),
                              help="Ya tiene N° de factura: no se puede quitar." if facturado else None)
        nuevos["requiere_facturacion"] = bool(req_fac)
        if req_fac:
            with st.container(border=True):
                f1, f2 = st.columns(2)
                nuevos["uso_cfdi"] = f1.text_input("USO DE CFDI", value=v("uso_cfdi"), key=k("cfdi"))
                conc = v("concepto")
                ops = CONCEPTOS_FACTURA + ([conc] if conc and conc not in CONCEPTOS_FACTURA else [])
                nuevos["concepto"] = f2.selectbox("CONCEPTO *", ops, index=ops.index(conc) if conc in ops else None,
                                                  key=k("concepto"), placeholder="Elegir…") or ""
                f3, f4 = st.columns(2)
                nuevos["telefono_facturacion"] = f3.text_input("TELÉFONO (de quien factura)",
                                                               value=v("telefono_facturacion"), key=k("tel_fac"))
                nuevos["email_facturacion"] = f4.text_input("E-MAIL (de quien factura)",
                                                            value=v("email_facturacion"), key=k("mail_fac"))
                nuevos["notas_facturacion"] = st.text_input("NOTAS DE FACTURACIÓN", value=v("notas_facturacion"),
                                                            key=k("notas_fac"))

        q_v, q_c = codigos.pedir_identidad(sb, k("id"))
        if st.button("Guardar datos de entrega y facturación", type="primary", key=k("guardar")):
            if req_fac and not nuevos.get("concepto"):
                st.error("Elige el concepto de facturación (o desmarca los datos de facturación).")
                return
            quien = codigos.confirmar(sb, q_v, q_c)
            if not quien:
                return
            # Lo que no se muestra (sección desmarcada) se conserva tal cual: si la vuelven a marcar, ahí sigue
            datos = {c: (_nn(x) if isinstance(x, str) else x) for c, x in nuevos.items()}
            cambios = [c for c, x in datos.items() if (ped.get(c) or None) != (x or None)
                       and not (isinstance(x, bool) and bool(ped.get(c)) == x)]
            if not cambios:
                st.info("No hay cambios que guardar.")
                return
            try:
                sb.table("pedidos").update(datos).eq("numero_pedido", sel).execute()
            except Exception as ex:
                st.error(f"No se pudo guardar: {ex}")
                return
            resumen = []
            if "requiere_entrega" in cambios:
                resumen.append("agregó entrega" if req_ent else "quitó entrega")
            if "requiere_facturacion" in cambios:
                resumen.append("agregó facturación" if req_fac else "quitó facturación")
            otros = [c for c in cambios if c not in ("requiere_entrega", "requiere_facturacion")]
            if otros:
                resumen.append("cambió " + ", ".join(otros))
            codigos.registrar(sb, quien, "Editó entrega/facturación", "pedido", sel, "; ".join(resumen))
            st.session_state["vp_msg"] = f"Pedido {sel}: datos de entrega y facturación guardados."
            st.rerun()


def tab_nuevo_pedido(sb, user, desde_cot=False):
    ss = st.session_state
    ss.setdefault("vt_ver", 0)
    ss.setdefault("vt_items", [])
    ss.setdefault("vt_npagos", 1)
    ver = ss["vt_ver"]
    dc = ss.get("vt_desde_cot")
    if dc and not desde_cot:
        st.info(f"Estás pasando la cotización **{dc['codigo']}** a pedido. Termina o cancela la conversión en la pestaña "
                "«Cotizaciones» (el formulario solo puede estar abierto en un lugar).")
        return
    if desde_cot and not dc:
        return

    def k(nombre):
        return f"vt{ver}_{nombre}"

    ve_costos = user["role"] in ROLES_COSTOS
    items = ss["vt_items"]

    if not desde_cot:
        _mostrar_ultimo("np")
    else:
        st.subheader(f"Pasar a pedido la cotización {dc['codigo']}")
        st.caption("Los productos, precios y descuentos vienen de la cotización. Completa lo que falta "
                   "desde el pago en adelante y guarda.")
        if st.button("Cancelar conversión (la cotización sigue vigente)", key="vt_cancel_conv"):
            cancelar_conversion()
            st.rerun()

    st.caption("N° de pedido: se asigna automáticamente al guardar.")

    # ---- Cabecera ----
    c1, c2 = st.columns([3, 1])
    cliente = c1.text_input("CLIENTE O RAZÓN SOCIAL *", key=k("cliente"))
    fecha = c2.date_input("FECHA", value=dt.date.today(), key=k("fecha"), format="DD/MM/YYYY")

    # Sugerencias de clientes ya registrados (para no duplicar). Se actualizan al presionar Enter
    # o al salir del campo.
    try:
        sugeridos = sugerir_clientes(sb, cliente)
    except Exception:
        sugeridos = []
    if sugeridos:
        exacto = next((s for s in sugeridos if _norm(s["nombre"]) == _norm(cliente)), None)
        if exacto:
            st.info(f"Cliente ya registrado: **{exacto['nombre']}** — {exacto['pedidos']} pedido(s), "
                    f"última compra {exacto['ultima']}.")
            if exacto["nombre"] != cliente.strip():
                st.button(f"Usar el nombre tal como está registrado: {exacto['nombre']}",
                          key=k("sug_exacto"), on_click=_usar_cliente, args=(k("cliente"), exacto["nombre"]))
        else:
            st.caption("¿Es alguno de estos clientes ya registrados? Tócalo para usar el mismo nombre:")
            for i, s in enumerate(sugeridos):
                st.button(f"{s['nombre']}  ·  {s['pedidos']} pedido(s)  ·  última: {s['ultima']}",
                          key=k(f"sug_{i}"), on_click=_usar_cliente, args=(k("cliente"), s["nombre"]))

    k1, k2, k3 = st.columns(3)
    telefono = k1.text_input("TELÉFONO", key=k("tel"))
    email = k2.text_input("E-MAIL", key=k("email"))
    celular = k3.text_input("CELULAR (opcional)", key=k("cel"))

    c3, c4 = st.columns(2)
    with c3:
        vendedores = st.multiselect("VENDEDOR/ES *", catalogo(sb, "vendedor"), key=k("vend"),
                                    placeholder="Elegir uno o varios…")
    with c4:
        categoria = _select_con_otro("CATEGORÍA DE VENTA / PROCEDENCIA *",
                                     catalogo(sb, "categoria_venta"), k("categoria"))

    # Entrega y facturación: solo entran al pedido (y al PDF) si se marca el check
    req_entrega = st.checkbox("Este pedido lleva DATOS DE ENTREGA", key=k("req_ent"))
    domicilio = atencion = horario = tel_ent = mail_ent = notas_ent = ""
    fecha_entrega = None
    if req_entrega:
        with st.container(border=True):
            e1, e2 = st.columns(2)
            domicilio = e1.text_area("DOMICILIO", key=k("dom"), height=90)
            atencion = e2.text_input("ATENCIÓN", key=k("atencion"))
            e3, e4 = st.columns(2)
            fecha_entrega = e3.date_input("FECHA DE ENTREGA", value=None, key=k("fent"), format="DD/MM/YYYY")
            horario = e4.text_input("HORARIO DE ENTREGA", key=k("hent"))
            e5, e6 = st.columns(2)
            tel_ent = e5.text_input("TELÉFONO (de quien recibe)", key=k("tel_ent"))
            mail_ent = e6.text_input("E-MAIL (de quien recibe)", key=k("mail_ent"))
            notas_ent = st.text_input("NOTAS DE ENTREGA", key=k("notas_ent"))

    req_fact = st.checkbox("Este pedido lleva DATOS DE FACTURACIÓN", key=k("req_fac"))
    uso_cfdi = concepto = tel_fac = mail_fac = notas_fac = ""
    if req_fact:
        with st.container(border=True):
            f1, f2 = st.columns(2)
            uso_cfdi = f1.text_input("USO DE CFDI", key=k("cfdi"))
            concepto = f2.selectbox("CONCEPTO *", CONCEPTOS_FACTURA, index=None, key=k("concepto"),
                                    placeholder="Elegir…") or ""
            f3, f4 = st.columns(2)
            tel_fac = f3.text_input("TELÉFONO (de quien factura)", key=k("tel_fac"))
            mail_fac = f4.text_input("E-MAIL (de quien factura)", key=k("mail_fac"))
            notas_fac = st.text_input("NOTAS DE FACTURACIÓN", key=k("notas_fac"))

    # ---- Modelos ----
    st.divider()
    st.subheader("Productos")

    with st.form(f"vt_form_modelo_{ver}", clear_on_submit=True):
        m1, m2, m3 = st.columns([3, 1, 1])
        modelo_txt = m1.text_input("MODELO")
        cant_txt = m2.number_input("CANT.", min_value=1, value=1, step=1)
        m3.write("")
        m3.write("")
        agregar = m3.form_submit_button("Agregar", use_container_width=True)
    if agregar:
        m = modelo_txt.strip()
        if m.endswith(".0"):
            m = m[:-2]
        if not m:
            st.warning("Escribe un número de modelo.")
        elif any((not i["es_sn"]) and i["modelo"] == m for i in items):
            st.warning(f"El modelo {m} ya está en el pedido.")
        else:
            try:
                prod = buscar_modelo(sb, m)
            except Exception as ex:
                prod = None
                st.error(f"Error consultando inventario: {ex}")
            if prod is None:
                st.error(f"El modelo {m} no existe en el inventario. Si no tiene número, usa 'Producto sin número (S/N)'.")
            else:
                if prod.get("reservado") and not ss.get("vt_desde_cot"):
                    st.warning(f"{texto_reservado(sb, m)}. Se agregó de todos modos; "
                               "si es la misma venta, mejor usa «Pasar a pedido» desde Cotizaciones.")
                if prod["vendido"]:
                    ped_shop = _venta_shopify_de(sb, m)
                    if ped_shop:
                        st.info(f"El modelo {m} figura como vendido por el pedido {ped_shop} de Shopify. "
                                "Es lo esperado si estás registrando esa venta online.")
                    else:
                        st.warning(f"El modelo {m} figura como VENDIDO en el inventario. Se agregó de todos modos; revísalo.")
                if prod["precio"] <= 0:
                    st.warning(f"El modelo {m} no tiene REDONDEO SHOWROOM. Captura el precio a mano en la lista.")
                items.append({
                    "id": uuid.uuid4().hex[:8], "es_sn": False, "modelo": prod["modelo"],
                    "descripcion": prod["nombre"] or "Sin nombre", "cantidad": int(cant_txt),
                    "precio_unitario": prod["precio"], "costo_unitario": prod["costo"],
                    "utilidad_bruta_inv": prod["utilidad_bruta"], "descuento_pct": 0.0,
                    "precio_lista": prod["precio"], "desc_lista": 0.0,
                })

    with st.expander("Producto sin número de modelo (S/N)"):
        with st.form(f"vt_form_sn_{ver}", clear_on_submit=True):
            s1, s2 = st.columns(2)
            sn_nombre = s1.text_input("NOMBRE *")
            sn_desc = s2.text_input("DESCRIPCIÓN CORTA")
            s3, s4, s5 = st.columns(3)
            sn_precio = s3.number_input("PRECIO FINAL ($) *", min_value=0.0, step=50.0, format="%.2f")
            if ve_costos:
                sn_costo = s4.number_input("COSTO ($) (opcional)", min_value=0.0, step=10.0, format="%.2f")
            else:
                sn_costo = 0.0
            sn_cant = s5.number_input("CANT.", min_value=1, value=1, step=1)
            st.caption("El precio de un S/N necesita autorización de Viriato o Ernesto al guardar el pedido.")
            agregar_sn = st.form_submit_button("Agregar S/N")
        if agregar_sn:
            if not sn_nombre.strip() or sn_precio <= 0:
                st.warning("El S/N necesita nombre y precio final.")
            else:
                desc = sn_nombre.strip() + (f" — {sn_desc.strip()}" if sn_desc.strip() else "")
                items.append({
                    "id": uuid.uuid4().hex[:8], "es_sn": True, "modelo": None,
                    "descripcion": desc, "cantidad": int(sn_cant),
                    "precio_unitario": float(sn_precio), "costo_unitario": float(sn_costo),
                    "utilidad_bruta_inv": 0.0, "descuento_pct": 0.0,
                })

    # Descuento (se necesita antes de mostrar los renglones)
    # El descuento ahora es POR PRODUCTO (columna DESC. %); no hay descuento general.
    descuento = 0.0
    con_iva = st.checkbox("Desglosar IVA (el cliente lo pidió)", key=k("iva"),
                          help="Apagado: subtotal = total. Prendido: subtotal = total / 1.16.")
    anticipo = 0.0      # ya no se captura: lo cobrado sale de los pagos de la sección «Pago»
    envio = float(ss.get(k("envio")) or 0.0)

    # Lista de renglones (precio y cantidad editables)
    if items:
        h1, h2, h3, h6, h4, h5 = st.columns([4, 1, 2, 1, 2, 1])
        h1.caption("MODELO / DESCRIPCIÓN")
        h2.caption("CANT.")
        h3.caption("PRECIO UNIT.")
        h6.caption("DESC. %")
        h4.caption("TOTAL (con desc.)")
        quitar = None
        precio_hab = precios.habilitado(k("precio"))
        for it in items:
            c1, c2, c3, c6, c4, c5 = st.columns([4, 1, 2, 1, 2, 1])
            etiqueta = "S/N" if it["es_sn"] else it["modelo"]
            c1.markdown(f"**{etiqueta}** — {it['descripcion']}")
            it["cantidad"] = c2.number_input("cant", min_value=1, value=int(it["cantidad"]), step=1,
                                             key=k(f"cant_{it['id']}"), label_visibility="collapsed")
            if not precio_hab:
                # Precio bloqueado: el del inventario / cotización. Un S/N conserva el que se capturó (al guardar
                # pide autorización). El descuento es libre hasta 10%.
                if _num(it.get("precio_lista")) > 0:
                    it["precio_unitario"] = float(it["precio_lista"])
                c3.number_input("precio", value=float(it["precio_unitario"]), format="%.2f", disabled=True,
                                key=k(f"precio_b_{it['id']}"), label_visibility="collapsed")
                tope = max(precios.DESC_LIBRE, _num(it.get("desc_lista")))
                it["descuento_pct"] = c6.number_input("desc", min_value=0.0, max_value=float(tope),
                                                      value=float(min(_num(it.get("descuento_pct")), tope)),
                                                      step=5.0, format="%.1f", key=k(f"dsc_{it['id']}"),
                                                      label_visibility="collapsed")
            else:
                it["precio_unitario"] = c3.number_input("precio", min_value=0.0, value=float(it["precio_unitario"]),
                                                        step=50.0, format="%.2f", key=k(f"precio_{it['id']}"),
                                                        label_visibility="collapsed")
                it["descuento_pct"] = c6.number_input("desc", min_value=0.0, max_value=100.0,
                                                      value=float(it.get("descuento_pct") or 0.0), step=5.0,
                                                      format="%.1f", key=k(f"dsc_h_{it['id']}"),
                                                      label_visibility="collapsed")
            c4.markdown(f"**${calcular([it], 0.0, False, 0.0)['total']:,.2f}**")
            if c5.button("Quitar", key=k(f"del_{it['id']}")):
                quitar = it["id"]
        # Envío: un renglón más, abajo de los modelos
        e1, e2, e3, e6, e4, e5 = st.columns([4, 1, 2, 1, 2, 1])
        e1.markdown("**ENVÍO** — Servicio de envío (IVA incluido; déjalo en 0 si no lleva)")
        e2.markdown("1")
        envio = e3.number_input("envío", min_value=0.0, step=100.0, format="%.2f", key=k("envio"),
                                label_visibility="collapsed",
                                help="Se suma al total, pero no cuenta para la utilidad ni la comisión. "
                                     "No lo agregues como producto S/N.")
        e4.markdown(f"**${envio:,.2f}**")
        precio_hab, precio_aut = precios.control(k("precio"))
        if quitar:
            ss["vt_items"] = [i for i in items if i["id"] != quitar]
            st.rerun()
    else:
        precio_hab, precio_aut = False, None

    calc = calcular(items, descuento, con_iva, anticipo, ss.get(k("status")), envio)

    if items:
        st.caption("Totales por renglón: " + " · ".join(
            f"{('S/N' if it['es_sn'] else it['modelo'])}: ${calc['lineas'][i]:,.2f}"
            for i, it in enumerate(items)))

    # ---- Totales (se llenan más abajo, cuando ya se capturaron los pagos y el status) ----
    st.divider()
    caja_totales = st.container()

    # ---- Pagos ----
    st.divider()
    st.subheader("Pago")
    formas = catalogo(sb, "forma_pago")
    bancos = catalogo(sb, "banco_caja")
    pagos = []
    for i in range(ss["vt_npagos"]):
        p1, p2, p3, p4 = st.columns([2, 3, 2, 2])
        forma = p1.selectbox("FORMA DE PAGO" if i == 0 else f"FORMA DE PAGO #{i + 1}", formas,
                             index=None, key=k(f"forma_{i}"), placeholder="Elegir…")
        banco = p2.selectbox("BANCO / CAJA" if i == 0 else f"BANCO / CAJA #{i + 1}", bancos,
                             index=None, key=k(f"banco_{i}"), placeholder="Elegir…")
        monto = p3.number_input("MONTO ($)" if i == 0 else f"MONTO #{i + 1}", min_value=0.0, step=100.0,
                                format="%.2f", key=k(f"monto_{i}"))
        fpago = p4.date_input("FECHA DE PAGO" if i == 0 else f"FECHA PAGO #{i + 1}", value=None,
                              key=k(f"fpago_{i}"), format="DD/MM/YYYY")
        if forma:
            pagos.append({"forma_pago": forma, "banco_caja": banco, "monto": float(monto),
                          "fecha_pago": _fecha_iso(fpago)})
    if st.button("Agregar otro método de pago", key=k("add_pago")):
        ss["vt_npagos"] += 1
        st.rerun()
    st.caption("Lo cobrado es la suma de los pagos: si es menor al total, queda como anticipo y la diferencia como saldo. "
               "Si eliges forma de pago y dejas el monto en 0, se registra el total. Si el pedido está pendiente, "
               "deja la forma de pago vacía.")

    # ---- Estado ----
    st.divider()
    s1, s2 = st.columns(2)
    with s1:
        estado = st.selectbox("OBSERVACIONES (estado del pedido) *", catalogo(sb, "estado_pedido"),
                              index=None, key=k("estado"), placeholder="Elegir…",
                              on_change=_estado_a_status, args=(k("estado"), k("status")))
    with s2:
        status = _select_con_otro("STATUS *", catalogo(sb, "status"), k("status"))
    observaciones = st.text_area("NOTAS ADICIONALES", key=k("obs"), height=70)

    # Totales con lo ya cobrado (pagos capturados) y el status elegido
    anticipo_ef = anticipo_efectivo(anticipo, pagos, calc["total"])
    if es_pagado(status, estado) and not any(p["monto"] > 0 for p in pagos):
        anticipo_ef = calc["total"]          # pagado y sin montos capturados: se toma como pagado el total
    calc = calcular(items, descuento, con_iva, anticipo_ef, status, envio)
    with caja_totales:
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("TOTAL", f"${calc['total']:,.2f}")
        t2.metric("COBRADO", f"${anticipo_ef:,.2f}")
        t3.metric("SALDO", f"${calc['saldo']:,.2f}")
        if con_iva:
            t4.metric("SUBTOTAL / IVA", f"${calc['subtotal']:,.2f} / ${calc['iva']:,.2f}")
        if calc["envio"]:
            st.caption(f"Productos ${calc['total_productos']:,.2f} + envío ${calc['envio']:,.2f} = "
                       f"total ${calc['total']:,.2f}" + (" (subtotal e IVA son solo de los productos)" if con_iva else ""))
        if ve_costos and items:
            u1, u2, u3 = st.columns(3)
            u1.metric("COSTO", f"${calc['costo']:,.2f}")
            u2.metric("UTILIDAD", f"${calc['utilidad']:,.2f}")
            u3.metric("COMISIÓN TOTAL (2%)", f"${calc['comision']:,.2f}")

    # ---- Guardar ----
    st.divider()
    if vendedores:
        _, cod_resp = codigos.pedir_identidad(sb, k("resp"), fijo=vendedores[0])
    else:
        cod_resp = ""
        st.caption("Elige al menos un vendedor: el primero es el responsable y escribe su código para guardar.")
    if st.button("Guardar pedido", type="primary", use_container_width=True, key=k("guardar")):
        errores = []
        if not cliente.strip():
            errores.append("Falta el cliente o razón social.")
        if not vendedores:
            errores.append("Elige al menos un vendedor.")
        if not categoria:
            errores.append("Falta la categoría de venta.")
        if not estado:
            errores.append("Falta elegir las observaciones (estado del pedido).")
        if not status:
            errores.append("Falta el status.")
        if not items:
            errores.append("Agrega al menos un producto.")
        cancelado = (estado == "Cancelado" or status == "Cancelado")
        if items and calc["total"] <= 0 and status not in ("Obsequio", "Cancelado"):
            errores.append("El total es 0. Revisa los precios (o usa status Obsequio).")
        if req_fact and not concepto:
            errores.append("Elige el concepto de facturación (o desmarca los datos de facturación).")
        cambios_precio = precios.cambios_de_items(items)
        if cambios_precio and not precio_aut:
            errores.append("Hay precios o descuentos de más de 10% que necesitan autorización: marca la casilla y elige quién autorizó.")
        if dc and not cancelado:
            # cotización que no apartó (vigencia 0): revisar que sus modelos sigan disponibles
            for it in items:
                if it["es_sn"] or it["modelo"] in dc["apartados"]:
                    continue
                info = buscar_modelo(sb, it["modelo"])
                if info and (info["vendido"] or info.get("reservado")):
                    errores.append(f"El modelo {it['modelo']} ya está VENDIDO." if info["vendido"]
                                   else texto_reservado(sb, it["modelo"]) + ".")

        if len(pagos) == 1 and pagos[0]["monto"] == 0:
            pagos[0]["monto"] = float(calc["total"])
        pagos_validos = [p for p in pagos if p["monto"] > 0]
        if sum(p["monto"] for p in pagos_validos) > calc["total"] + 0.01:
            errores.append("La suma de los pagos es mayor que el total.")

        if errores:
            for e in errores:
                st.error("" + e)
        elif not (responsable := codigos.confirmar(sb, vendedores[0], cod_resp)):
            return
        else:
            fechas = [p["fecha_pago"] for p in pagos_validos if p["fecha_pago"]]
            datos = {
                "fecha": fecha.isoformat(), "cliente": cliente.strip(),
                "categoria_venta": categoria, "estado_pedido": estado, "status": status,
                "observaciones": _nn(observaciones),
                "domicilio": _nn(domicilio), "fecha_entrega": _fecha_iso(fecha_entrega),
                "horario_entrega": _nn(horario), "atencion": _nn(atencion),
                "uso_cfdi": _nn(uso_cfdi), "concepto": _nn(concepto), "email": _nn(email),
                "telefono": _nn(telefono), "celular": _nn(celular),
                "requiere_entrega": bool(req_entrega), "requiere_facturacion": bool(req_fact),
                "telefono_entrega": _nn(tel_ent), "email_entrega": _nn(mail_ent), "notas_entrega": _nn(notas_ent),
                "telefono_facturacion": _nn(tel_fac), "email_facturacion": _nn(mail_fac),
                "notas_facturacion": _nn(notas_fac),
                "con_iva": bool(con_iva), "descuento_pct": float(descuento),
                "costo_total": calc["costo"], "subtotal": calc["subtotal"], "iva": calc["iva"],
                "total": calc["total"], "anticipo": float(anticipo_ef),
                **({"envio": calc["envio"]} if calc["envio"] else {}),
                "fecha_pago": min(fechas) if fechas else None,
                "created_by": responsable, "origen": "app",
            }
            with st.spinner("Guardando pedido..."):
                try:
                    numero = guardar_pedido(sb, datos, items, calc["lineas"], vendedores, pagos_validos)
                except Exception as ex:
                    if "envio" in str(ex):
                        st.error("No se pudo guardar: falta la columna «envio» en la tabla pedidos de Supabase "
                                 "(hay que correr el SQL del envío). Mientras, deja el envío en 0.")
                    else:
                        st.error(f"No se pudo guardar el pedido: {ex}")
                    return
                avisos = []
                apartados = set(dc["apartados"]) if dc else set()
                if not cancelado:
                    for it in items:
                        if it["es_sn"]:
                            continue
                        try:
                            if it["modelo"] in apartados:
                                import reservas
                                a = reservas.consumir(sb, it["modelo"])      # reservado → vendido
                            else:
                                a = marcar_disponibilidad(sb, it["modelo"], True)
                            if a:
                                avisos.append(a)
                        except Exception as ex:
                            avisos.append(f"Modelo {it['modelo']}: no se pudo marcar como vendido ({ex}).")
                elif dc:
                    # pedido cancelado desde el inicio: lo apartado vuelve a estar disponible
                    import reservas
                    for it in items:
                        if it["modelo"] in apartados:
                            a = reservas.liberar(sb, it["modelo"], 0)
                            if a:
                                avisos.append(a)
                if dc:
                    try:
                        sb.table("cotizaciones").update({"estado": "Convertida", "numero_pedido": numero}) \
                            .eq("id", dc["id"]).execute()
                        sb.table("cotizacion_items").update({"apartado": False, "reservado": 0}) \
                            .eq("cotizacion_id", dc["id"]).execute()
                    except Exception as ex:
                        avisos.append(f"El pedido se guardó, pero no se pudo marcar la cotización como convertida ({ex}). "
                                      "Revísala en Cotizaciones.")
            codigos.registrar(sb, responsable, "Pedido creado" + (f" (de {dc['codigo']})" if dc else ""), "pedido", numero,
                              f"{cliente.strip()} · total ${calc['total']:,.2f}")
            a = precios.registrar(sb, f"Pedido {numero}", cambios_precio, precio_aut, responsable)
            if a:
                avisos.append(a)
            _clientes_registrados.clear()   # que el cliente nuevo aparezca ya en las sugerencias
            pdf = pdf2 = None
            try:
                pdf = pdf_de_pedido(sb, numero)
                pdf2 = pdf_de_pedido(sb, numero, doble=True)
            except Exception as ex:
                avisos.append(f"El pedido se guardó, pero no se pudo generar el PDF ({ex}). "
                              "Puedes descargarlo después desde la pestaña Pedidos.")
            ss["vt_ultimo"] = {"numero": numero, "pdf": pdf, "pdf2": pdf2, "cliente": cliente.strip(), "telefono": telefono, "avisos": avisos,
                               "msg": f"Pedido N° {numero} guardado correctamente."}
            if dc:
                ss.pop("vt_desde_cot", None)
            _reset_formulario()
            st.rerun()


# ----------------------------------------------------------------------
# PESTAÑA: PEDIDOS (consulta y cancelación)
# ----------------------------------------------------------------------
@guardar_en_memoria(ttl=30)
def _lista_pedidos(_sb, busq, filtro_status, sin_costo, ve_costos, limite, filtro_vend=(), filtro_forma=(),
                   filtro_banco=()):
    """Lista de pedidos (con memoria de 30 s; se borra sola al guardar cualquier cambio).
    filtro_vend: pedidos donde participa cualquiera de esos vendedores.
    filtro_forma / filtro_banco: pedidos con algún pago de esa forma de pago / en ese banco o caja.
    Cada fila trae «pago»: cómo y a dónde se pagó («Efectivo · CAJA $1,000 + Transferencia · BBVA $500»)."""
    def consulta(numeros=None):
        q = (_sb.table("pedidos")
             .select("numero_pedido,fecha,cliente,total,anticipo,saldo,status,estado_pedido" + (",costo_total" if ve_costos else ""))
             .order("numero_pedido", desc=True).limit(limite))
        if numeros is not None:
            q = q.in_("numero_pedido", numeros)
        if busq:
            if busq.isdigit():
                q = q.eq("numero_pedido", int(busq))
            else:
                q = q.ilike("cliente", f"%{re.sub(r'[%,()]', '', busq)}%")
        if filtro_status:
            q = q.in_("status", list(filtro_status))
        if sin_costo:
            q = q.eq("costo_total", 0).neq("status", "Cancelado")
        return q.execute().data

    # Pedidos posibles según vendedor y pago (si hay esos filtros)
    posibles = None
    if filtro_vend:
        posibles = {r["numero_pedido"] for r in (_sb.table("pedido_vendedores").select("numero_pedido")
                                                 .in_("vendedor", list(filtro_vend)).limit(20000).execute().data or [])}
    if filtro_forma or filtro_banco:
        q = _sb.table("pedido_pagos").select("numero_pedido").limit(20000)
        if filtro_forma:
            q = q.in_("forma_pago", list(filtro_forma))
        if filtro_banco:
            q = q.in_("banco_caja", list(filtro_banco))
        por_pago = {r["numero_pedido"] for r in (q.execute().data or [])}
        posibles = por_pago if posibles is None else (posibles & por_pago)

    if posibles is None:
        filas = consulta()
    else:
        nums, filas = sorted(posibles, reverse=True), []
        for i in range(0, len(nums), 300):          # de a 300 para no armar una consulta enorme
            filas += consulta(nums[i:i + 300])
            if len(filas) >= limite:
                break
        filas.sort(key=lambda r: r["numero_pedido"], reverse=True)
        filas = filas[:limite]

    # Columna «cómo y a dónde se pagó»
    nums = [f["numero_pedido"] for f in filas]
    pagos = {}
    for i in range(0, len(nums), 300):
        for p in (_sb.table("pedido_pagos").select("numero_pedido,forma_pago,banco_caja,monto")
                  .in_("numero_pedido", nums[i:i + 300]).order("id").execute().data or []):
            pagos.setdefault(p["numero_pedido"], []).append(p)
    for f in filas:
        ps = pagos.get(f["numero_pedido"], [])
        partes = []
        for p in ps:
            t = " · ".join(x for x in (p.get("forma_pago"), p.get("banco_caja")) if x)
            if len(ps) > 1:
                t += f" ${_num(p.get('monto')):,.0f}"
            partes.append(t)
        f["pago"] = " + ".join(partes)
    return filas


def tab_pedidos(sb, user):
    ve_costos = user["role"] in ROLES_COSTOS
    puede_cancelar = user["role"] in ROLES_CANCELAR

    msg = st.session_state.pop("vp_msg", None)
    if msg:
        st.success(msg)
    for aviso in st.session_state.pop("vp_avisos", []):
        st.warning(aviso)

    # Captura rápida de costos (solo Administrador)
    try:
        from costos import captura_rapida_costos
    except Exception as ex:
        st.caption(f"Captura rápida de costos no disponible: {ex}")
    else:
        captura_rapida_costos(sb, user)

    busq = st.text_input("Buscar por N° de pedido o por cliente", key="vp_busq").strip()

    f1, f3, f2 = st.columns([3, 3, 2])
    status_opts = catalogo(sb, "status")
    filtro_status = f1.multiselect("Filtrar por status", status_opts, key="vp_f_status",
                                   placeholder="Todos los status")
    filtro_vend = f3.multiselect("Filtrar por vendedor", catalogo(sb, "vendedor"), key="vp_f_vend",
                                 placeholder="Todos los vendedores",
                                 help="Pedidos donde participa ese vendedor (como responsable o compartido).")
    g1, g2, _g3 = st.columns([3, 3, 2])
    filtro_forma = g1.multiselect("Filtrar por forma de pago", catalogo(sb, "forma_pago"), key="vp_f_forma",
                                  placeholder="Todas las formas de pago")
    filtro_banco = g2.multiselect("Filtrar por banco / caja", catalogo(sb, "banco_caja"), key="vp_f_banco",
                                  placeholder="Todos los bancos / cajas",
                                  help="Pedidos con al menos un pago en ese banco o caja.")
    sin_costo = False
    if ve_costos:
        sin_costo = f2.checkbox("Solo pedidos sin costo", key="vp_f_sincosto",
                                help="Pedidos con costo total en $0 (sin costo capturado). No incluye cancelados.")
    hay_filtro = bool(busq or filtro_status or sin_costo or filtro_vend or filtro_forma or filtro_banco)
    limite = 500 if hay_filtro else 50

    rows = _lista_pedidos(sb, busq, tuple(filtro_status), sin_costo, ve_costos, limite, tuple(filtro_vend),
                          tuple(filtro_forma), tuple(filtro_banco))
    if not rows:
        st.info("No hay pedidos para mostrar con ese filtro." if hay_filtro else "No hay pedidos para mostrar.")
        return

    if hay_filtro:
        extra = f" (se muestran los {limite} más recientes; afina el filtro)" if len(rows) >= limite else ""
        st.caption(f"{len(rows)} pedido(s) encontrado(s){extra}.")
    else:
        st.caption("Últimos 50 pedidos. Usa la búsqueda o los filtros para ver otros.")

    df = pd.DataFrame(rows).rename(columns={
        "numero_pedido": "N° PEDIDO", "fecha": "FECHA", "cliente": "CLIENTE", "total": "TOTAL",
        "anticipo": "ANTICIPO", "saldo": "SALDO", "status": "STATUS", "estado_pedido": "OBSERVACIONES",
        "costo_total": "COSTO", "pago": "PAGO (CÓMO Y DÓNDE)"})
    if not ve_costos and "COSTO" in df.columns:
        df = df.drop(columns=["COSTO"])
    st.dataframe(df, use_container_width=True, hide_index=True)

    sel = st.selectbox("Ver detalle del pedido", [r["numero_pedido"] for r in rows],
                       index=None, key="vp_sel", placeholder="Elegir N° de pedido…")
    if sel is None:
        return

    ped = sb.table("pedidos").select("*").eq("numero_pedido", sel).execute().data[0]
    items = sb.table("pedido_items").select("*").eq("numero_pedido", sel).order("id").execute().data
    vends = sb.table("pedido_vendedores").select("vendedor").eq("numero_pedido", sel).execute().data
    pagos = sb.table("pedido_pagos").select("*").eq("numero_pedido", sel).execute().data

    st.markdown(f"### Pedido N° {sel} — {ped['cliente']}")
    st.write(f"{ped['fecha']}  |  {', '.join(v['vendedor'] for v in vends) or '—'}  |  "
             f"{ped.get('categoria_venta') or '—'}")
    st.write(f"**Observaciones:** {ped.get('estado_pedido') or '—'}  |  **Status:** {ped.get('status') or '—'}")
    m1, m2, m3 = st.columns(3)
    m1.metric("TOTAL", f"${_num(ped['total']):,.2f}")
    m2.metric("ANTICIPO", f"${_num(ped['anticipo']):,.2f}")
    m3.metric("SALDO", f"${_num(ped['saldo']):,.2f}")
    if _num(ped.get("envio")):
        st.caption(f"El total incluye envío de ${_num(ped.get('envio')):,.2f} (no cuenta para utilidad ni comisión).")
    if ve_costos:
        n1, n2, n3 = st.columns(3)
        n1.metric("COSTO", f"${_num(ped['costo_total']):,.2f}")
        n2.metric("UTILIDAD", f"${_num(ped['utilidad']):,.2f}")
        n3.metric("COMISIÓN (2%)", f"${_num(ped['comision_total']):,.2f}")
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("EFECTIVO", f"${_num(ped.get('efectivo')):,.2f}")
        g2.metric("FACTURADO CON IVA", f"${_num(ped.get('facturado_con_iva')):,.2f}")
        g3.metric("FACTURADO SIN IVA", f"${_num(ped.get('facturado_sin_iva')):,.2f}")
        g4.metric("N° FACTURA", ped.get("no_factura") or "—")

    # El PDF se arma solo cuando se pide (antes se armaban 2 en cada clic: ~15 MB y tiempo). Se guarda uno a la vez
    # y se descarta si el pedido cambió.
    firma = hash(repr((sorted(ped.items()), [sorted(i.items()) for i in items], [v["vendedor"] for v in vends])))
    guardado = st.session_state.get("vp_pdf")
    if guardado and (guardado.get("num") != sel or guardado.get("firma") != firma):
        st.session_state.pop("vp_pdf", None)
        guardado = None
    if guardado is None:
        if st.button("📄 Preparar PDF (descargar / imprimir / WhatsApp)", key=f"vp_prep_{sel}"):
            try:
                nombres = [v["vendedor"] for v in vends]
                st.session_state["vp_pdf"] = {"num": sel, "firma": firma, "pdf": _generar_pdf(ped, items, nombres),
                                              "pdf2": _generar_pdf(ped, items, nombres, doble=True)}
                st.rerun()
            except Exception as ex:
                st.caption(f"No se pudo generar el PDF: {ex}")
    else:
        botones_pedido(guardado["pdf"], guardado["pdf2"], f"pedido_{sel}.pdf", ped.get("telefono"),
                       f"Hola {ped.get('cliente') or ''}, te comparto tu pedido N° {sel} de Viriathus.", f"p{sel}")
    if ped.get("estado_pedido") != "Cancelado" and ped.get("status") != "Cancelado":
        try:
            from remisiones import boton_remision_pedido
            boton_remision_pedido(sb, ped, user)
        except Exception as ex:
            st.caption(f"Remisiones no disponible: {ex}")

    if items:
        dfi = pd.DataFrame(items)
        dfi["modelo"] = dfi["modelo"].fillna("S/N")
        cols = ["modelo", "descripcion", "cantidad", "precio_unitario"]
        if "descuento_pct" in dfi.columns and (dfi["descuento_pct"].fillna(0) > 0).any():
            cols.append("descuento_pct")
        cols.append("total_linea")
        if ve_costos:
            cols.append("costo_unitario")
        st.dataframe(dfi[cols], use_container_width=True, hide_index=True)
    elif ped.get("modelos_texto"):
        st.caption("Pedido importado del Excel: no tiene precios por pieza, solo el texto original de los modelos.")
        st.text(ped["modelos_texto"])
    if pagos:
        st.dataframe(pd.DataFrame(pagos)[["forma_pago", "banco_caja", "monto", "fecha_pago"]],
                     use_container_width=True, hide_index=True)

    if user["role"] in ROLES_EDITAR:
        ya_cancelado = ped.get("estado_pedido") == "Cancelado" or ped.get("status") == "Cancelado"
        with st.expander("Cambiar status / observaciones"):
            if ya_cancelado and not puede_cancelar:
                st.caption("Solo Administrador o Inventario pueden modificar un pedido cancelado.")
            else:
                def _opciones(tipo, actual):
                    ops = list(catalogo(sb, tipo))
                    if actual and actual not in ops:
                        ops.append(actual)
                    return ops, (ops.index(actual) if actual in ops else None)

                ops_e, idx_e = _opciones("estado_pedido", ped.get("estado_pedido"))
                nuevo_estado = st.selectbox("OBSERVACIONES (estado del pedido)", ops_e, index=idx_e,
                                            key=f"vp_e_estado_{sel}", placeholder="Elegir…",
                                            on_change=_estado_a_status,
                                            args=(f"vp_e_estado_{sel}", f"vp_e_status_{sel}"))
                ops_s, idx_s = _opciones("status", ped.get("status"))
                nuevo_status = _select_con_otro("STATUS", ops_s, f"vp_e_status_{sel}", index=idx_s)
                notas = st.text_area("NOTAS ADICIONALES", value=ped.get("observaciones") or "",
                                     key=f"vp_e_notas_{sel}", height=70)

                saldo_real = round(_num(ped["total"]) - _num(ped["anticipo"]), 2)
                pago = None
                if nuevo_status != "Cancelado" and saldo_real > 0.01:
                    st.caption(f"Saldo por cobrar según los pagos registrados: ${saldo_real:,.2f}")
                    pasa_a_pagado = (nuevo_status == "Pagado")
                    if st.checkbox("Registrar el cobro del saldo ahora", value=pasa_a_pagado,
                                   key=f"vp_e_chk_{sel}_{pasa_a_pagado}"):
                        c1, c2, c3, c4 = st.columns([2, 3, 2, 2])
                        forma = c1.selectbox("FORMA DE PAGO", catalogo(sb, "forma_pago"), index=None,
                                             key=f"vp_e_forma_{sel}", placeholder="Elegir…")
                        banco = c2.selectbox("BANCO / CAJA", catalogo(sb, "banco_caja"), index=None,
                                             key=f"vp_e_banco_{sel}", placeholder="Elegir…")
                        monto = c3.number_input("MONTO ($)", min_value=0.0, value=float(saldo_real), step=100.0,
                                                format="%.2f", key=f"vp_e_monto_{sel}")
                        fpago = c4.date_input("FECHA DE PAGO", value=dt.date.today(),
                                              key=f"vp_e_fecha_{sel}", format="DD/MM/YYYY")
                        pago = {"forma_pago": forma, "banco_caja": banco, "monto": float(monto),
                                "fecha_pago": _fecha_iso(fpago)}
                    elif pasa_a_pagado:
                        st.caption("Sin registrar el cobro: el saldo se pondrá en 0, pero no quedará el pago anotado.")

                q_v, q_c = codigos.pedir_identidad(sb, f"vp_e_{sel}")
                if st.button("Guardar cambios", type="primary", key=f"vp_e_guardar_{sel}") \
                        and (quien := codigos.confirmar(sb, q_v, q_c)):
                    try:
                        mensaje, avisos = actualizar_pedido(sb, ped, nuevo_estado, nuevo_status, notas,
                                                            pago=pago, puede_cancelar=puede_cancelar)
                    except ValueError as ex:
                        st.error(f"{ex}")
                    except Exception as ex:
                        st.error(f"No se pudo guardar: {ex}")
                    else:
                        codigos.registrar(sb, quien, "Cambió observaciones/status/cobro", "pedido", sel,
                                          f"{ped.get('estado_pedido')} → {nuevo_estado}; {ped.get('status')} → {nuevo_status}"
                                          + (f"; cobro ${pago['monto']:,.2f}" if pago else ""))
                        st.session_state["vp_msg"] = mensaje
                        st.session_state["vp_avisos"] = avisos
                        st.rerun()

    if user["role"] in ROLES_EDITAR and ped.get("estado_pedido") != "Cancelado" and ped.get("status") != "Cancelado":
        _editar_entrega_facturacion(sb, ped, sel)

    if puede_cancelar and items and ped.get("estado_pedido") != "Cancelado" and ped.get("status") != "Cancelado":
        with st.expander("Editar productos del pedido" + (" (modelo, precio, costo)" if ve_costos else " (modelo, precio, descuento)")):
            st.markdown("- **Quitar un producto:** marca su casilla **QUITAR** (última columna).\n"
                        "- **Agregar uno:** escríbelo en un renglón vacío de abajo (modelo vacío = S/N).\n"
                        "- **Envío:** ponlo en el campo **ENVÍO**, no como producto S/N.\n"
                        "- Luego tu código y **Guardar productos**. Nada se guarda hasta ese botón.")
            st.caption("Se recalculan subtotal, IVA, total, costo, utilidad y comisión; los modelos que salen vuelven a "
                       "disponible y los que entran quedan vendidos. Los pagos registrados no cambian.")
            vers = st.session_state.get(f"vp_ei_v_{sel}", 0)
            vacio = {"modelo": "", "descripcion": "", "cantidad": 1, "precio_unitario": 0.0, "descuento_pct": 0.0,
                     **({"costo_unitario": 0.0} if ve_costos else {}), "quitar": False}
            base = pd.DataFrame([{"modelo": i.get("modelo") or "", "descripcion": i.get("descripcion") or "",
                                  "cantidad": int(_num(i.get("cantidad"), 1)),
                                  "precio_unitario": _num(i.get("precio_unitario")),
                                  "descuento_pct": _num(i.get("descuento_pct")),
                                  **({"costo_unitario": _num(i.get("costo_unitario"))} if ve_costos else {}),
                                  "quitar": False}
                                 for i in items] + [dict(vacio), dict(vacio)])
            clave_precio = f"vp_ei_p_{sel}_{vers}"
            ei_hab = precios.habilitado(clave_precio)
            editado = st.data_editor(
                base, num_rows="fixed", use_container_width=True, hide_index=True, key=f"vp_ei_{sel}_{vers}",
                disabled=[] if ei_hab else ["precio_unitario"],
                column_config={
                    "modelo": st.column_config.TextColumn("MODELO (vacío = S/N)"),
                    "descripcion": st.column_config.TextColumn("DESCRIPCIÓN"),
                    "cantidad": st.column_config.NumberColumn("CANTIDAD", min_value=1, step=1, default=1),
                    "precio_unitario": st.column_config.NumberColumn("PRECIO UNIT.", min_value=0.0, format="$%.2f", default=0.0),
                    "descuento_pct": st.column_config.NumberColumn("DESC. %", min_value=0.0, max_value=100.0, format="%.1f", default=0.0),
                    **({"costo_unitario": st.column_config.NumberColumn("COSTO UNIT.", min_value=0.0, format="$%.2f", default=0.0)}
                       if ve_costos else {}),
                    "quitar": st.column_config.CheckboxColumn("🗑 QUITAR", default=False,
                                                              help="Marca para quitar este renglón del pedido.")})
            envio_sn = [i for i in items if not i.get("modelo") and "envi" in
                        str(i.get("descripcion") or "").lower().replace("í", "i")]
            if envio_sn and not _num(ped.get("envio")):
                st.info("Este pedido tiene el envío como producto S/N. Para separarlo: marca **QUITAR** en ese "
                        "renglón y pon el monto en **ENVÍO** (abajo).")
            if not ei_hab:
                st.caption("🔒 Precio bloqueado: los modelos toman el precio que ya tenían o el del inventario. Descuento "
                           "libre hasta 10%. Para cambiar un precio, ponerle precio a un S/N o dar más descuento, marca "
                           "la casilla de abajo.")
            ei_hab, ei_aut = precios.control(clave_precio)
            tomar_inv = st.checkbox("Si cambio el modelo y no toco el precio/costo, tomarlos del inventario",
                                    value=True, key=f"vp_ei_inv_{sel}")
            acepta_v = st.checkbox("Aceptar modelos que ya figuran como vendidos", value=False, key=f"vp_ei_vend_{sel}")
            envio_ed = st.number_input("ENVÍO ($, IVA incluido)", min_value=0.0, value=float(_num(ped.get("envio"))),
                                       step=100.0, format="%.2f", key=f"vp_ei_envio_{sel}",
                                       help="Se suma al total; no cuenta para utilidad ni comisión.")
            q_v2, q_c2 = codigos.pedir_identidad(sb, f"vp_ei_{sel}")
            if st.button("Guardar productos", type="primary", key=f"vp_ei_guardar_{sel}") \
                    and (quien2 := codigos.confirmar(sb, q_v2, q_c2)):
                filas = []
                for idx_f, fila in editado.iterrows():
                    d = {k: (None if pd.isna(v) else v) for k, v in fila.to_dict().items()}
                    if d.pop("quitar", False):
                        continue            # marcado para quitar
                    if not d.get("modelo") and not d.get("descripcion") and not _num(d.get("precio_unitario")):
                        continue            # renglón en blanco
                    d["orig"] = items[idx_f] if isinstance(idx_f, int) and idx_f < len(items) else None
                    filas.append(d)
                try:
                    mensaje, avisos, cambios_p = editar_items_pedido(sb, ped, filas, tomar_inv, acepta_v, quien2,
                                                                    envio_ed, ei_hab, ei_aut)
                except ValueError as ex:
                    st.error(f"{ex}")
                except Exception as ex:
                    st.error(f"No se pudo guardar: {ex}")
                else:
                    codigos.registrar(sb, quien2, "Editó productos", "pedido", sel, mensaje)
                    a = precios.registrar(sb, f"Pedido {sel}", cambios_p, ei_aut, quien2)
                    if a:
                        avisos = list(avisos) + [a]
                    st.session_state[f"vp_ei_v_{sel}"] = vers + 1      # el editor vuelve a leer el pedido guardado
                    st.session_state["vp_msg"] = mensaje
                    st.session_state["vp_avisos"] = avisos
                    st.rerun()

    if user["role"] in ROLES_EDITAR:
        with st.expander("Facturación y efectivo"):
            en_efectivo = round(sum(_num(pg.get("monto")) for pg in pagos
                                    if str(pg.get("forma_pago") or "").strip().lower() == "efectivo"), 2)
            st.caption(f"Pagos registrados en efectivo: ${en_efectivo:,.2f}  |  "
                       f"Total del pedido: ${_num(ped['total']):,.2f}"
                       + (f"  |  Subtotal sin IVA: ${_num(ped['subtotal']):,.2f}" if ped.get("con_iva") else ""))
            fc1, fc2, fc3 = st.columns(3)
            v_efec = fc1.number_input("EFECTIVO ($)", min_value=0.0, value=float(_num(ped.get("efectivo"))),
                                      step=100.0, format="%.2f", key=f"vp_f_efec_{sel}")
            v_con = fc2.number_input("FACTURADO CON IVA ($)", min_value=0.0,
                                     value=float(_num(ped.get("facturado_con_iva"))),
                                     step=100.0, format="%.2f", key=f"vp_f_con_{sel}")
            auto_sin = st.checkbox("Calcular 'facturado sin IVA' automáticamente (con IVA ÷ 1.16)",
                                   value=True, key=f"vp_f_auto_{sel}")
            if auto_sin:
                fc3.metric("FACTURADO SIN IVA ($)", f"{round(v_con / 1.16, 2):,.2f}")
                v_sin = 0.0
            else:
                v_sin = fc3.number_input("FACTURADO SIN IVA ($)", min_value=0.0,
                                         value=float(_num(ped.get("facturado_sin_iva"))),
                                         step=100.0, format="%.2f", key=f"vp_f_sin_{sel}")
            v_fact = st.text_input("N° DE FACTURA", value=ped.get("no_factura") or "", key=f"vp_f_nofact_{sel}")
            q_v3, q_c3 = codigos.pedir_identidad(sb, f"vp_f_{sel}")
            if st.button("Guardar facturación", key=f"vp_f_guardar_{sel}") \
                    and (quien3 := codigos.confirmar(sb, q_v3, q_c3)):
                try:
                    mensaje = actualizar_facturacion(sb, sel, v_efec, v_con, v_sin, v_fact, auto_sin)
                except Exception as ex:
                    st.error(f"No se pudo guardar: {ex}")
                else:
                    codigos.registrar(sb, quien3, "Editó facturación/efectivo", "pedido", sel,
                                      f"efectivo {v_efec} · con IVA {v_con} · factura {v_fact}")
                    st.session_state["vp_msg"] = mensaje
                    st.rerun()

    if puede_cancelar and ped.get("estado_pedido") != "Cancelado" and ped.get("status") != "Cancelado":
        with st.expander("Cancelar este pedido"):
            st.warning("Marca el pedido como CANCELADO y devuelve sus modelos a DISPONIBLE en el inventario.")
            ok = st.checkbox("Confirmo que deseo cancelar este pedido", key=f"vp_conf_{sel}")
            q_v4, q_c4 = codigos.pedir_identidad(sb, f"vp_x_{sel}")
            if ok and st.button("Cancelar pedido", type="primary", key=f"vp_cancel_{sel}") \
                    and (quien4 := codigos.confirmar(sb, q_v4, q_c4)):
                try:
                    sb.table("pedidos").update(
                        {"estado_pedido": "Cancelado", "status": "Cancelado"}
                    ).eq("numero_pedido", sel).execute()
                    avisos = liberar_modelos(sb, sel)
                except Exception as ex:
                    st.error(f"Error al cancelar: {ex}")
                    return
                codigos.registrar(sb, quien4, "Pedido cancelado", "pedido", sel)
                st.session_state["vp_msg"] = f"Pedido {sel} cancelado y modelos liberados."
                st.session_state["vp_avisos"] = avisos
                st.rerun()


# ----------------------------------------------------------------------
# PESTAÑA: COMISIONES
# ----------------------------------------------------------------------
def tab_comisiones(sb, user):
    hoy = dt.date.today()
    c1, c2, c3 = st.columns([1, 1, 3])
    anio = c1.selectbox("Año", list(range(hoy.year, hoy.year - 6, -1)), key="vc_anio")
    mes = c2.selectbox("Mes", list(range(1, 13)), index=hoy.month - 1,
                       format_func=lambda m: MESES[m - 1], key="vc_mes")
    # Opciones: vendedores activos del catálogo + los que tuvieron comisiones ese mes (aunque ya no estén activos)
    del_mes = (sb.table("v_comisiones_mensual").select("vendedor").eq("anio", anio).eq("mes", mes)
               .execute().data)
    opciones = sorted(set(catalogo(sb, "vendedor")) | {r["vendedor"] for r in del_mes})
    vend = c3.multiselect("Vendedor/es (vacío = todos)", opciones, key="vc_vend")
    solo_pagados = st.checkbox("Solo pedidos pagados (status Pagado u observación \"Pagado…\")", key="vc_pagados")

    q = sb.table("v_comisiones").select("*").eq("anio", anio).eq("mes", mes).order("fecha")
    if vend:
        q = q.in_("vendedor", vend)
    rows = q.range(0, 4999).execute().data
    if not rows:
        st.info("No hay comisiones para ese mes.")
        return

    df = pd.DataFrame(rows)
    # Solo se paga comisión de pedidos ya ENTREGADOS
    entregado = [es_entregado(e) for e in df["estado_pedido"]]
    sin_entregar = df[[not x for x in entregado]]
    df = df[entregado]
    if solo_pagados:
        df = df[[es_pagado(a, b) for a, b in zip(df["status"], df["estado_pedido"])]]
    if not sin_entregar.empty:
        n_ped = sin_entregar["numero_pedido"].nunique()
        with st.expander(f"{n_ped} pedido(s) de este mes aún sin entregar (no suman comisión todavía)"):
            st.dataframe(sin_entregar[["numero_pedido", "fecha", "cliente", "vendedor", "estado_pedido"]].rename(
                columns={"numero_pedido": "N° PEDIDO", "fecha": "FECHA", "cliente": "CLIENTE",
                         "vendedor": "VENDEDOR", "estado_pedido": "OBSERVACIONES"}),
                use_container_width=True, hide_index=True)
    if df.empty:
        st.info("No hay pedidos entregados para ese filtro: la comisión se da cuando el objeto se entrega.")
        return
    for c in ("subtotal_asignado", "comision"):
        df[c] = pd.to_numeric(df[c])

    resumen = (df.groupby("vendedor")
               .agg(pedidos=("numero_pedido", "count"),
                    ventas_subtotal=("subtotal_asignado", "sum"),
                    comision_a_pagar=("comision", "sum"))
               .reset_index().sort_values("comision_a_pagar", ascending=False))
    st.subheader(f"Comisión a pagar — {MESES[mes - 1]} {anio}")
    st.dataframe(resumen.rename(columns={
        "vendedor": "VENDEDOR", "pedidos": "PEDIDOS", "ventas_subtotal": "VENTAS (SUBTOTAL)",
        "comision_a_pagar": "COMISIÓN A PAGAR"}),
        use_container_width=True, hide_index=True)
    st.metric("TOTAL DE COMISIONES", f"${resumen['comision_a_pagar'].sum():,.2f}")

    with st.expander("Ver detalle por pedido"):
        det = df[["vendedor", "numero_pedido", "fecha", "cliente", "status",
                  "n_vendedores", "subtotal_asignado", "comision"]].rename(columns={
            "vendedor": "VENDEDOR", "numero_pedido": "N° PEDIDO", "fecha": "FECHA", "cliente": "CLIENTE",
            "status": "STATUS", "n_vendedores": "N° VENDEDORES",
            "subtotal_asignado": "SUBTOTAL ASIGNADO", "comision": "COMISIÓN"})
        st.dataframe(det, use_container_width=True, hide_index=True)
        descargar(st, "Descargar detalle (CSV)", det.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"comisiones_{anio}_{mes:02d}.csv", mime="text/csv")


# ----------------------------------------------------------------------
# PESTAÑA: RESUMEN MENSUAL (ventas sin IVA, costos y utilidad por mes)
# ----------------------------------------------------------------------
def resumen_mensual(sb, anio, solo_pagados=False):
    """Resumen por mes (con memoria de 2 min; se borra sola al guardar cualquier cambio)."""
    return _resumen_mensual_mem(sb, anio, solo_pagados)


@guardar_en_memoria(ttl=120)
def _resumen_mensual_mem(_sb, anio, solo_pagados):
    return _resumen_mensual(_sb, anio, solo_pagados)


def _resumen_mensual(sb, anio, solo_pagados=False):
    """Devuelve un DataFrame con una fila por mes (1-12) del año, sin pedidos cancelados."""
    filas, desde = [], 0
    while True:
        r = (sb.table("pedidos")
             .select("numero_pedido,mes,subtotal,costo_total,utilidad,status,estado_pedido")
             .eq("anio", anio).order("numero_pedido").range(desde, desde + 999).execute().data)
        filas += r
        if len(r) < 1000:
            break
        desde += 1000
    df = pd.DataFrame(filas, columns=["numero_pedido", "mes", "subtotal", "costo_total", "utilidad",
                                      "status", "estado_pedido"])
    for c in ("subtotal", "costo_total", "utilidad"):
        df[c] = pd.to_numeric(df[c]).fillna(0.0)
    df = df[(df["status"].fillna("") != "Cancelado") & (df["estado_pedido"].fillna("") != "Cancelado")]
    if solo_pagados:
        df = df[[es_pagado(a, b) for a, b in zip(df["status"], df["estado_pedido"])]]
    df = df.assign(sin_costo=(df["costo_total"] <= 0).astype(int))
    por_mes = df.groupby("mes").agg(pedidos=("numero_pedido", "count"), ventas=("subtotal", "sum"),
                                    costos=("costo_total", "sum"), utilidad=("utilidad", "sum"),
                                    sin_costo=("sin_costo", "sum"))
    por_mes = por_mes.reindex(range(1, 13), fill_value=0)
    por_mes.index.name = "mes"
    por_mes = por_mes.reset_index()
    por_mes["margen"] = [(u / v * 100.0) if v else 0.0 for u, v in zip(por_mes["utilidad"], por_mes["ventas"])]
    return por_mes


def tab_resumen(sb, user):
    hoy = dt.date.today()
    c1, c2 = st.columns([1, 3])
    anio = c1.selectbox("Año", list(range(hoy.year, hoy.year - 8, -1)), key="vr_anio")
    solo_pagados = c2.checkbox("Solo pedidos pagados (status Pagado u observación \"Pagado…\")", key="vr_pagados")
    st.caption("Ventas = subtotal sin IVA de los pedidos del mes (por fecha del pedido). "
               "No incluye pedidos cancelados. Utilidad = ventas − costos.")

    try:
        df = resumen_mensual(sb, anio, solo_pagados)
    except Exception as ex:
        st.error(f"No se pudo calcular el resumen: {ex}")
        return
    if df["pedidos"].sum() == 0:
        st.info("No hay pedidos para ese año.")
        return

    tot_ventas, tot_costos, tot_util = df["ventas"].sum(), df["costos"].sum(), df["utilidad"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(f"VENTAS SIN IVA {anio}", f"${tot_ventas:,.2f}")
    m2.metric(f"COSTOS {anio}", f"${tot_costos:,.2f}")
    m3.metric(f"UTILIDAD {anio}", f"${tot_util:,.2f}")
    m4.metric("MARGEN", f"{(tot_util / tot_ventas * 100.0) if tot_ventas else 0.0:,.1f}%")

    tabla = df.assign(MES=[MESES[m - 1] for m in df["mes"]])[
        ["MES", "pedidos", "ventas", "costos", "utilidad", "margen", "sin_costo"]].rename(columns={
        "pedidos": "PEDIDOS", "ventas": "VENTAS SIN IVA", "costos": "COSTOS", "utilidad": "UTILIDAD",
        "margen": "MARGEN %", "sin_costo": "PEDIDOS SIN COSTO"})
    total = {"MES": "TOTAL " + str(anio), "PEDIDOS": int(df["pedidos"].sum()), "VENTAS SIN IVA": tot_ventas,
             "COSTOS": tot_costos, "UTILIDAD": tot_util,
             "MARGEN %": (tot_util / tot_ventas * 100.0) if tot_ventas else 0.0,
             "PEDIDOS SIN COSTO": int(df["sin_costo"].sum())}
    tabla = pd.concat([tabla, pd.DataFrame([total])], ignore_index=True)
    st.dataframe(tabla, use_container_width=True, hide_index=True, column_config={
        "VENTAS SIN IVA": st.column_config.NumberColumn(format="$%.2f"),
        "COSTOS": st.column_config.NumberColumn(format="$%.2f"),
        "UTILIDAD": st.column_config.NumberColumn(format="$%.2f"),
        "MARGEN %": st.column_config.NumberColumn(format="%.1f%%")})
    if int(df["sin_costo"].sum()):
        st.caption("\"Pedidos sin costo\" son pedidos con costo $0: su utilidad sale igual a la venta. "
                   "Puedes encontrarlos en Pedidos con el filtro \"Solo pedidos sin costo\".")

    grafica = df.assign(MES=[MESES[m - 1][:3] for m in df["mes"]]).set_index("MES")[["ventas", "costos", "utilidad"]]
    st.bar_chart(grafica.rename(columns={"ventas": "Ventas sin IVA", "costos": "Costos", "utilidad": "Utilidad"}))
    descargar(st, "Descargar resumen (CSV)", tabla.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"resumen_mensual_{anio}.csv", mime="text/csv")


# ----------------------------------------------------------------------
# PUNTO DE ENTRADA
# ----------------------------------------------------------------------
def render_ventas(sb, user):
    st.header("Ventas y Pedidos")
    try:
        catalogo(sb, "vendedor")
    except Exception as ex:
        st.error(f"No se pudieron cargar los catálogos de ventas. ¿Ya corriste ventas_schema.sql en Supabase? Detalle: {ex}")
        return

    secciones = [("Nuevo pedido", tab_nuevo_pedido), ("Pedidos", tab_pedidos)]
    try:
        from cotizaciones import ROLES_COTIZAR, tab_cotizaciones
        if user["role"] in ROLES_COTIZAR:
            secciones.insert(1, ("Cotizaciones", tab_cotizaciones))
    except Exception as ex:
        st.caption(f"Cotizaciones no disponible: {ex}")
    try:
        from remisiones import ROLES_REMISIONES, tab_remisiones
        if user["role"] in ROLES_REMISIONES:
            pos = [n for n, _ in secciones].index("Pedidos") + 1
            secciones.insert(pos, ("Remisiones", tab_remisiones))
    except Exception as ex:
        st.caption(f"Remisiones no disponible: {ex}")
    try:
        from facturar import ROLES_FACTURAR, tab_por_facturar
        if user["role"] in ROLES_FACTURAR:
            secciones.insert(secciones.index(("Pedidos", tab_pedidos)) + 1, ("Por facturar", tab_por_facturar))
    except Exception as ex:
        st.caption(f"Por facturar no disponible: {ex}")
    if user["role"] in ROLES_RESUMEN:
        secciones.append(("Resumen mensual", tab_resumen))
    if user["role"] in ROLES_COMISIONES:
        secciones.append(("Comisiones", tab_comisiones))
    if user["role"] in roles("codigos"):
        secciones.append(("Códigos", codigos.tab_codigos))
    # Solo se carga la sección elegida (con pestañas se cargaban todas en cada clic)
    from navegacion import selector
    ir = st.session_state.pop("nav_ventas_ir", None)        # p. ej. «Ver remisión» desde un pedido
    if ir in [n for n, _ in secciones]:
        st.session_state["nav_ventas"] = st.session_state["nav_ventas__ultimo"] = ir
    elegida = selector([n for n, _ in secciones], "nav_ventas", "Sección de ventas")
    dict(secciones)[elegida](sb, user)