"""
RENTAS: cotización de renta de piezas (para producciones), firma del cliente, confirmación y devolución.

  * Cotizar: cliente, fecha de RETIRO, DÍAS QUE SE COBRAN (los de gracia no se cobran) y fecha de DEVOLUCIÓN.
    Cada pieza: precio con IVA (REDONDEO SHOWROOM) × % de renta por día (20 % editable) × cantidad × días.
    Garantía = 80 % (editable) del valor de las piezas sin IVA. Cotizar NO aparta las piezas.
  * PDF igual al Excel «Renta LOLA Films» (renta_pdf.py).
  * Link para firmar: el cliente ve el PDF entero (como imágenes) y solo al llegar al final puede firmar.
  * Confirmar: las piezas quedan «En renta» desde el retiro hasta que se registra la devolución (antes del retiro
    se ven «Rentada del … al …» y se pueden vender con aviso). En renta: se pueden cotizar, NO vender.
    Con varias unidades se avisa cuántas están en renta.
  * Devolución: cómo regresó cada pieza y qué se cobra (el % de la renta, o más si hubo daño; las que no regresaron
    se cobran completas y quedan VENDIDAS). Se crea la nota de pedido con su pago (y entra a Caudal), y la renta
    queda «Devuelta»: las piezas se liberan.

Tablas (correr una vez en Supabase): ver RENTAS_SQL.
"""
import datetime as dt
import json
import secrets
import urllib.parse

import streamlit as st

import codigos
from caches import guardar_en_memoria

RENTAS_SQL = """
create table if not exists rentas (
  id bigserial primary key,
  creado timestamptz not null default now(),
  actualizado timestamptz not null default now(),
  cliente text not null,
  contacto text,
  retiro date not null,                 -- el día que se llevan las piezas
  devolucion date not null,             -- el día que las regresan
  dias int not null default 1,          -- días que SE COBRAN
  garantia_pct numeric not null default 80,
  estado text not null default 'cotizacion',   -- cotizacion | confirmada | devuelta | cancelada
  creado_por text,
  confirmado timestamptz, confirmado_por text,
  devuelto timestamptz, devuelto_por text,
  numero_pedido int,                    -- la nota de pedido que se hizo en la devolución
  cobrado numeric
);
create table if not exists renta_items (
  id bigserial primary key,
  renta_id bigint not null references rentas(id) on delete cascade,
  modelo text not null,
  nombre text,
  precio numeric not null default 0,    -- precio con IVA por unidad (redondeo showroom) al cotizar
  pct numeric not null default 20,      -- % de renta por día
  cantidad int not null default 1,
  regreso text,                         -- en la devolución: bien | dano | no
  no_regresaron int not null default 0,
  pct_cobrado numeric,
  cobro numeric
);
create index if not exists renta_items_modelo on renta_items (modelo);
create index if not exists rentas_estado on rentas (estado);
"""

TABLA, ITEMS = "rentas", "renta_items"
COTIZACION, CONFIRMADA, DEVUELTA, CANCELADA = "cotizacion", "confirmada", "devuelta", "cancelada"
NOMBRE_ESTADO = {COTIZACION: "Cotización", CONFIRMADA: "Confirmada", DEVUELTA: "Devuelta", CANCELADA: "Cancelada"}
PCT_RENTA, PCT_GARANTIA = 20, 80
DIAS_ENLACE = 3
BUCKET = "imagenes"
REGRESO = {"bien": "Regresó bien", "dano": "Regresó con daño", "no": "No regresó"}


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
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
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def _hoy():
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).date()


def _fecha(v):
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _corta(v):
    d = _fecha(v)
    return d.strftime("%d/%m/%Y") if d else "—"


def dinero(x):
    return f"${_num(x):,.2f}"


def _ahora():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def calcular(items, dias, garantia_pct=PCT_GARANTIA):
    from renta_pdf import calcular as _c
    return _c(items, dias, garantia_pct)


# ----------------------------------------------------------------------
# Datos
# ----------------------------------------------------------------------
def leer(sb, rid):
    r = sb.table(TABLA).select("*").eq("id", int(rid)).limit(1).execute().data
    if not r:
        return None, []
    its = sb.table(ITEMS).select("*").eq("renta_id", int(rid)).order("id").execute().data or []
    return r[0], its


def ficha(sb, modelo):
    """Lo que hace falta de la ficha para rentar: nombre, precio, unidades y si está vendida / reservada."""
    r = sb.table("productos").select("MODELO,datos_json").eq("MODELO", str(modelo).strip()).limit(1).execute().data
    if not r:
        return None
    d = _parse(r[0].get("datos_json"))
    disp = str(d.get("DISP", d.get("DISPONIBILIDAD", "")) or "").strip().lower()
    return {"modelo": str(r[0]["MODELO"]), "nombre": " ".join(str(d.get("NOMBRE") or "").split()),
            "precio": _num(d.get("REDONDEO_SHOWROOM")), "unidades": int(_num(d.get("CANTIDAD"), 1.0)),
            "vendido": disp == "v", "reservado": disp == "r", "costo": _num(d.get("TOTAL_COSTO", d.get("COSTO_REGISTRADO"))),
            "utilidad": _num(d.get("UTILIDAD_BRUTA", d.get("UTILIDAD_SHOWROOM")))}


@guardar_en_memoria(ttl=60)
def _confirmadas_mem(_sb):
    rs = _sb.table(TABLA).select("id,cliente,retiro,devolucion").eq("estado", CONFIRMADA).execute().data or []
    if not rs:
        return {}
    its = (_sb.table(ITEMS).select("renta_id,modelo,cantidad").in_("renta_id", [r["id"] for r in rs])
           .execute().data or [])
    por_id = {r["id"]: r for r in rs}
    out = {}
    for it in its:
        r = por_id.get(it["renta_id"])
        if r:
            out.setdefault(str(it["modelo"]).strip(), []).append(
                {"renta": r["id"], "cliente": r.get("cliente") or "", "retiro": r["retiro"],
                 "devolucion": r["devolucion"], "cantidad": int(_num(it.get("cantidad"), 1))})
    return out


def rentas_de(sb, modelos=None):
    """{modelo: [rentas confirmadas]} de todas o solo de `modelos`. Si falta la tabla, {}."""
    try:
        todo = _confirmadas_mem(sb)
    except Exception:
        return {}
    if modelos is None:
        return todo
    return {m: todo[m] for m in (str(x).strip() for x in modelos) if m in todo}


def estado_renta(rs, hoy=None):
    """De las rentas confirmadas de UN modelo: ("en_renta", texto, unidades) si alguna ya empezó (no se puede
    vender), ("futura", texto, unidades) si solo hay rentas por venir, o (None, "", 0)."""
    hoy = hoy or _hoy()
    if not rs:
        return None, "", 0
    ya = [r for r in rs if _fecha(r["retiro"]) and _fecha(r["retiro"]) <= hoy]
    if ya:
        u = sum(r["cantidad"] for r in ya)
        hasta = max(_fecha(r["devolucion"]) for r in ya)
        return "en_renta", f"En renta hasta el {hasta.strftime('%d/%m')} · {ya[0]['cliente']}", u
    r = min(rs, key=lambda x: _fecha(x["retiro"]))
    return ("futura", f"Rentada del {_fecha(r['retiro']).strftime('%d/%m')} al {_fecha(r['devolucion']).strftime('%d/%m')}"
            f" · {r['cliente']}", sum(x["cantidad"] for x in rs))


def etiqueta(rs, unidades_ficha=1):
    """Para las tarjetas de Buscar y la app: ("en_renta" | "futura" | None, texto). Con varias unidades dice cuántas."""
    est, txt, u = estado_renta(rs)
    if est and _num(unidades_ficha, 1) > 1:
        txt = (f"{u} en renta" + txt[len("En renta"):]) if est == "en_renta" else (f"{u} rentadas" + txt[len("Rentada"):])
    return est, txt


def bloqueo_venta(sb, modelo, unidades_ficha=None, pide=1):
    """Texto de error si el modelo NO se puede vender por estar en renta (None si se puede).
    Con varias unidades: solo bloquea si las que quedan libres no alcanzan."""
    rs = rentas_de(sb, [modelo]).get(str(modelo).strip())
    est, txt, u = estado_renta(rs)
    if est != "en_renta":
        return None
    if unidades_ficha is not None and unidades_ficha - u >= max(int(pide or 1), 1):
        return None
    return (f"El modelo {modelo} está en renta {txt[len('En renta '):]}. No se puede vender hasta que se "
            "registre su devolución en Rentas.")


def aviso_venta(sb, modelo):
    """Aviso (no bloquea) si el modelo tiene una renta por venir."""
    rs = rentas_de(sb, [modelo]).get(str(modelo).strip())
    est, txt, _ = estado_renta(rs)
    return f"Ojo: el modelo {modelo} está rentado {txt[len('Rentada '):]}. Si lo vendes, avisa a quien lo rentó." \
        if est == "futura" else None


def choques(sb, items, retiro, devolucion, sin_id=None):
    """Piezas que ya están en OTRA renta confirmada que se cruza con estas fechas (o vendidas). Lista de textos."""
    out = []
    r0, r1 = _fecha(retiro), _fecha(devolucion)
    todas = rentas_de(sb)
    for it in items:
        m = str(it["modelo"]).strip()
        f = ficha(sb, m)
        if f is None:
            out.append(f"El modelo {m} ya no existe en el inventario.")
            continue
        if f["vendido"]:
            out.append(f"El modelo {m} está VENDIDO.")
            continue
        cruzan = [r for r in todas.get(m, []) if r["renta"] != sin_id
                  and _fecha(r["retiro"]) <= r1 and _fecha(r["devolucion"]) >= r0]
        ocupadas = sum(r["cantidad"] for r in cruzan)
        if cruzan and (f["unidades"] <= 1 or ocupadas + int(it["cantidad"]) > f["unidades"]):
            r = cruzan[0]
            out.append(f"El modelo {m} ya está rentado del {_corta(r['retiro'])} al {_corta(r['devolucion'])} "
                       f"({r['cliente']}, renta {r['renta']}).")
        elif f["unidades"] > 1 and int(it["cantidad"]) > f["unidades"]:
            out.append(f"Del modelo {m} solo quedan {f['unidades']} unidades.")
    return out


def _limpiar_memoria():
    try:
        import caches
        caches.limpiar()
    except Exception:
        pass


def guardar(sb, rid, datos, items, usuario):
    """Crea (rid None) o reescribe una cotización de renta con sus piezas. Devuelve el id."""
    fila = {**datos, "actualizado": _ahora()}
    if rid:
        sb.table(TABLA).update(fila).eq("id", rid).execute()
        sb.table(ITEMS).delete().eq("renta_id", rid).execute()
        _caducar_enlaces(sb, rid)                 # lo que el cliente iba a firmar ya cambió
    else:
        rid = sb.table(TABLA).insert({**fila, "estado": COTIZACION, "creado_por": usuario}).execute().data[0]["id"]
    sb.table(ITEMS).insert([{"renta_id": rid, "modelo": it["modelo"], "nombre": it.get("nombre"),
                             "precio": round(_num(it.get("precio")), 2), "pct": _num(it.get("pct"), PCT_RENTA),
                             "cantidad": max(int(_num(it.get("cantidad"), 1)), 1)} for it in items]).execute()
    codigos.registrar(sb, usuario, "Cotización de renta " + ("editada" if datos.get("_editada") else "guardada"),
                      "renta", rid, f"{datos.get('cliente')} · {len(items)} pieza(s)")
    _limpiar_memoria()
    return rid


def cambiar_estado(sb, rid, estado, usuario, extra=None):
    fila = {"estado": estado, "actualizado": _ahora(), **(extra or {})}
    if estado == CONFIRMADA:
        fila.update({"confirmado": _ahora(), "confirmado_por": usuario})
    sb.table(TABLA).update(fila).eq("id", rid).execute()
    codigos.registrar(sb, usuario, f"Renta {NOMBRE_ESTADO.get(estado, estado).lower()}", "renta", rid, "")
    _limpiar_memoria()


# ----------------------------------------------------------------------
# Firma del cliente (link /f/<token> de la app, como los pedidos)
# ----------------------------------------------------------------------
def _base_url():
    try:
        return str(st.secrets.get("FICHA_PUBLICA_URL", "https://viriathusapp.vercel.app")).strip().rstrip("/")
    except Exception:
        return "https://viriathusapp.vercel.app"


def firma_de(sb, rid):
    try:
        import firmas
        return firmas.leer(sb, "renta", rid)
    except Exception:
        return None


def enlace_vigente(sb, rid):
    try:
        r = (sb.table("firma_enlaces").select("*").eq("tipo", "renta").eq("referencia", str(rid))
             .is_("firmado", "null").gt("vence", _ahora()).order("id", desc=True).limit(1).execute().data)
        return r[0] if r else None
    except Exception:
        return None


def _caducar_enlaces(sb, rid):
    try:
        sb.table("firma_enlaces").update({"vence": _ahora()}).eq("tipo", "renta").eq("referencia", str(rid)) \
            .is_("firmado", "null").execute()
    except Exception:
        pass


def _fotos(sb, items):
    """Agrega "foto_bytes" a cada pieza (la foto del inventario), descargando en paralelo."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        from cotizaciones import _url_foto_inventario
        from shopify_borrador import http
    except Exception:
        return items

    def bajar(it):
        try:
            r = http("get", _url_foto_inventario(sb, it["modelo"]), timeout=8)
            return r.content if r.status_code == 200 and r.content else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        fotos = list(ex.map(bajar, items))
    return [{**it, "foto_bytes": f} for it, f in zip(items, fotos)]


def pdf(sb, renta, items, con_firma=True):
    from renta_pdf import generar_pdf_renta
    firma = None
    if con_firma:
        f = firma_de(sb, renta["id"])
        if f:
            import firmas
            firma = firmas.para_pdf(f)
    return generar_pdf_renta(renta, _fotos(sb, items), firma)


def crear_enlace(sb, renta, items, usuario):
    """Arma el PDF, lo sube (y cada hoja como imagen) a Storage y crea el link para que el cliente lo lea y firme."""
    ya = enlace_vigente(sb, renta["id"])
    if ya:
        return ya
    from renta_pdf import pdf_a_imagenes
    doc = pdf(sb, renta, items, con_firma=False)
    hojas = pdf_a_imagenes(doc)
    token = secrets.token_urlsafe(12)
    carpeta = f"rentas/{token}"
    st_ = sb.storage.from_(BUCKET)
    for i, img in enumerate(hojas, 1):
        st_.upload(path=f"{carpeta}/{i}.jpg", file=img, file_options={"content-type": "image/jpeg"})
    st_.upload(path=f"{carpeta}/cotizacion.pdf", file=doc, file_options={"content-type": "application/pdf"})
    _, t = calcular(items, renta.get("dias"), renta.get("garantia_pct"))
    fila = {"token": token, "tipo": "renta", "referencia": str(renta["id"]), "creado_por": usuario,
            "vence": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=DIAS_ENLACE)).isoformat(),
            "total": t["renta"], "detalle": {"hojas": len(hojas), "garantia": t["garantia"], "valor": t["subtotal"]}}
    r = sb.table("firma_enlaces").insert(fila).execute().data
    codigos.registrar(sb, usuario, "Link para firmar renta", "renta", renta["id"], f"vence {fila['vence'][:16]}")
    return r[0] if r else fila


# ----------------------------------------------------------------------
# Devolución → nota de pedido (y Caudal, por sus pagos)
# ----------------------------------------------------------------------
def cobro_item(it, dias, regreso, no_regresaron, pct):
    """Lo que se cobra de una pieza: renta (precio × % × cantidad × días) + las unidades que no regresaron, completas."""
    cant = max(int(_num(it.get("cantidad"), 1)), 1)
    renta = round(_num(it.get("precio")) * _num(pct) / 100 * cant * max(int(_num(dias, 1)), 0), 2)
    faltan = cant if regreso == "no" and cant == 1 else min(max(int(no_regresaron or 0), 0), cant)
    if regreso == "no" and cant > 1 and not faltan:
        faltan = cant
    return renta, faltan, round(_num(it.get("precio")) * faltan, 2)


def registrar_devolucion(sb, renta, items, decisiones, pago, usuario):
    """decisiones = {item_id: {"regreso", "no_regresaron", "pct"}}; pago = {forma_pago, banco_caja, fecha_pago}.
    Crea la nota de pedido (renta + piezas que no regresaron), vende esas piezas y marca la renta «devuelta».
    Devuelve (numero_pedido, avisos)."""
    from ventas import guardar_pedido, vender_modelo
    dias = renta.get("dias")
    lineas_ped, total, costo, util, vender = [], 0.0, 0.0, 0.0, []
    for it in items:
        dcs = decisiones[it["id"]]
        cobro_r, faltan, cobro_f = cobro_item(it, dias, dcs["regreso"], dcs.get("no_regresaron"), dcs["pct"])
        cant = max(int(_num(it.get("cantidad"), 1)), 1)
        if cobro_r > 0:
            lineas_ped.append({"es_sn": True, "modelo": None, "cantidad": 1, "precio_unitario": cobro_r,
                               "descripcion": f"Renta Mod {it['modelo']} · {it.get('nombre') or ''} · {cant} u. × "
                                              f"{dias} día(s) × {_num(dcs['pct']):g}%"
                                              + (" (con daño)" if dcs["regreso"] == "dano" else ""),
                               "costo_unitario": 0.0, "utilidad_bruta_inv": 0.0, "descuento_pct": 0.0})
            total += cobro_r
        if faltan:
            f = ficha(sb, it["modelo"]) or {}
            lineas_ped.append({"es_sn": False, "modelo": it["modelo"], "cantidad": faltan,
                               "precio_unitario": round(_num(it.get("precio")), 2),
                               "descripcion": f"{it.get('nombre') or ''} (no regresó de la renta {renta['id']})",
                               "costo_unitario": _num(f.get("costo")), "utilidad_bruta_inv": _num(f.get("utilidad")),
                               "descuento_pct": 0.0})
            total += cobro_f
            costo += _num(f.get("costo")) * faltan
            vender.append((it["modelo"], faltan))
        sb.table(ITEMS).update({"regreso": dcs["regreso"], "no_regresaron": faltan, "pct_cobrado": _num(dcs["pct"]),
                                "cobro": round(cobro_r + cobro_f, 2)}).eq("id", it["id"]).execute()
    total = round(total, 2)
    subtotal = round(total / 1.16, 2)
    datos = {"fecha": _hoy().isoformat(), "cliente": renta["cliente"], "categoria_venta": "Renta",
             "estado_pedido": "Pagado y entregado", "status": "Pagado",
             "observaciones": f"Renta {renta['id']}: retiro {_corta(renta['retiro'])}, devolución "
                              f"{_corta(renta['devolucion'])}, {dias} día(s) cobrado(s).",
             "telefono": renta.get("contacto") or None, "con_iva": True, "descuento_pct": 0.0,
             "costo_total": round(costo, 2), "subtotal": subtotal, "iva": round(total - subtotal, 2), "total": total,
             "anticipo": total, "fecha_pago": pago["fecha_pago"], "created_by": usuario, "origen": "renta"}
    pagos = [{"forma_pago": pago["forma_pago"], "banco_caja": pago.get("banco_caja"), "monto": total,
              "fecha_pago": pago["fecha_pago"]}] if total > 0 else []
    numero = guardar_pedido(sb, datos, lineas_ped, [x["precio_unitario"] * x["cantidad"] for x in lineas_ped],
                            [usuario], pagos)
    avisos = []
    for m, u in vender:
        try:
            avisos += vender_modelo(sb, m, numero, u)
        except Exception as ex:
            avisos.append(f"Modelo {m}: no se pudo marcar como vendido ({ex}).")
    cambiar_estado(sb, renta["id"], DEVUELTA, usuario, {"devuelto": _ahora(), "devuelto_por": usuario,
                                                        "numero_pedido": numero, "cobrado": total})
    codigos.registrar(sb, usuario, "Devolución de renta", "renta", renta["id"], f"pedido {numero} · {dinero(total)}")
    return numero, avisos


# ----------------------------------------------------------------------
# Pantallas
# ----------------------------------------------------------------------
def _msgs():
    for tipo, txt in st.session_state.pop("rt_msgs", []):
        getattr(st, tipo)(txt)


def _ok(txt, tipo="success"):
    st.session_state.setdefault("rt_msgs", []).append((tipo, txt))


def _form_reset(cargar=None):
    ss = st.session_state
    for k in [k for k in ss.keys() if str(k).startswith("rtf_")]:
        ss.pop(k, None)
    ss["rt_form_n"] = ss.get("rt_form_n", 0) + 1      # campos nuevos (vacíos o con la renta a editar)
    ss["rtf_v"] = ss["rt_form_n"]
    if cargar:
        r, its = cargar
        ss["rtf_id"] = r["id"]
        ss["rtf_items"] = [{"modelo": i["modelo"], "nombre": i.get("nombre"), "precio": _num(i.get("precio")),
                            "pct": _num(i.get("pct"), PCT_RENTA), "cantidad": int(_num(i.get("cantidad"), 1)),
                            "unidades": None} for i in its]
        ss["rtf_datos"] = {"cliente": r.get("cliente") or "", "contacto": r.get("contacto") or "",
                           "retiro": _fecha(r["retiro"]), "devolucion": _fecha(r["devolucion"]),
                           "dias": int(_num(r.get("dias"), 1)), "garantia_pct": _num(r.get("garantia_pct"), PCT_GARANTIA)}


def _agregar_pieza(sb):
    ss = st.session_state
    m = str(ss.get(f"rtf_modelo_{ss.get('rtf_v', 0)}") or "").strip()
    if not m:
        return
    items = ss.setdefault("rtf_items", [])
    if any(i["modelo"] == m for i in items):
        ss["rtf_aviso"] = ("warning", f"El modelo {m} ya está en la lista.")
        return
    f = ficha(sb, m)
    if f is None:
        ss["rtf_aviso"] = ("error", f"No existe el modelo {m}.")
        return
    if f["vendido"]:
        ss["rtf_aviso"] = ("error", f"El modelo {m} está VENDIDO: no se puede rentar.")
        return
    if f["precio"] <= 0:
        ss["rtf_aviso"] = ("warning", f"El modelo {m} no tiene REDONDEO SHOWROOM: la renta saldrá en $0.")
    items.append({"modelo": m, "nombre": f["nombre"], "precio": f["precio"], "pct": PCT_RENTA, "cantidad": 1,
                  "unidades": f["unidades"]})
    ss[f"rtf_modelo_{ss.get('rtf_v', 0)}"] = ""


def tab_nueva(sb, user):
    ss = st.session_state
    _msgs()
    v = ss.get("rtf_v", 0)
    rid = ss.get("rtf_id")
    d0 = ss.get("rtf_datos") or {}
    if rid:
        c1, c2 = st.columns([4, 1])
        c1.info(f"Editando la cotización de renta **{rid}**.")
        if c2.button("Cancelar edición", key=f"rtf_cancelar_{v}", use_container_width=True):
            _form_reset()
            st.rerun()
    hoy = _hoy()
    c1, c2, c3 = st.columns([2, 1, 1])
    cliente = c1.text_input("Cliente / producción *", value=d0.get("cliente", ""), key=f"rtf_cliente_{v}")
    contacto = c2.text_input("Teléfono / correo", value=d0.get("contacto", ""), key=f"rtf_contacto_{v}")
    garantia_pct = c3.number_input("Garantía (% del valor sin IVA)", 0.0, 200.0, float(d0.get("garantia_pct", PCT_GARANTIA)),
                                   step=5.0, key=f"rtf_gar_{v}")
    f1, f2, f3 = st.columns(3)
    retiro = f1.date_input("Fecha de retiro (se las llevan)", value=d0.get("retiro") or hoy, format="DD/MM/YYYY",
                           key=f"rtf_retiro_{v}")
    dias = f2.number_input("Días de renta que se cobran", 1, 365, int(d0.get("dias", 1)), key=f"rtf_dias_{v}")
    devolucion = f3.date_input("Fecha de devolución (regresan)", value=d0.get("devolucion") or retiro,
                               format="DD/MM/YYYY", key=f"rtf_dev_{v}")
    ocupan = (devolucion - retiro).days + 1 if devolucion and retiro else 0
    if ocupan > 0:
        st.caption(f"Ocupan {ocupan} día(s) (del {retiro:%d/%m} al {devolucion:%d/%m}) · se cobran {dias}"
                   + (f" · {ocupan - dias} de gracia" if ocupan > dias else ""))

    st.subheader("Piezas")
    a1, a2 = st.columns([3, 1])
    a1.text_input("Agregar por modelo", key=f"rtf_modelo_{v}", placeholder="Ej. 28206 y Enter",
                  on_change=_agregar_pieza, args=(sb,))
    a2.write("")
    a2.button("Agregar", key=f"rtf_add_{v}", on_click=_agregar_pieza, args=(sb,), use_container_width=True)
    if ss.get("rtf_aviso"):
        tipo, txt = ss.pop("rtf_aviso")
        getattr(st, tipo)(txt)
    items = ss.setdefault("rtf_items", [])
    quitar = None
    if items:
        cab = st.columns([1, 3, 1.4, 1, 1, 1.4, 0.8])
        for c, t in zip(cab, ["Modelo", "Nombre", "Precio c/IVA", "% renta/día", "Cant.", "Renta", ""]):
            c.markdown(f"**{t}**")
        for i, it in enumerate(items):
            c = st.columns([1, 3, 1.4, 1, 1, 1.4, 0.8])
            c[0].write(it["modelo"])
            c[1].write(it.get("nombre") or "—")
            c[2].write(dinero(it["precio"]))
            it["pct"] = c[3].number_input("%", 0.0, 100.0, float(it["pct"]), step=1.0, key=f"rtf_pct_{v}_{i}",
                                          label_visibility="collapsed")
            maxu = it.get("unidades") or 999
            it["cantidad"] = int(c[4].number_input("Cant.", 1, max(int(maxu), 1), min(int(it["cantidad"]), max(int(maxu), 1)),
                                                   key=f"rtf_cant_{v}_{i}", label_visibility="collapsed"))
            c[5].write(dinero(round(it["precio"] * it["pct"] / 100, 2) * it["cantidad"] * dias))
            if c[6].button("Quitar", key=f"rtf_q_{v}_{i}"):
                quitar = i
        if quitar is not None:
            items.pop(quitar)
            st.rerun()
        _, t = calcular(items, dias, garantia_pct)
        m1, m2, m3 = st.columns(3)
        m1.metric("Valor de los objetos sin IVA", dinero(t["subtotal"]))
        m2.metric("Total de la renta con IVA", dinero(t["renta"]), help="Depósito / transferencia a Ensalada de Ideas")
        m3.metric(f"Garantía ({garantia_pct:g} %)", dinero(t["garantia"]),
                  help="A Ernesto V. Cuenca Madrigal o efectivo · sin tarjeta ni cheque")
        st.caption("Renta = precio con IVA × % × cantidad × días que se cobran · la cotización NO aparta las piezas.")
    else:
        st.caption("Agrega las piezas por número de modelo.")

    st.divider()
    q_v, q_c = codigos.pedir_identidad(sb, f"rtf_id_{v}")
    if st.button("Guardar cotización de renta", type="primary", key=f"rtf_guardar_{v}", use_container_width=True):
        errores = []
        if not cliente.strip():
            errores.append("Falta el cliente.")
        if not items:
            errores.append("Agrega al menos una pieza.")
        if devolucion < retiro:
            errores.append("La devolución no puede ser antes del retiro.")
        for e in errores:
            st.error(e)
        if errores:
            return
        quien = codigos.confirmar(sb, q_v, q_c)
        if not quien:
            return
        datos = {"cliente": " ".join(cliente.split()), "contacto": contacto.strip() or None,
                 "retiro": retiro.isoformat(), "devolucion": devolucion.isoformat(), "dias": int(dias),
                 "garantia_pct": float(garantia_pct)}
        try:
            nuevo = guardar(sb, rid, datos, items, quien)
        except Exception as ex:
            st.error(f"No se pudo guardar: {ex}. ¿Ya corriste en Supabase el SQL de rentas?")
            with st.expander("SQL de rentas"):
                st.code(RENTAS_SQL, language="sql")
            return
        _form_reset()
        ss["rt_sel"] = nuevo
        ss["rt_ir"] = "Rentas"
        _ok(f"Cotización de renta {nuevo} guardada. Desde aquí descargas el PDF o mandas el link para firmar.")
        st.rerun()


def _lista(_sb, filtro):
    q = _sb.table(TABLA).select("*").order("id", desc=True).limit(200)
    if filtro == "Abiertas":
        q = q.in_("estado", [COTIZACION, CONFIRMADA])
    elif filtro == "Devueltas":
        q = q.eq("estado", DEVUELTA)
    else:
        q = q.eq("estado", CANCELADA)
    return q.execute().data or []


def tab_rentas(sb, user):
    ss = st.session_state
    _msgs()
    c1, c2 = st.columns([2, 2])
    filtro = c1.radio("Ver", ["Abiertas", "Devueltas", "Canceladas"], horizontal=True, key="rt_filtro",
                      label_visibility="collapsed")
    busca = c2.text_input("Buscar cliente", key="rt_busca", placeholder="Buscar cliente…", label_visibility="collapsed")
    try:
        filas = _lista(sb, filtro)
    except Exception as ex:
        st.error(f"No se pudieron leer las rentas. ¿Ya corriste en Supabase el SQL de rentas? ({ex})")
        st.code(RENTAS_SQL, language="sql")
        return
    if busca.strip():
        b = busca.strip().lower()
        filas = [f for f in filas if b in str(f.get("cliente") or "").lower() or b == str(f["id"])]
    if not filas:
        st.info("No hay rentas aquí.")
        return
    try:
        firmadas = {str(x["referencia"]) for x in (sb.table("firmas").select("referencia").eq("tipo", "renta")
                                                    .in_("referencia", [str(f["id"]) for f in filas]).execute().data or [])}
    except Exception:
        firmadas = set()
    ids = [f["id"] for f in filas]
    if ss.get("rt_sel") not in ids:
        ss["rt_sel"] = ids[0]

    def etiqueta(rid):
        f = next(x for x in filas if x["id"] == rid)
        est = NOMBRE_ESTADO.get(f["estado"], f["estado"])
        if f["estado"] == CONFIRMADA and _fecha(f["retiro"]) and _fecha(f["retiro"]) <= _hoy():
            est = "En renta"
        elif f["estado"] == COTIZACION and str(rid) in firmadas:
            est = "Firmada"
        return f"{rid} · {f.get('cliente')} · {_corta(f['retiro'])} → {_corta(f['devolucion'])} · {est}"

    rid = st.selectbox("Renta", ids, key="rt_sel", format_func=etiqueta)
    st.divider()
    _detalle(sb, user, rid)


def _detalle(sb, user, rid):
    ss = st.session_state
    r, its = leer(sb, rid)
    if not r:
        st.warning("Esa renta ya no existe.")
        return
    _, t = calcular(its, r["dias"], r.get("garantia_pct"))
    firma = firma_de(sb, rid)
    st.subheader(f"Renta {rid} · {r['cliente']}")
    st.caption(f"Hizo: {r.get('creado_por') or '—'} · {_corta(r.get('creado'))}"
               + (f" · tel./correo {r['contacto']}" if r.get("contacto") else ""))
    c = st.columns(4)
    c[0].metric("Retiro", _corta(r["retiro"]))
    c[1].metric("Días cobrados", r["dias"])
    c[2].metric("Devolución", _corta(r["devolucion"]))
    c[3].metric("Estado", NOMBRE_ESTADO.get(r["estado"], r["estado"]))
    for it in its:
        st.markdown(f"- **{it['modelo']}** · {it.get('nombre') or ''} · {int(it['cantidad'])} u. × "
                    f"{_num(it['pct']):g}% → {dinero(_num(it['precio']) * _num(it['pct']) / 100 * int(it['cantidad']) * r['dias'])}"
                    + (f" · cobrado {dinero(it.get('cobro'))} ({REGRESO.get(it.get('regreso'), '')})"
                       if r["estado"] == DEVUELTA else ""))
    m1, m2, m3 = st.columns(3)
    m1.metric("Total renta con IVA", dinero(t["renta"]))
    m2.metric(f"Garantía ({t['garantia_pct']:g} %)", dinero(t["garantia"]))
    m3.metric("Valor sin IVA", dinero(t["subtotal"]))

    # Firma: a distancia (link) o aquí en la tienda (firmas.widget: también la muestra y permite borrarla)
    if r["estado"] in (COTIZACION, CONFIRMADA):
        if not firma:
            _seccion_enlace(sb, user, r, its)
        try:
            import firmas
            firma = firmas.widget(sb, "renta", rid, "✍️ Firma del cliente (aquí en la tienda / ver la firma)")
        except Exception as ex:
            st.caption(f"No se pudo mostrar la firma: {ex}")
    elif firma:
        st.caption(f"✍️ Firmada por {firma.get('nombre')} · el PDF lleva su firma.")
    if r["estado"] == DEVUELTA and r.get("numero_pedido"):
        st.info(f"Devuelta el {_corta(r.get('devuelto'))} · nota de pedido **{r['numero_pedido']}** por "
                f"{dinero(r.get('cobrado'))}.")
        try:
            from ventas import abrir_pedido
            st.button("Ver la nota de pedido", key=f"rt_verped_{rid}", on_click=abrir_pedido, args=(r["numero_pedido"],))
        except Exception:
            pass

    # PDF
    k_pdf = f"rt_pdf_{rid}"
    b1, b2 = st.columns(2)
    if b1.button("Preparar PDF", key=f"rt_genpdf_{rid}", use_container_width=True):
        with st.spinner("Armando el PDF…"):
            try:
                ss[k_pdf] = pdf(sb, r, its)
            except Exception as ex:
                st.error(f"No se pudo armar el PDF: {ex}")
    if ss.get(k_pdf):
        from navegacion import descargar
        descargar(b2, "Descargar PDF", ss[k_pdf], file_name=f"Renta_{rid}_{r['cliente'][:30]}.pdf",
                  mime="application/pdf", key=f"rt_dl_{rid}", use_container_width=True)

    # Acciones según el estado
    if r["estado"] == COTIZACION:
        st.divider()
        a1, a2, a3 = st.columns(3)
        if a1.button("Editar", key=f"rt_edit_{rid}", use_container_width=True, disabled=bool(firma),
                     help="Ya está firmada: para cambiarla, primero borra la firma (en «Firma del cliente»)."
                     if firma else None):
            _form_reset((r, its))
            ss["rt_ir"] = "Nueva cotización de renta"
            st.rerun()
        conf = a2.toggle("Confirmar renta", key=f"rt_conf_on_{rid}")
        canc = a3.toggle("Cancelar renta", key=f"rt_canc_on_{rid}")
        if conf:
            with st.container(border=True):
                st.markdown("**Confirmar renta**")
                prob = choques(sb, its, r["retiro"], r["devolucion"], sin_id=rid)
                for p_ in prob:
                    st.error(p_)
                if not firma:
                    st.warning("El cliente todavía no firmó la cotización.")
                st.caption("Las piezas quedan «En renta» desde el retiro hasta que registres la devolución.")
                q_v, q_c = codigos.pedir_identidad(sb, f"rt_conf_{rid}")
                if st.button("Sí, confirmar", type="primary", key=f"rt_conf_ok_{rid}", disabled=bool(prob)) \
                        and (quien := codigos.confirmar(sb, q_v, q_c)):
                    cambiar_estado(sb, rid, CONFIRMADA, quien)
                    _ok(f"Renta {rid} confirmada: las piezas quedan en renta del {_corta(r['retiro'])} al "
                        f"{_corta(r['devolucion'])}.")
                    st.rerun()
        if canc:
            _cancelar(sb, r)
    elif r["estado"] == CONFIRMADA:
        st.divider()
        a1, a2 = st.columns(2)
        dev = a1.toggle("Registrar devolución", key=f"rt_dev_on_{rid}")
        canc = a2.toggle("Cancelar renta", key=f"rt_canc_on_{rid}")
        if dev:
            _devolucion(sb, r, its)
        if canc:
            _cancelar(sb, r)


def _cancelar(sb, r):
    """Cancelar (cotización o renta confirmada que no se va a hacer). Se dibuja a todo lo ancho para que los
    avisos (código incorrecto, etc.) se vean."""
    rid = r["id"]
    with st.container(border=True):
        st.markdown("**Cancelar renta**")
        if r["estado"] == CONFIRMADA and _fecha(r["retiro"]) <= _hoy():
            st.warning("Esta renta ya empezó (fecha de retiro pasada). Si las piezas ya se las llevaron y hay que "
                       "cobrar, usa «Registrar devolución». Cancela solo si al final no se la llevaron.")
        st.caption("La renta queda cancelada, sus piezas libres y el link para firmar deja de servir.")
        q_v, q_c = codigos.pedir_identidad(sb, f"rt_canc_{rid}")
        if st.button("Sí, cancelar", key=f"rt_canc_ok_{rid}") and (quien := codigos.confirmar(sb, q_v, q_c)):
            cambiar_estado(sb, rid, CANCELADA, quien)
            _caducar_enlaces(sb, rid)
            _ok(f"Renta {rid} cancelada.", "info")
            st.rerun()


def _seccion_enlace(sb, user, r, its):
    rid = r["id"]
    e = enlace_vigente(sb, rid)
    with st.expander("📲 Link para que el cliente lea y firme", expanded=bool(e)):
        st.caption(f"El cliente ve el PDF completo y recién al llegar al final puede firmar. Vale {DIAS_ENLACE} días. "
                   "Si editas la cotización, el link deja de servir y hay que crear otro.")
        if not e:
            if st.button("Crear link para firmar", key=f"rt_link_{rid}"):
                u = st.session_state.get("user_info") or user or {}
                with st.spinner("Preparando el contrato…"):
                    try:
                        crear_enlace(sb, r, its, u.get("nombre") or u.get("usuario") or "página")
                    except Exception as ex:
                        st.error(f"No se pudo crear el link: {ex}")
                        return
                st.rerun()
            return
        url = f"{_base_url()}/f/{e['token']}"
        st.code(url, language=None)
        nombre = str(r.get("cliente") or "").split("/")[0].strip().split(" ")[0]
        msg = (f"Hola{' ' + nombre if nombre else ''}, esta es la cotización de renta de Viriathus. Por favor léela "
               f"completa y fírmala aquí:\n{url}")
        st.markdown(f"[💬 Mandar por WhatsApp](https://wa.me/?text={urllib.parse.quote(msg)})")


def _devolucion(sb, r, its):
    rid, dias = r["id"], r["dias"]
    st.caption("Marca cómo regresó cada pieza y qué % se cobra. Las que no regresaron se cobran completas y quedan "
               "VENDIDAS. Al guardar se crea la nota de pedido con el pago (entra a Caudal) y las piezas se liberan.")
    decisiones, total = {}, 0.0
    for it in its:
        cant = int(it["cantidad"])
        c = st.columns([2.4, 1.6, 1, 1, 1.4])
        c[0].markdown(f"**{it['modelo']}** · {it.get('nombre') or ''}  \n{cant} u. · pactado {_num(it['pct']):g}%")
        reg = c[1].selectbox("Regresó", list(REGRESO), format_func=REGRESO.get, key=f"rtd_reg_{rid}_{it['id']}",
                             label_visibility="collapsed")
        faltan = 0
        if cant > 1:
            faltan = int(c[2].number_input("No regresaron", 0, cant, cant if reg == "no" else 0,
                                           key=f"rtd_no_{rid}_{it['id']}_{reg}"))
        pct = c[3].number_input("% a cobrar", 0.0, 1000.0, float(_num(it["pct"])), step=1.0,
                                key=f"rtd_pct_{rid}_{it['id']}")
        cobro_r, f_, cobro_f = cobro_item(it, dias, reg, faltan, pct)
        c[4].markdown(f"{dinero(cobro_r)}" + (f"  \n+ {f_} u. completa(s) {dinero(cobro_f)}" if f_ else ""))
        decisiones[it["id"]] = {"regreso": reg, "no_regresaron": faltan, "pct": pct}
        total += cobro_r + cobro_f
    st.markdown(f"### Total a cobrar: {dinero(total)}")
    _, t = calcular(its, dias, r.get("garantia_pct"))
    st.caption(f"Garantía que se le devuelve al cliente: {dinero(t['garantia'])} (no pasa por Caudal).")
    from ventas import catalogo
    p1, p2, p3 = st.columns(3)
    forma = p1.selectbox("Forma de pago", catalogo(sb, "forma_pago"), index=None, key=f"rtd_forma_{rid}",
                         placeholder="Elegir…")
    banco = p2.selectbox("Banco / caja", catalogo(sb, "banco_caja"), index=None, key=f"rtd_banco_{rid}",
                         placeholder="Elegir…")
    fecha = p3.date_input("Fecha de pago", value=_hoy(), format="DD/MM/YYYY", key=f"rtd_fecha_{rid}")
    q_v, q_c = codigos.pedir_identidad(sb, f"rtd_id_{rid}")
    if st.button("Guardar devolución", type="primary", key=f"rtd_ok_{rid}", use_container_width=True):
        if total > 0 and not forma:
            st.error("Elige la forma de pago.")
            return
        quien = codigos.confirmar(sb, q_v, q_c)
        if not quien:
            return
        with st.spinner("Creando la nota de pedido…"):
            try:
                numero, avisos = registrar_devolucion(
                    sb, r, its, decisiones, {"forma_pago": forma, "banco_caja": banco, "fecha_pago": fecha.isoformat()},
                    quien)
            except Exception as ex:
                st.error(f"No se pudo registrar la devolución: {ex}")
                return
        for a in avisos:
            _ok(a, "warning")
        _ok(f"Devolución registrada: nota de pedido {numero} por {dinero(total)}. Las piezas quedaron libres"
            + (" (las que no regresaron, vendidas)." if any(d["regreso"] == "no" or d["no_regresaron"] for d in decisiones.values()) else "."))
        st.rerun()


def render_rentas(sb, user):
    from navegacion import selector
    ss = st.session_state
    ir = ss.pop("rt_ir", None)
    if ir:                                    # ir a otra pestaña ANTES de dibujar el selector
        ss["nav_rentas"] = ss["nav_rentas__ultimo"] = ir
    sec = selector(["Nueva cotización de renta", "Rentas"], "nav_rentas")
    if sec == "Rentas":
        tab_rentas(sb, user)
    else:
        tab_nueva(sb, user)
