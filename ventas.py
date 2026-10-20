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

IVA_FACTOR = 1.16
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# Quién puede ver costos/utilidad, cancelar pedidos y ver comisiones
ROLES_COSTOS = ("Administrador", "Inventario")
ROLES_CANCELAR = ("Administrador", "Inventario")
ROLES_EDITAR = ("Administrador", "Inventario", "Vendedor")   # quién puede cambiar status / observaciones
ROLES_COMISIONES = ("Administrador",)


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
SALDO_CERO = ("Pagado", "Obsequio", "Cancelado")   # con estos status no hay saldo por cobrar


def anticipo_efectivo(anticipo, pagos, total):
    """Lo ya cobrado del pedido: lo mayor entre el anticipo capturado y la suma de los pagos
    (con un solo pago en 0 se asume el anticipo o, si no hay, el total). Nunca pasa del total."""
    montos = [float(p["monto"]) for p in pagos]
    cobrado = sum(montos)
    if len(montos) == 1 and montos[0] == 0:
        cobrado = anticipo if anticipo > 0 else total
    return round(min(max(anticipo or 0.0, cobrado), total), 2)


def calcular(items, descuento_pct, con_iva, anticipo, status=None):
    factor = 1.0 - (descuento_pct or 0.0) / 100.0
    lineas, bruto = [], 0.0
    for it in items:
        b = it["precio_unitario"] * it["cantidad"]
        bruto += b
        # Con descuento se redondea por renglón (como en el pedido en papel)
        lineas.append(redondear_peso(b * factor) if descuento_pct else round(b, 2))
    total = round(sum(lineas), 2)
    costo = round(sum(it["costo_unitario"] * it["cantidad"] for it in items), 2)
    util_inv = round(sum(it.get("utilidad_bruta_inv", 0.0) * it["cantidad"] for it in items), 2)
    if con_iva:
        subtotal = round(total / IVA_FACTOR, 2)
        iva = round(total - subtotal, 2)
    else:
        subtotal, iva = total, 0.0
    return {
        "lineas": lineas, "bruto": round(bruto, 2), "total": total, "subtotal": subtotal,
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
    vendido = (str(d.get("DISP", d.get("DISPONIBILIDAD", ""))).strip().lower() == "v")
    return {
        "modelo": str(r.data[0]["MODELO"]),
        "nombre": str(d.get("NOMBRE", "")).strip(),
        "precio": _num(d.get("REDONDEO_SHOWROOM")),
        "costo": _num(d.get("TOTAL_COSTO", d.get("COSTO_REGISTRADO"))),
        "utilidad_bruta": _num(d.get("UTILIDAD_BRUTA", d.get("UTILIDAD_SHOWROOM"))),
        "vendido": vendido,
        "stock": _num(d.get("CANTIDAD"), 1.0),
    }


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
    return None


def liberar_modelos(sb, numero):
    """Al cancelar: devuelve a 'disponible' los modelos del pedido, salvo que estén
    también en otro pedido que siga activo."""
    avisos = []
    items = sb.table("pedido_items").select("modelo").eq("numero_pedido", numero).execute().data
    for it in items:
        m = it.get("modelo")
        if not m:
            continue
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
            avisos.append(f"Modelo {m}: sigue en el pedido activo {activos[0]['numero_pedido']}, se dejó como vendido.")
            continue
        aviso = marcar_disponibilidad(sb, m, False)
        if aviso:
            avisos.append(aviso)
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
def _generar_pdf(ped, items, vendedores):
    """Genera el PDF con pedido_pdf.py (requiere: pip install reportlab)."""
    from pedido_pdf import generar_pdf_pedido
    return generar_pdf_pedido(ped, items, vendedores)


def pdf_de_pedido(sb, numero):
    """Lee el pedido ya guardado y devuelve los bytes del PDF."""
    ped = sb.table("pedidos").select("*").eq("numero_pedido", numero).execute().data[0]
    items = sb.table("pedido_items").select("*").eq("numero_pedido", numero).order("id").execute().data
    vends = sb.table("pedido_vendedores").select("vendedor").eq("numero_pedido", numero).execute().data
    return _generar_pdf(ped, items, [v["vendedor"] for v in vends])


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


def _reset_formulario():
    ss = st.session_state
    ss["vt_ver"] = ss.get("vt_ver", 0) + 1
    ss["vt_items"] = []
    ss["vt_npagos"] = 1


# ----------------------------------------------------------------------
# PESTAÑA: NUEVO PEDIDO
# ----------------------------------------------------------------------
def tab_nuevo_pedido(sb, user):
    ss = st.session_state
    ss.setdefault("vt_ver", 0)
    ss.setdefault("vt_items", [])
    ss.setdefault("vt_npagos", 1)
    ver = ss["vt_ver"]

    def k(nombre):
        return f"vt{ver}_{nombre}"

    ve_costos = user["role"] in ROLES_COSTOS
    items = ss["vt_items"]

    ult = ss.get("vt_ultimo")
    if ult:
        st.success(ult["msg"])
        for aviso in ult["avisos"]:
            st.warning(aviso)
        b1, b2 = st.columns([2, 1])
        if ult.get("pdf"):
            b1.download_button(f"🖨️ Descargar pedido N° {ult['numero']} (PDF)", ult["pdf"],
                               file_name=f"pedido_{ult['numero']}.pdf", mime="application/pdf",
                               type="primary", key=f"vt_dl_{ult['numero']}")
        if b2.button("✖ Ocultar", key=f"vt_hide_{ult['numero']}"):
            ss.pop("vt_ultimo", None)
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
            st.info(f"✅ Cliente ya registrado: **{exacto['nombre']}** — {exacto['pedidos']} pedido(s), "
                    f"última compra {exacto['ultima']}.")
            if exacto["nombre"] != cliente.strip():
                st.button(f"Usar el nombre tal como está registrado: {exacto['nombre']}",
                          key=k("sug_exacto"), on_click=_usar_cliente, args=(k("cliente"), exacto["nombre"]))
        else:
            st.caption("¿Es alguno de estos clientes ya registrados? Tócalo para usar el mismo nombre:")
            for i, s in enumerate(sugeridos):
                st.button(f"👤 {s['nombre']}  ·  {s['pedidos']} pedido(s)  ·  última: {s['ultima']}",
                          key=k(f"sug_{i}"), on_click=_usar_cliente, args=(k("cliente"), s["nombre"]))

    c3, c4 = st.columns(2)
    with c3:
        vendedores = st.multiselect("VENDEDOR/ES *", catalogo(sb, "vendedor"), key=k("vend"),
                                    placeholder="Elegir uno o varios…")
    with c4:
        categoria = _select_con_otro("CATEGORÍA DE VENTA / PROCEDENCIA *",
                                     catalogo(sb, "categoria_venta"), k("categoria"))

    with st.expander("📍 Datos de entrega"):
        e1, e2 = st.columns(2)
        domicilio = e1.text_area("DOMICILIO", key=k("dom"), height=90)
        atencion = e2.text_input("ATENCIÓN", key=k("atencion"))
        e3, e4 = st.columns(2)
        fecha_entrega = e3.date_input("FECHA DE ENTREGA", value=None, key=k("fent"), format="DD/MM/YYYY")
        horario = e4.text_input("HORARIO DE ENTREGA", key=k("hent"))

    with st.expander("🧾 Datos de facturación y contacto"):
        f1, f2 = st.columns(2)
        uso_cfdi = f1.text_input("USO DE CFDI", key=k("cfdi"))
        concepto = f2.text_input("CONCEPTO", key=k("concepto"))
        f3, f4, f5 = st.columns(3)
        email = f3.text_input("E-MAIL DEL CLIENTE", key=k("email"))
        telefono = f4.text_input("TELÉFONO", key=k("tel"))
        celular = f5.text_input("CELULAR", key=k("cel"))

    # ---- Modelos ----
    st.divider()
    st.subheader("🛍️ Productos")

    with st.form(f"vt_form_modelo_{ver}", clear_on_submit=True):
        m1, m2, m3 = st.columns([3, 1, 1])
        modelo_txt = m1.text_input("MODELO")
        cant_txt = m2.number_input("CANT.", min_value=1, value=1, step=1)
        m3.write("")
        m3.write("")
        agregar = m3.form_submit_button("➕ Agregar", use_container_width=True)
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
                st.error(f"❌ El modelo {m} no existe en el inventario. Si no tiene número, usa 'Producto sin número (S/N)'.")
            else:
                if prod["vendido"]:
                    st.warning(f"⚠️ El modelo {m} figura como VENDIDO en el inventario. Se agregó de todos modos; revísalo.")
                if prod["precio"] <= 0:
                    st.warning(f"⚠️ El modelo {m} no tiene REDONDEO SHOWROOM. Captura el precio a mano en la lista.")
                items.append({
                    "id": uuid.uuid4().hex[:8], "es_sn": False, "modelo": prod["modelo"],
                    "descripcion": prod["nombre"] or "Sin nombre", "cantidad": int(cant_txt),
                    "precio_unitario": prod["precio"], "costo_unitario": prod["costo"],
                    "utilidad_bruta_inv": prod["utilidad_bruta"],
                })

    with st.expander("➕ Producto sin número de modelo (S/N)"):
        with st.form(f"vt_form_sn_{ver}", clear_on_submit=True):
            s1, s2 = st.columns(2)
            sn_nombre = s1.text_input("NOMBRE *")
            sn_desc = s2.text_input("DESCRIPCIÓN CORTA")
            s3, s4, s5 = st.columns(3)
            sn_precio = s3.number_input("PRECIO FINAL ($) *", min_value=0.0, step=50.0, format="%.2f")
            sn_costo = s4.number_input("COSTO ($) (opcional)", min_value=0.0, step=10.0, format="%.2f")
            sn_cant = s5.number_input("CANT.", min_value=1, value=1, step=1)
            agregar_sn = st.form_submit_button("➕ Agregar S/N")
        if agregar_sn:
            if not sn_nombre.strip() or sn_precio <= 0:
                st.warning("El S/N necesita nombre y precio final.")
            else:
                desc = sn_nombre.strip() + (f" — {sn_desc.strip()}" if sn_desc.strip() else "")
                items.append({
                    "id": uuid.uuid4().hex[:8], "es_sn": True, "modelo": None,
                    "descripcion": desc, "cantidad": int(sn_cant),
                    "precio_unitario": float(sn_precio), "costo_unitario": float(sn_costo),
                    "utilidad_bruta_inv": 0.0,
                })

    # Descuento (se necesita antes de mostrar los renglones)
    d1, d2, d3 = st.columns([1, 1, 2])
    descuento = d1.number_input("DESCUENTO (%)", min_value=0.0, max_value=100.0, value=0.0,
                                step=1.0, format="%.2f", key=k("desc"))
    con_iva = d3.checkbox("Desglosar IVA (el cliente lo pidió)", key=k("iva"),
                          help="Apagado: subtotal = total. Prendido: subtotal = total / 1.16.")
    anticipo = d2.number_input("ANTICIPO ($)", min_value=0.0, step=100.0, format="%.2f", key=k("anticipo"))

    # Lista de renglones (precio y cantidad editables)
    if items:
        h1, h2, h3, h4, h5 = st.columns([4, 1, 2, 2, 1])
        h1.caption("MODELO / DESCRIPCIÓN")
        h2.caption("CANT.")
        h3.caption("PRECIO UNIT.")
        h4.caption("TOTAL (con desc.)")
        quitar = None
        for it in items:
            c1, c2, c3, c4, c5 = st.columns([4, 1, 2, 2, 1])
            etiqueta = "S/N" if it["es_sn"] else it["modelo"]
            c1.markdown(f"**{etiqueta}** — {it['descripcion']}")
            it["cantidad"] = c2.number_input("cant", min_value=1, value=int(it["cantidad"]), step=1,
                                             key=k(f"cant_{it['id']}"), label_visibility="collapsed")
            it["precio_unitario"] = c3.number_input("precio", min_value=0.0, value=float(it["precio_unitario"]),
                                                    step=50.0, format="%.2f", key=k(f"precio_{it['id']}"),
                                                    label_visibility="collapsed")
            c5.write("")
            if c5.button("🗑️", key=k(f"del_{it['id']}")):
                quitar = it["id"]
            # el total por renglón se pinta después de calcular
        if quitar:
            ss["vt_items"] = [i for i in items if i["id"] != quitar]
            st.rerun()

    calc = calcular(items, descuento, con_iva, anticipo, ss.get(k("status")))

    if items:
        st.caption("Totales por renglón: " + " · ".join(
            f"{('S/N' if it['es_sn'] else it['modelo'])}: ${calc['lineas'][i]:,.2f}"
            for i, it in enumerate(items)))

    # ---- Totales (se llenan más abajo, cuando ya se capturaron los pagos y el status) ----
    st.divider()
    caja_totales = st.container()

    # ---- Pagos ----
    st.divider()
    st.subheader("💳 Pago")
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
    if st.button("➕ Agregar otro método de pago", key=k("add_pago")):
        ss["vt_npagos"] += 1
        st.rerun()
    st.caption("Si hay un solo pago y dejas el monto en 0, se registra por el anticipo (si lo hay) o por el total. "
               "Si el pedido está pendiente, deja la forma de pago vacía.")

    # ---- Estado ----
    st.divider()
    s1, s2 = st.columns(2)
    with s1:
        estado = st.selectbox("OBSERVACIONES (estado del pedido) *", catalogo(sb, "estado_pedido"),
                              index=None, key=k("estado"), placeholder="Elegir…")
    with s2:
        status = _select_con_otro("STATUS *", catalogo(sb, "status"), k("status"))
    observaciones = st.text_area("NOTAS ADICIONALES", key=k("obs"), height=70)

    # Totales con lo ya cobrado (pagos capturados) y el status elegido
    anticipo_ef = anticipo_efectivo(anticipo, pagos, calc["total"])
    calc = calcular(items, descuento, con_iva, anticipo_ef, status)
    with caja_totales:
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("TOTAL", f"${calc['total']:,.2f}")
        t2.metric("COBRADO / ANTICIPO", f"${anticipo_ef:,.2f}")
        t3.metric("SALDO", f"${calc['saldo']:,.2f}")
        if con_iva:
            t4.metric("SUBTOTAL / IVA", f"${calc['subtotal']:,.2f} / ${calc['iva']:,.2f}")
        if ve_costos and items:
            u1, u2, u3 = st.columns(3)
            u1.metric("COSTO", f"${calc['costo']:,.2f}")
            u2.metric("UTILIDAD", f"${calc['utilidad']:,.2f}")
            u3.metric("COMISIÓN TOTAL (2%)", f"${calc['comision']:,.2f}")

    # ---- Guardar ----
    st.divider()
    if st.button("🚀 Guardar pedido", type="primary", use_container_width=True, key=k("guardar")):
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
        if anticipo > calc["total"] + 0.01:
            errores.append("El anticipo no puede ser mayor que el total.")

        if len(pagos) == 1 and pagos[0]["monto"] == 0:
            pagos[0]["monto"] = float(anticipo if anticipo > 0 else calc["total"])
        pagos_validos = [p for p in pagos if p["monto"] > 0]
        if sum(p["monto"] for p in pagos_validos) > calc["total"] + 0.01:
            errores.append("La suma de los pagos es mayor que el total.")

        if errores:
            for e in errores:
                st.error("❌ " + e)
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
                "con_iva": bool(con_iva), "descuento_pct": float(descuento),
                "costo_total": calc["costo"], "subtotal": calc["subtotal"], "iva": calc["iva"],
                "total": calc["total"], "anticipo": float(anticipo_ef),
                "fecha_pago": min(fechas) if fechas else None,
                "created_by": user["nombre"], "origen": "app",
            }
            with st.spinner("Guardando pedido..."):
                try:
                    numero = guardar_pedido(sb, datos, items, calc["lineas"], vendedores, pagos_validos)
                except Exception as ex:
                    st.error(f"❌ No se pudo guardar el pedido: {ex}")
                    return
                avisos = []
                if not cancelado:
                    for it in items:
                        if it["es_sn"]:
                            continue
                        try:
                            a = marcar_disponibilidad(sb, it["modelo"], True)
                            if a:
                                avisos.append(a)
                        except Exception as ex:
                            avisos.append(f"Modelo {it['modelo']}: no se pudo marcar como vendido ({ex}).")
            _clientes_registrados.clear()   # que el cliente nuevo aparezca ya en las sugerencias
            pdf = None
            try:
                pdf = pdf_de_pedido(sb, numero)
            except Exception as ex:
                avisos.append(f"El pedido se guardó, pero no se pudo generar el PDF ({ex}). "
                              "Puedes descargarlo después desde la pestaña Pedidos.")
            ss["vt_ultimo"] = {"numero": numero, "pdf": pdf, "avisos": avisos,
                               "msg": f"🎉 Pedido N° {numero} guardado correctamente."}
            _reset_formulario()
            st.rerun()


# ----------------------------------------------------------------------
# PESTAÑA: PEDIDOS (consulta y cancelación)
# ----------------------------------------------------------------------
def tab_pedidos(sb, user):
    ve_costos = user["role"] in ROLES_COSTOS
    puede_cancelar = user["role"] in ROLES_CANCELAR

    msg = st.session_state.pop("vp_msg", None)
    if msg:
        st.success(msg)
    for aviso in st.session_state.pop("vp_avisos", []):
        st.warning(aviso)

    busq = st.text_input("Buscar por N° de pedido o por cliente", key="vp_busq").strip()

    f1, f2 = st.columns([3, 2])
    status_opts = catalogo(sb, "status")
    filtro_status = f1.multiselect("Filtrar por status", status_opts, key="vp_f_status",
                                   placeholder="Todos los status")
    sin_costo = False
    if ve_costos:
        sin_costo = f2.checkbox("Solo pedidos sin costo", key="vp_f_sincosto",
                                help="Pedidos con costo total en $0 (sin costo capturado). No incluye cancelados.")
    hay_filtro = bool(busq or filtro_status or sin_costo)
    limite = 500 if hay_filtro else 50

    q = (sb.table("pedidos")
         .select("numero_pedido,fecha,cliente,total,anticipo,saldo,status,estado_pedido,costo_total")
         .order("numero_pedido", desc=True).limit(limite))
    if busq:
        if busq.isdigit():
            q = q.eq("numero_pedido", int(busq))
        else:
            q = q.ilike("cliente", f"%{re.sub(r'[%,()]', '', busq)}%")
    if filtro_status:
        q = q.in_("status", filtro_status)
    if sin_costo:
        q = q.eq("costo_total", 0).neq("status", "Cancelado")
    rows = q.execute().data
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
        "costo_total": "COSTO"})
    if not ve_costos:
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
    st.write(f"📅 {ped['fecha']}  |  👤 {', '.join(v['vendedor'] for v in vends) or '—'}  |  "
             f"🏷️ {ped.get('categoria_venta') or '—'}")
    st.write(f"**Observaciones:** {ped.get('estado_pedido') or '—'}  |  **Status:** {ped.get('status') or '—'}")
    m1, m2, m3 = st.columns(3)
    m1.metric("TOTAL", f"${_num(ped['total']):,.2f}")
    m2.metric("ANTICIPO", f"${_num(ped['anticipo']):,.2f}")
    m3.metric("SALDO", f"${_num(ped['saldo']):,.2f}")
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

    try:
        pdf = _generar_pdf(ped, items, [v["vendedor"] for v in vends])
        st.download_button(f"🖨️ Descargar pedido N° {sel} (PDF)", pdf, file_name=f"pedido_{sel}.pdf",
                           mime="application/pdf", key=f"vp_pdf_{sel}")
    except Exception as ex:
        st.caption(f"No se pudo generar el PDF: {ex}")

    if items:
        dfi = pd.DataFrame(items)
        dfi["modelo"] = dfi["modelo"].fillna("S/N")
        cols = ["modelo", "descripcion", "cantidad", "precio_unitario", "total_linea"]
        if ve_costos:
            cols.append("costo_unitario")
        st.dataframe(dfi[cols], use_container_width=True, hide_index=True)
    elif ped.get("modelos_texto"):
        st.caption("📜 Pedido importado del Excel: no tiene precios por pieza, solo el texto original de los modelos.")
        st.text(ped["modelos_texto"])
    if pagos:
        st.dataframe(pd.DataFrame(pagos)[["forma_pago", "banco_caja", "monto", "fecha_pago"]],
                     use_container_width=True, hide_index=True)

    if user["role"] in ROLES_EDITAR:
        ya_cancelado = ped.get("estado_pedido") == "Cancelado" or ped.get("status") == "Cancelado"
        with st.expander("✏️ Cambiar status / observaciones"):
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
                                            key=f"vp_e_estado_{sel}", placeholder="Elegir…")
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

                if st.button("💾 Guardar cambios", type="primary", key=f"vp_e_guardar_{sel}"):
                    try:
                        mensaje, avisos = actualizar_pedido(sb, ped, nuevo_estado, nuevo_status, notas,
                                                            pago=pago, puede_cancelar=puede_cancelar)
                    except ValueError as ex:
                        st.error(f"❌ {ex}")
                    except Exception as ex:
                        st.error(f"❌ No se pudo guardar: {ex}")
                    else:
                        st.session_state["vp_msg"] = mensaje
                        st.session_state["vp_avisos"] = avisos
                        st.rerun()

    if ve_costos:
        with st.expander("🧾 Facturación y efectivo"):
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
            if st.button("💾 Guardar facturación", key=f"vp_f_guardar_{sel}"):
                try:
                    mensaje = actualizar_facturacion(sb, sel, v_efec, v_con, v_sin, v_fact, auto_sin)
                except Exception as ex:
                    st.error(f"❌ No se pudo guardar: {ex}")
                else:
                    st.session_state["vp_msg"] = mensaje
                    st.rerun()

    if puede_cancelar and ped.get("estado_pedido") != "Cancelado" and ped.get("status") != "Cancelado":
        with st.expander("⚠️ Cancelar este pedido"):
            st.warning("Marca el pedido como CANCELADO y devuelve sus modelos a DISPONIBLE en el inventario.")
            ok = st.checkbox("Confirmo que deseo cancelar este pedido", key=f"vp_conf_{sel}")
            if ok and st.button("🗑️ Cancelar pedido", type="primary", key=f"vp_cancel_{sel}"):
                try:
                    sb.table("pedidos").update(
                        {"estado_pedido": "Cancelado", "status": "Cancelado"}
                    ).eq("numero_pedido", sel).execute()
                    avisos = liberar_modelos(sb, sel)
                except Exception as ex:
                    st.error(f"❌ Error al cancelar: {ex}")
                    return
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
    solo_pagados = st.checkbox("Solo pedidos con status Pagado", key="vc_pagados")

    q = sb.table("v_comisiones").select("*").eq("anio", anio).eq("mes", mes).order("fecha")
    if vend:
        q = q.in_("vendedor", vend)
    rows = q.range(0, 4999).execute().data
    if not rows:
        st.info("No hay comisiones para ese mes.")
        return

    df = pd.DataFrame(rows)
    if solo_pagados:
        df = df[df["status"] == "Pagado"]
        if df.empty:
            st.info("No hay pedidos pagados para ese filtro.")
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
        st.download_button("⬇️ Descargar detalle (CSV)", det.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"comisiones_{anio}_{mes:02d}.csv", mime="text/csv")


# ----------------------------------------------------------------------
# PESTAÑA: RESUMEN MENSUAL (ventas sin IVA, costos y utilidad por mes)
# ----------------------------------------------------------------------
def resumen_mensual(sb, anio, solo_pagados=False):
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
        df = df[df["status"] == "Pagado"]
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
    solo_pagados = c2.checkbox("Solo pedidos con status Pagado", key="vr_pagados")
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
        st.caption("⚠️ \"Pedidos sin costo\" son pedidos con costo $0: su utilidad sale igual a la venta. "
                   "Puedes encontrarlos en 📋 Pedidos con el filtro \"Solo pedidos sin costo\".")

    grafica = df.assign(MES=[MESES[m - 1][:3] for m in df["mes"]]).set_index("MES")[["ventas", "costos", "utilidad"]]
    st.bar_chart(grafica.rename(columns={"ventas": "Ventas sin IVA", "costos": "Costos", "utilidad": "Utilidad"}))
    st.download_button("⬇️ Descargar resumen (CSV)", tabla.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"resumen_mensual_{anio}.csv", mime="text/csv")


# ----------------------------------------------------------------------
# PUNTO DE ENTRADA
# ----------------------------------------------------------------------
def render_ventas(sb, user):
    st.header("🧾 Ventas y Pedidos")
    try:
        catalogo(sb, "vendedor")
    except Exception as ex:
        st.error(f"No se pudieron cargar los catálogos de ventas. ¿Ya corriste ventas_schema.sql en Supabase? Detalle: {ex}")
        return

    secciones = [("🧾 Nuevo pedido", tab_nuevo_pedido), ("📋 Pedidos", tab_pedidos)]
    if user["role"] in ROLES_COSTOS:
        secciones.append(("📊 Resumen mensual", tab_resumen))
    if user["role"] in ROLES_COMISIONES:
        secciones.append(("💰 Comisiones", tab_comisiones))
    for tab, (_, funcion) in zip(st.tabs([n for n, _ in secciones]), secciones):
        with tab:
            funcion(sb, user)