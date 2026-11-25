"""
Remisiones.

Dos tipos, en una sola lista:

  * DE PEDIDO: se genera desde el detalle del pedido («Generar remisión»). Toma cliente y productos del pedido;
    cada objeto tiene foto, nombre, descripción, observaciones, estado (Pendiente, En restauro, Listo para
    entregar, Enviado, Entregado, Devuelto) y fecha de entrega. «Enviado» = ya salió pero el cliente aún no lo
    recibe: no cuenta como entregado. Cuando todo está entregado, el pedido pasa solo a
    «Pagado y entregado» (o «Entregado pendiente de pago» si tiene saldo). Si cambian los productos del pedido,
    la remisión agrega los nuevos y avisa de los que se quitaron. El PDF no lleva precios.

  * ABIERTA = CUENTA CORRIENTE: un cliente (p. ej. un decorador) se lleva, aparta o devuelve piezas de a poco
    y paga cuando puede. Un solo saldo:  SALDO = lo que se llevó − lo que pagó  (> 0 debe, < 0 saldo a favor).
      - Se lo llevó → VENDIDA en el inventario («cuenta corriente de CLIENTE») y agotada en Shopify.
      - Apartado    → RESERVADA (sin vencer); no suma al saldo hasta que se la lleva.
      - Devuelto    → vuelve a DISPONIBLE y deja de sumar: su valor queda a favor del cliente
                      (nunca se devuelve dinero: se lleva otra cosa o se descuenta de lo que debe).
      - Cada pago entra a Caudal en el momento (caudal.py lee remision_pagos); lo que sobra queda a favor.
      - Lo pagado se reparte entre las piezas de la más vieja a la más nueva, solo para mostrar cuáles
        están pagadas. No genera pedidos ni comisiones (eso se arregla aparte con los vendedores).
      - Precio por pieza con la misma regla de autorización (precios.py). El PDF es el estado de cuenta.

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

ESTADOS_PEDIDO = ("Pendiente", "En restauro", "Listo para entregar", "Enviado", "Entregado", "Devuelto")
ENVIADO, ENTREGADO, DEVUELTO = "Enviado", "Entregado", "Devuelto"   # «Enviado» todavía no cuenta como entregado
APARTADO = "Apartado"
SE_LLEVO = "Se lo llevó"
LLEVO_PEND = "Se lo llevó · pendiente de pago"       # estados viejos: cuentan como «Se lo llevó»
LLEVO_PAG = "Se lo llevó · pagado"
ESTADOS_ABIERTA = (SE_LLEVO, APARTADO, DEVUELTO)


def estado_abierta(e):
    """Estado de una pieza de cuenta corriente (los viejos «Se lo llevó · …» son «Se lo llevó»)."""
    return SE_LLEVO if e in (LLEVO_PEND, LLEVO_PAG) else e

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
    rem = dict(rem[0], _direccion=direccion_de_pedido(sb, rem[0]))
    return rem, items, pagos


def direccion_de_pedido(sb, rem):
    """Domicilio de entrega del pedido (remisión de pedido) para el PDF, en renglones. [] si no tiene."""
    if rem.get("tipo") != "pedido" or not rem.get("numero_pedido"):
        return []
    try:
        p = (sb.table("pedidos").select("domicilio").eq("numero_pedido", int(rem["numero_pedido"]))
             .limit(1).execute().data or [{}])[0]
    except Exception:
        return []
    return [" ".join(x.split()) for x in str(p.get("domicilio") or "").splitlines() if x.strip()]


def remision_de_pedido(sb, numero):
    r = (sb.table("remisiones").select("id").eq("tipo", "pedido").eq("numero_pedido", int(numero))
         .limit(1).execute().data)
    return r[0]["id"] if r else None


def saldo_favor(pagos):
    """(Compatibilidad) Pagos que no se usaron en pedidos viejos."""
    return round(sum(_num(p.get("monto")) - _num(p.get("aplicado")) for p in pagos), 2)


def _orden_cargo(it):
    return (str(it.get("fecha_entrega") or ""), int(_num(it.get("orden"))), int(_num(it.get("id"))))


def resumen_abierta(items, pagos):
    """Saldo de la cuenta corriente.
    debe > 0: el cliente debe · debe < 0: saldo a favor. `cobertura` = {id: (pagado, total)} de cada pieza que se
    llevó, repartiendo lo pagado de la más vieja a la más nueva (solo para mostrar)."""
    r = {"n_llevo": 0, "llevo": 0.0, "n_apartado": 0, "apartado": 0.0, "n_devuelto": 0, "devuelto": 0.0,
         "pagado": round(sum(_num(p.get("monto")) for p in pagos), 2), "cobertura": {}}
    llevadas = []
    for it in items:
        e, t = estado_abierta(it.get("estado")), _linea(it)
        if e == SE_LLEVO:
            r["n_llevo"] += 1
            r["llevo"] += t
            llevadas.append((it, t))
        elif e == APARTADO:
            r["n_apartado"] += 1
            r["apartado"] += t
        elif e == DEVUELTO:
            r["n_devuelto"] += 1
            r["devuelto"] += t
    r["llevo"] = round(r["llevo"], 2)
    r["debe"] = round(r["llevo"] - r["pagado"], 2)
    r["a_favor"] = max(-r["debe"], 0.0)
    resto = r["pagado"]
    for it, t in sorted(llevadas, key=lambda x: _orden_cargo(x[0])):
        toma = round(min(max(resto, 0.0), t), 2)
        r["cobertura"][it["id"]] = (toma, round(t, 2))
        resto = round(resto - toma, 2)
    return r


def texto_cobertura(pagado, total):
    if total <= 0.005 or pagado >= total - 0.005:
        return "✅ pagada"
    if pagado > 0.005:
        return f"🟡 pagada en parte (${pagado:,.2f} de ${total:,.2f})"
    return "⏳ por pagar"


def reservado_en_remision(sb, modelo):
    """Cliente de la remisión abierta que tiene reservado el modelo ("" si ninguna)."""
    try:
        its = (sb.table("remision_items").select("remision_id,estado,numero_pedido")
               .eq("modelo", str(modelo)).execute().data or [])
        ids = list({i["remision_id"] for i in its if i.get("estado") == APARTADO and not i.get("numero_pedido")})
        if not ids:
            return ""
        rems = (sb.table("remisiones").select("cliente").in_("id", ids).eq("tipo", "abierta")
                .eq("estado", "Abierta").limit(1).execute().data or [])
        return str(rems[0].get("cliente") or "").strip() if rems else ""
    except Exception:
        return ""


def vendidos_en_cuenta(sb, modelos):
    """{modelo: "$precio (cuenta corriente de CLIENTE)"} de las piezas que «Se lo llevó» un cliente de cuenta corriente."""
    out = {}
    modelos = [str(m).strip() for m in modelos if str(m or "").strip()]
    if not modelos:
        return out
    try:
        its = (sb.table("remision_items").select("remision_id,modelo,estado,precio_unitario,cantidad,descuento_pct")
               .in_("modelo", modelos).in_("estado", [SE_LLEVO, LLEVO_PEND, LLEVO_PAG]).execute().data or [])
        ids = sorted({i["remision_id"] for i in its})
        clientes = {}
        if ids:
            clientes = {r["id"]: r.get("cliente") for r in (sb.table("remisiones").select("id,cliente,tipo")
                                                            .in_("id", ids).eq("tipo", "abierta").execute().data or [])}
        for i in sorted(its, key=lambda x: x["remision_id"]):
            if i["remision_id"] in clientes:
                out[str(i["modelo"]).strip()] = (f"${_linea(i):,.2f} (cuenta corriente de "
                                                  f"{clientes[i['remision_id']] or 'un cliente'}, R-{i['remision_id']})")
    except Exception:
        pass
    return out


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

    # 2) Cuenta corriente: cada cambio de estado mueve el inventario.
    #    Primero lo que puede fallar (lo DEVUELTO que se vuelve a apartar o a llevar): si algo no se puede,
    #    se deshace lo hecho y no se guarda nada.
    nuevas_desc, hoy = {}, _hoy().isoformat()
    if abierta:
        movs = []
        for iid, f in cambios.items():
            it = por_id[iid]
            viejo, nuevo = estado_abierta(it.get("estado")), f.get("estado") or estado_abierta(it.get("estado"))
            if it.get("numero_pedido") or viejo == nuevo or not it.get("modelo"):
                continue
            if viejo == SE_LLEVO and nuevo == APARTADO:
                raise ValueError(f"El modelo {it['modelo']} ya se lo llevó: para cambiarlo, márcalo «Devuelto».")
            movs.append((iid, it, viejo, nuevo))
        hechos = []
        try:
            for iid, it, viejo, nuevo in [m for m in movs if m[2] == DEVUELTO]:
                cant = int(_num(it.get("cantidad"), 1))
                ok, msg, aviso, desc = (reservas.reservar if nuevo == APARTADO else vender)(sb, it["modelo"], cant)
                if not ok:
                    from ventas import texto_reservado
                    raise ValueError(f"No se puede cambiar el modelo {it['modelo']}: "
                                     f"{texto_reservado(sb, it['modelo']) if 'reservado' in msg else msg}")
                hechos.append((it["modelo"], desc))
                nuevas_desc[iid] = desc
                if aviso:
                    avisos.append(aviso)
        except ValueError:
            for m, d in hechos:
                devolver(sb, m, d)
            raise
        for iid, it, viejo, nuevo in [m for m in movs if m[2] != DEVUELTO]:
            if viejo == APARTADO and nuevo == SE_LLEVO:              # apartado → vendido
                a = reservas.consumir(sb, it["modelo"])
            else:                                                     # → devuelto: vuelve a disponible
                a = devolver(sb, it["modelo"], int(_num(it.get("reservado"))))
                nuevas_desc[iid] = 0
            if a:
                avisos.append(a)

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
        if abierta and f.get("estado") and estado_abierta(it.get("estado")) != f["estado"]:
            datos["fecha_entrega"] = hoy                          # en la cuenta corriente: fecha del último movimiento
        if iid in nuevas_desc:
            datos["reservado"] = int(nuevas_desc[iid])
        sb.table("remision_items").update(datos).eq("id", iid).execute()
    return avisos, cambios_precio


# ----------------------------------------------------------------------
# Remisión abierta
# ----------------------------------------------------------------------
def vender(sb, modelo, unidades=1):
    """La pieza se la llevó un cliente de cuenta corriente: queda VENDIDA (y agotada en Shopify).
    Con varias unidades descuenta las que se llevó; si llegan a 0 queda vendida.
    Devuelve (ok, mensaje, aviso, descontadas) como reservas.reservar."""
    d = reservas._leer(sb, modelo)
    if d is None:
        return False, f"El modelo {modelo} no existe en el inventario.", None, 0
    est = reservas.estado_disp(d)
    if est == "v":
        return False, f"El modelo {modelo} ya está vendido.", None, 0
    if est == "r":
        return False, f"El modelo {modelo} ya está reservado.", None, 0
    stock = reservas._num(d.get("CANTIDAD"), 1.0)
    try:
        import rentas
        en_renta = rentas.bloqueo_venta(sb, modelo, stock, unidades)
    except Exception:
        en_renta = None
    if en_renta:
        return False, en_renta, None, 0
    if stock <= 0:
        stock = 1.0
    if unidades > stock:
        return False, f"El modelo {modelo} solo tiene {stock:g} unidad(es) disponible(s).", None, 0
    descontadas, vendido = 0, True
    if stock > 1:                                   # varias unidades: se descuentan
        descontadas = int(unidades)
        resta = stock - descontadas
        d["CANTIDAD"] = int(resta) if float(resta).is_integer() else resta
        vendido = resta <= 0
    else:
        d["CANTIDAD"] = 0                           # vendido = 0 unidades
    if vendido:
        d["DISP"] = d["DISPONIBILIDAD"] = "v"
    reservas._guardar(sb, modelo, d)
    return True, "ok", (reservas._shopify(modelo, True) if vendido else None), descontadas


def devolver(sb, modelo, descontadas=0):
    """La pieza vuelve a la tienda (estaba apartada o se la había llevado): DISPONIBLE otra vez.
    Devuelve las unidades descontadas. Devuelve un aviso o None."""
    d = reservas._leer(sb, modelo)
    if d is None:
        return f"Modelo {modelo}: ya no existe en el inventario."
    if descontadas:
        nuevo = reservas._num(d.get("CANTIDAD"), 0.0) + descontadas
        d["CANTIDAD"] = int(nuevo) if float(nuevo).is_integer() else nuevo
    estaba = reservas.estado_disp(d)
    if estaba in ("v", "r"):
        d["DISP"] = d["DISPONIBILIDAD"] = ""
        if reservas._num(d.get("CANTIDAD"), 0.0) <= 0:
            d["CANTIDAD"] = 1                       # disponible = al menos 1
    if descontadas or estaba in ("v", "r"):
        reservas._guardar(sb, modelo, d)
    return reservas._shopify(modelo, False) if estaba in ("v", "r") else None


def crear_abierta(sb, cliente, vendedor, fecha, usuario):
    rem = sb.table("remisiones").insert({
        "tipo": "abierta", "estado": "Abierta", "cliente": cliente.strip(), "vendedor": vendedor or None,
        "fecha": (fecha or _hoy()).isoformat(), "created_by": usuario}).execute().data[0]
    codigos.registrar(sb, usuario, "Abrió cuenta corriente", "remision", f"R-{rem['id']}", cliente.strip())
    return rem["id"]


def agregar_pieza(sb, rem, items, modelo, usuario, estado=SE_LLEVO):
    """Agrega un modelo a la cuenta: «Se lo llevó» (queda VENDIDO) o «Apartado» (queda RESERVADO).
    Devuelve avisos. Lanza ValueError."""
    from ventas import texto_reservado
    modelo = str(modelo).strip()
    if modelo.endswith(".0"):
        modelo = modelo[:-2]
    if estado not in (SE_LLEVO, APARTADO):
        raise ValueError("Elige si se lo llevó o queda apartado.")
    if any(str(i.get("modelo")) == modelo and estado_abierta(i.get("estado")) != DEVUELTO for i in items):
        raise ValueError(f"El modelo {modelo} ya está en esta cuenta.")
    info = _info(sb, modelo)
    if info is None:
        raise ValueError(f"El modelo {modelo} no existe en el inventario.")
    if info["disp"] == "v":
        raise ValueError(f"El modelo {modelo} figura como VENDIDO.")
    if info["disp"] == "r":
        raise ValueError(texto_reservado(sb, modelo) + ".")
    ok, msg, aviso, desc = (vender if estado == SE_LLEVO else reservas.reservar)(sb, modelo, 1)
    if not ok:
        raise ValueError(msg)
    try:
        sb.table("remision_items").insert({
            "remision_id": rem["id"], "orden": len(items), "modelo": info["modelo"], "es_sn": False,
            "nombre": info["nombre"], "descripcion": info["descripcion"], "foto_url": info["foto_url"],
            "estado": estado, "fecha_entrega": _hoy().isoformat(), "cantidad": 1,
            "precio_unitario": info["precio"], "descuento_pct": 0,
            "costo_unitario": info["costo"], "utilidad_bruta_inv": info["utilidad_bruta"],
            "reservado": int(desc)}).execute()
    except Exception:
        devolver(sb, modelo, desc)
        raise
    codigos.registrar(sb, usuario, "Se lo llevó (cuenta corriente)" if estado == SE_LLEVO else
                      "Apartó (cuenta corriente)", "remision", f"R-{rem['id']}", f"{modelo} · ${info['precio']:,.2f}")
    avisos = [aviso] if aviso else []
    if info["precio"] <= 0:
        avisos.append(f"El modelo {modelo} no tiene precio en el inventario: ponérselo necesita autorización.")
    return avisos


def quitar_pieza(sb, rem, it, usuario):
    """Borra el renglón (para errores). Si la pieza seguía en la cuenta, vuelve a DISPONIBLE."""
    if it.get("numero_pedido"):
        raise ValueError("Esa pieza ya pasó a un pedido: no se puede quitar.")
    aviso = None
    if estado_abierta(it.get("estado")) != DEVUELTO and it.get("modelo"):
        aviso = devolver(sb, it["modelo"], int(_num(it.get("reservado"))))
    sb.table("remision_items").delete().eq("id", it["id"]).execute()
    codigos.registrar(sb, usuario, "Quitó pieza de cuenta corriente", "remision", f"R-{rem['id']}",
                      it.get("modelo") or "")
    return aviso


def registrar_pago(sb, rem, monto, forma, banco, fecha, usuario):
    """El pago entra a Caudal en el momento (caudal.py lee remision_pagos)."""
    if _num(monto) <= 0:
        raise ValueError("El monto debe ser mayor que 0.")
    if not forma:
        raise ValueError("Elige la forma de pago.")
    sb.table("remision_pagos").insert({
        "remision_id": rem["id"], "monto": round(_num(monto), 2), "forma_pago": forma, "banco_caja": banco,
        "fecha": (fecha or _hoy()).isoformat(), "usuario": usuario}).execute()
    codigos.registrar(sb, usuario, "Pago en cuenta corriente", "remision", f"R-{rem['id']}",
                      f"{rem.get('cliente') or ''} · ${_num(monto):,.2f} {forma}")


def borrar_pago(sb, rem, pago, usuario):
    """Para un pago cargado por error: se borra y deja de contar en Caudal."""
    sb.table("remision_pagos").delete().eq("id", pago["id"]).execute()
    codigos.registrar(sb, usuario, "Borró pago de cuenta corriente", "remision", f"R-{rem['id']}",
                      f"${_num(pago.get('monto')):,.2f} {pago.get('forma_pago') or ''} del {pago.get('fecha') or ''}")


def borrar(sb, rem, items, pagos, usuario):
    """Borra la remisión. En una cuenta corriente, las piezas que seguían en la cuenta (apartadas o que se llevó)
    vuelven a DISPONIBLE y los pagos se borran (salen de Caudal). Devuelve avisos."""
    avisos = []
    if rem.get("tipo") == "abierta":
        for it in items:
            if it.get("modelo") and not it.get("numero_pedido") and estado_abierta(it.get("estado")) != DEVUELTO:
                a = devolver(sb, it["modelo"], int(_num(it.get("reservado"))))
                if a:
                    avisos.append(a)
    sb.table("remision_items").delete().eq("remision_id", rem["id"]).execute()
    sb.table("remision_pagos").delete().eq("remision_id", rem["id"]).execute()
    sb.table("remisiones").delete().eq("id", rem["id"]).execute()
    codigos.registrar(sb, usuario, "Borró remisión", "remision", folio(rem),
                      f"{rem.get('cliente') or ''} · {len(items)} renglón(es)"
                      + (f" · pagos ${sum(_num(p.get('monto')) for p in pagos):,.2f}" if pagos else ""))
    return avisos


def cerrar(sb, rem, items, pagos, usuario):
    r = resumen_abierta(items, pagos)
    if r["n_apartado"]:
        raise ValueError("Todavía hay piezas apartadas: márcalas «Se lo llevó» o «Devuelto» antes de cerrar.")
    if abs(r["debe"]) > 0.005:
        raise ValueError("La cuenta no está en cero: " + (f"debe ${r['debe']:,.2f}." if r["debe"] > 0 else
                         f"tiene ${-r['debe']:,.2f} a favor (que se lleve algo por ese valor antes de cerrar)."))
    sb.table("remisiones").update({"estado": "Cerrada"}).eq("id", rem["id"]).execute()
    codigos.registrar(sb, usuario, "Cerró cuenta corriente", "remision", f"R-{rem['id']}", rem.get("cliente") or "")


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------
def generar_pdf(rem, items, pagos=(), firma=None):
    """firma = firmas.para_pdf(...) (opcional): se estampa sobre «Recibió» en cada hoja."""
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
    filas_izq = [[campo("Cliente:", rem.get("cliente"), 52, 270)]]
    for n, linea in enumerate(rem.get("_direccion") or []):      # domicilio de entrega del pedido (si tiene)
        filas_izq.append([campo("Dirección:" if n == 0 else "", linea, 52, 270)])
    filas_izq.append([campo("Fecha:", f.strftime("%d/%m/%Y") if f else "", 52, 120)])
    if str(rem.get("vendedor") or "").strip():                # quién atendió (varios: «ANA / LUIS»)
        filas_izq.append([campo("Vendedor:", " ".join(str(rem["vendedor"]).split()), 52, 270)])
    izq = Table(filas_izq, colWidths=[322])
    der = Table([[Paragraph("ESTADO DE CUENTA" if abierta else "REMISIÓN", ST_TIT)],
                 [Paragraph(escape(folio(rem)), ST_TIT)]], colWidths=[ANCHO - 340])
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
    res = resumen_abierta(items, pagos) if abierta else None
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
        estado = escape(estado_abierta(it.get("estado")) if abierta else (it.get("estado") or ""))
        fe = _fecha(it.get("fecha_entrega"))
        if fe:
            estado += f"<br/><font size='8'>{fe.strftime('%d/%m/%Y')}</font>"
        if abierta and it["id"] in res["cobertura"]:
            pg, tt = res["cobertura"][it["id"]]
            cob = texto_cobertura(pg, tt).split(" ", 1)[1]           # sin el emoji (la fuente del PDF no lo tiene)
            estado += f"<br/><font size='8'>{escape(cob)}</font>"
        if abierta and it.get("numero_pedido"):
            estado += f"<br/><font size='8'>Pedido {it['numero_pedido']}</font>"
        fila = [Paragraph(str(i + 1), ST_NUM), celda_foto, texto, Paragraph(estado, ST_EST)]
        if abierta:
            pre = dinero(_linea(it))
            if estado_abierta(it.get("estado")) == DEVUELTO:
                pre = f"<strike>{pre}</strike>"
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
        r = res
        if pagos:                                          # los pagos, uno por uno
            fp = [[Paragraph("PAGOS", ST_TH), Paragraph("FORMA", ST_TH), Paragraph("MONTO", ST_TH)]]
            for p in sorted(pagos, key=lambda x: (str(x.get("fecha") or ""), x.get("id") or 0)):
                fpg = _fecha(p.get("fecha"))
                fp.append([Paragraph(fpg.strftime("%d/%m/%Y") if fpg else "", ST_TOT),
                           Paragraph(escape(" · ".join(x for x in (p.get("forma_pago"), p.get("banco_caja")) if x)),
                                     ST_TOT), Paragraph(dinero(p.get("monto")), ST_TOT)])
            tp = Table(fp, colWidths=[90, 170, 90])
            tp.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NEGRO), ("GRID", (0, 0), (-1, -1), 0.5, NEGRO),
                                    ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
            tp.hAlign = "RIGHT"
            final += [Spacer(1, 10), tp]
        lineas = [(f"Se llevó · {r['n_llevo']} pieza(s)", dinero(r["llevo"]), False),
                  ("Pagos recibidos", "− " + dinero(r["pagado"]), False)]
        if r["n_apartado"]:
            lineas.insert(1, (f"Apartado (no suma) · {r['n_apartado']}", dinero(r["apartado"]), False))
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
                              f"{'Estado de cuenta' if abierta else 'Remisión'} {folio(rem)} — {rem.get('cliente') or ''} — hoja {doc.page}")
        W = 220
        izq_x, der_x = MARGEN, letter[0] - MARGEN - W
        y_tit, y_linea, y_pie = 92, 50, 36
        firmado = firma and firma.get("png")
        recibio = f"{firma.get('nombre')} · {firma.get('fecha')}" if firmado else "Nombre y firma"
        for x, titulo, debajo in ((izq_x, "Atentamente", "VIRIATHUS"), (der_x, "Recibió", recibio)):
            c.setFont("Times-Bold", 11)
            c.drawCentredString(x + W / 2, y_tit, titulo)
            c.setLineWidth(0.6)
            c.line(x, y_linea, x + W, y_linea)
            c.setFont("Times-Roman", 10)
            c.drawCentredString(x + W / 2, y_pie, debajo)
        if firmado:                                         # la firma del cliente, encima de su línea
            try:
                w, h = ImageReader(io.BytesIO(firma["png"])).getSize()
                esc = min((W - 20) / w, 34 / h)
                c.drawImage(ImageReader(io.BytesIO(firma["png"])), der_x + (W - w * esc) / 2, y_linea + 2,
                            width=w * esc, height=h * esc, mask="auto")
            except Exception:
                pass
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
        for f in (sb.table("remision_items").select("id,remision_id,orden,fecha_entrega,estado,numero_pedido,precio_unitario,cantidad,descuento_pct")
                  .in_("remision_id", ids[i:i + 200]).execute().data or []):
            out.setdefault(f["remision_id"], []).append(f)
    return out


def _lista(sb, user, tipo="pedido"):
    """tipo «pedido» (sección Remisiones) o «abierta» (sección Cuenta corriente)."""
    ss = st.session_state
    cc = tipo == "abierta"
    p = "cc" if cc else "rm"
    f2, f3 = st.columns([2, 3])
    ver = f2.selectbox("Mostrar", ["Abiertas", "Todas"] if cc else ["Con pendientes", "Todas"], key=f"{p}_f_ver")
    busq = f3.text_input("Buscar cliente" if cc else "Buscar cliente o N° de pedido", key=f"{p}_f_busq").strip()
    q = sb.table("remisiones").select("*").eq("tipo", tipo).order("id", desc=True).limit(300)
    if busq:
        q = q.eq("numero_pedido", int(busq)) if busq.isdigit() else q.ilike("cliente", f"%{busq.replace('%', '')}%")
    try:
        rems = q.execute().data or []
    except Exception as ex:
        st.error(f"No se pudieron leer las remisiones. ¿Ya corriste en Supabase el SQL de remisiones? ({ex})")
        return
    its = _items_de_varias(sb, [r["id"] for r in rems])
    pagos_de = {}
    ab = [r["id"] for r in rems if r["tipo"] == "abierta"]
    for i in range(0, len(ab), 200):
        for p in (sb.table("remision_pagos").select("remision_id,monto").in_("remision_id", ab[i:i + 200])
                  .execute().data or []):
            pagos_de.setdefault(p["remision_id"], []).append(p)
    filas = []
    for r in rems:
        it = its.get(r["id"], [])
        if r["tipo"] == "pedido":
            ent = sum(1 for i in it if i.get("estado") in (ENTREGADO, DEVUELTO))
            pendiente = ent < len(it) or not it
            env = sum(1 for i in it if i.get("estado") == ENVIADO)
            avance = f"{ent} de {len(it)} entregados" + (f" · {env} enviado(s)" if env else "")
        else:
            res = resumen_abierta(it, pagos_de.get(r["id"], []))
            pendiente = r.get("estado") == "Abierta"
            saldo = (f"debe ${res['debe']:,.2f}" if res["debe"] > 0.005 else
                     f"a favor ${-res['debe']:,.2f}" if res["debe"] < -0.005 else "en cero")
            avance = (f"{res['n_llevo']} se llevó · {res['n_apartado']} apartadas · {saldo}"
                      + ("" if pendiente else " · CERRADA"))
        if ver in ("Con pendientes", "Abiertas") and not pendiente:
            continue
        filas.append({"id": r["id"], "_id": r["id"], "FOLIO": folio(r),
                      "TIPO": "De pedido" if r["tipo"] == "pedido" else "Cuenta corriente",
                      "CLIENTE": r.get("cliente") or "", "FECHA": r.get("fecha") or "", "AVANCE": avance})
    if not filas:
        st.info(("No hay cuentas corrientes para mostrar. Ábrela en «Nueva cuenta corriente»." if cc else
                 "No hay remisiones para mostrar." + (" Las de pedido se generan desde el detalle del pedido."
                                                      if not rems else "")))
        return
    import lista_acciones as la
    columnas = [("FOLIO", 1.0, lambda f: f"**{f['FOLIO']}**"), ("CLIENTE", 2.4, lambda f: f["CLIENTE"]),
                ("DESDE" if cc else "FECHA", 1.0,
                 lambda f: (_fecha(f["FECHA"]).strftime("%d/%m/%Y") if _fecha(f["FECHA"]) else "")),
                ("SALDO" if cc else "AVANCE", 3.2, lambda f: f["AVANCE"])]
    la.lista(filas, columnas, f"{p}_lista",
             armar_pdf=lambda f: (pdf_de_remision(sb, f["id"]),
                                  f"{'estado_de_cuenta' if cc else 'remision'}_{f['FOLIO'].replace(' ', '_')}.pdf"),
             al_ver=(lambda f: st.session_state.__setitem__(f"{p}_sel", f["id"])),
             ayuda_pdf="Descargar el estado de cuenta" if cc else "Descargar el PDF de la remisión",
             ayuda_ver="Abrir la cuenta" if cc else "Abrir la remisión")


def pdf_de_remision(sb, rid):
    """PDF de la remisión ya guardada (con la firma de quien recibe, si tiene)."""
    rem, items, pagos = leer(sb, rid)
    if rem is None:
        raise ValueError("Esa remisión ya no existe.")
    fz = None
    try:
        import firmas
        fz = firmas.para_pdf(firmas.leer(sb, "remision", rid))
    except Exception:
        pass
    return generar_pdf(rem, items, pagos, fz)


def _nueva_abierta(sb, user):
    from ventas import catalogo
    st.caption("Para un cliente que se lleva, aparta o devuelve piezas de a poco y paga cuando puede. Lo que se lleva "
               "queda VENDIDO a su nombre; lo que devuelve vuelve a DISPONIBLE y queda a su favor. Cada pago entra a "
               "Caudal en el momento; lo que sobra queda como saldo a favor.")
    c1, c2, c3 = st.columns([3, 2, 1])
    cliente = c1.text_input("CLIENTE *", key="rm_n_cliente")
    vendedor = c2.selectbox("VENDEDOR *", catalogo(sb, "vendedor"), index=None, key="rm_n_vend", placeholder="Elegir…")
    fecha = c3.date_input("FECHA", value=_hoy(), key="rm_n_fecha", format="DD/MM/YYYY")
    q_v, q_c = codigos.pedir_identidad(sb, "rm_n_id")
    if st.button("Abrir cuenta corriente", type="primary", key="rm_n_crear"):
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
        st.session_state["cc_sel"] = rid
        st.session_state["cc_vista"] = "Cuentas"
        st.rerun()


def _editor_items(sb, rem, items, k, cobertura=None):
    """Dibuja los renglones. Devuelve (cambios, fotos, quitar_id).
    `cobertura` (cuenta corriente) = {id: (pagado, total)} para mostrar si cada pieza que se llevó está pagada."""
    cobertura = cobertura or {}
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
            if abierta and iid in cobertura:
                titulo += " · " + texto_cobertura(*cobertura[iid])
            if abierta and _fecha(it.get("fecha_entrega")):
                titulo += f" · {_fecha(it['fecha_entrega']):%d/%m/%Y}"
            t1.markdown(titulo)
            if abierta and not bloqueado and t2.button("Quitar", key=k(f"q_{iid}")):
                quitar = iid
            a, b, c = st.columns([1, 4, 2])
            sub = b.file_uploader("Foto" + ("" if it.get("foto_url") else " — ⚠️ no tiene foto, sube una"),
                                  type=["jpg", "jpeg", "png", "webp"], key=k(f"f_{iid}_{vf}"),
                                  help="Solo para la remisión; no cambia la foto del inventario.")
            if sub is not None:
                fotos[iid] = sub.getvalue()
                from cotizaciones import miniatura
                mini = miniatura(fotos[iid])
                if mini:
                    a.image(mini, width=90)
            elif it.get("foto_url"):
                a.image(it["foto_url"], width=90)
            else:
                a.warning("Sin foto")
            nombre = b.text_input("Nombre", value=it.get("nombre") or "", key=k(f"n_{iid}"))
            descripcion = b.text_area("Descripción", value=it.get("descripcion") or "", key=k(f"d_{iid}"), height=80)
            obs = b.text_input("Observaciones", value=it.get("observaciones") or "", key=k(f"o_{iid}"),
                               placeholder="Opcional")
            opciones = ESTADOS_ABIERTA if abierta else ESTADOS_PEDIDO
            est_it = estado_abierta(it.get("estado")) if abierta else it.get("estado")
            actual = est_it if est_it in opciones else opciones[0]
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


def _detalle(sb, user, rid, sel="rm_sel"):
    ss = st.session_state
    if st.button("← Volver a la lista", key=f"{sel}_volver"):
        ss.pop(sel, None)
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
        ss.pop(sel, None)
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

    st.markdown(f"### {'Cuenta corriente' if abierta else 'Remisión'} {folio(rem)} — {rem.get('cliente') or ''}")
    st.caption(f"{'Cuenta corriente' if abierta else 'Remisión del pedido'} · Desde: {rem.get('fecha') or '—'} · "
               f"Vendedor: {rem.get('vendedor') or '—'}" + (" · CERRADA" if rem.get("estado") == "Cerrada" else ""))
    if abierta:
        r = resumen_abierta(items, pagos)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Se llevó", f"${r['llevo']:,.2f}", f"{r['n_llevo']} pieza(s)", delta_color="off")
        m2.metric("Pagó", f"${r['pagado']:,.2f}", f"{len(pagos)} pago(s)", delta_color="off")
        if r["debe"] > 0.005:
            m3.metric("DEBE", f"${r['debe']:,.2f}")
        else:
            m3.metric("SALDO A FAVOR", f"${max(-r['debe'], 0):,.2f}")
        m4.metric("Apartado (no suma)", f"${r['apartado']:,.2f}", f"{r['n_apartado']} pieza(s)", delta_color="off")

    st.markdown("**Para guardar cambios, identifícate:**")
    q_v, q_c = codigos.pedir_identidad(sb, k("id"))

    if abierta and rem.get("estado") == "Abierta":
        with st.form(k("form_mod"), clear_on_submit=True):
            m1, m3, m2 = st.columns([3, 2, 1])
            modelo_txt = m1.text_input("Agregar pieza (MODELO)")
            como = m3.radio("¿Cómo?", [SE_LLEVO, APARTADO], horizontal=True,
                            help="Se lo llevó: queda VENDIDA y suma a lo que debe. Apartado: queda RESERVADA y no suma "
                                 "hasta que se la lleve.")
            m2.write("")
            m2.write("")
            agregar = m2.form_submit_button("Agregar", use_container_width=True)
        if agregar and modelo_txt.strip():
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    av = agregar_pieza(sb, rem, items, modelo_txt, quien, como)
                    ss["rm_msgs"] = [("ok", f"Modelo {modelo_txt.strip()}: "
                                      + ("se lo llevó (queda vendido)." if como == SE_LLEVO else "apartado."))] \
                        + [("w", a) for a in av]
                    st.rerun()
                except ValueError as ex:
                    st.error(str(ex))
                except Exception as ex:
                    st.error(f"No se pudo agregar: {ex}")

    if not items:
        st.info("Esta cuenta todavía no tiene piezas." if abierta else "El pedido no tiene productos.")
    cambios, fotos, quitar = _editor_items(sb, rem, items, k,
                                           resumen_abierta(items, pagos)["cobertura"] if abierta else None)
    if quitar:
        quien = codigos.confirmar(sb, q_v, q_c)
        if quien:
            it = next(i for i in items if i["id"] == quitar)
            try:
                a = quitar_pieza(sb, rem, it, quien)
                ss["rm_msgs"] = [("ok", f"Pieza {it.get('modelo')} quitada" + (" (vuelve a disponible)."
                                  if estado_abierta(it.get("estado")) != DEVUELTO else "."))] + ([("w", a)] if a else [])
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

    # ---- Firma del cliente (opcional) ----
    import firmas
    fz = firmas.para_pdf(firmas.widget(sb, "remision", rid, "✍️ Firma de quien recibe"))

    # ---- PDF ----
    st.divider()
    firma = hash(repr((sorted(rem.items()), [sorted(i.items()) for i in items], [sorted(p.items()) for p in pagos],
                       (fz or {}).get("id"))))
    g = ss.get("rm_pdf")
    if g and (g.get("rid") != rid or g.get("firma") != firma):
        ss.pop("rm_pdf", None)
        g = None
    p1, p2 = st.columns(2)
    if p1.button("📄 Preparar PDF", key=k("pdf"), disabled=bool(cambios),
                 help="Guarda los cambios antes de preparar el PDF." if cambios else None):
        with st.spinner("Armando el PDF (descarga las fotos)…"):
            try:
                ss["rm_pdf"] = {"rid": rid, "firma": firma, "pdf": generar_pdf(rem, items, pagos, fz)}
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
        _pagos(sb, rem, items, pagos, q_v, q_c, k)
    _borrar_ui(sb, rem, items, pagos, q_v, q_c, k, sel)


def _borrar_ui(sb, rem, items, pagos, q_v, q_c, k, sel="rm_sel"):
    ss = st.session_state
    st.divider()
    with st.expander("🗑 Borrar cuenta corriente" if rem.get("tipo") == "abierta" else "🗑 Borrar remisión"):
        abierta = rem.get("tipo") == "abierta"
        if abierta:
            libres = [i for i in items if i.get("modelo") and not i.get("numero_pedido")
                      and estado_abierta(i.get("estado")) != DEVUELTO]
            tot_p = sum(_num(p.get("monto")) for p in pagos)
            st.caption("Es para cuentas cargadas por error.")
            st.warning(f"Se borra la cuenta con todo: {len(libres)} pieza(s) que se llevó o apartó vuelven a DISPONIBLE "
                       f"(aunque las tenga el cliente) y {len(pagos)} pago(s) por ${tot_p:,.2f} SALEN DE CAUDAL.")
        else:
            st.caption("Se borra la remisión (fotos, estados y observaciones). El pedido no cambia; puedes volver a "
                       "generarla desde el pedido.")
        seguro = st.checkbox("Sí, quiero borrarla", key=k("borrar_ok"))
        if st.button("Borrar", key=k("borrar"), disabled=not seguro):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    av = borrar(sb, rem, items, pagos, quien)
                except Exception as ex:
                    st.error(f"No se pudo borrar: {ex}")
                    return
                ss.pop(sel, None)
                ss.pop("rm_pdf", None)
                ss[f"{sel[:2]}_lista_msgs"] = [("ok", f"{'Cuenta' if rem.get('tipo') == 'abierta' else 'Remisión'} "
                                                     f"{folio(rem)} borrada.")] + [("w", a) for a in av]
                st.rerun()


def _pagos(sb, rem, items, pagos, q_v, q_c, k):
    from ventas import catalogo
    ss = st.session_state
    r = resumen_abierta(items, pagos)
    st.divider()
    st.subheader("Pagos")
    st.caption("Cada pago entra a Caudal en el momento (como una venta). Lo que paga de más queda como saldo a favor "
               "para lo próximo que se lleve.")
    if pagos:
        import pandas as pd
        st.dataframe(pd.DataFrame([{"FECHA": p.get("fecha"), "MONTO": _num(p.get("monto")),
                                    "FORMA DE PAGO": p.get("forma_pago"), "BANCO / CAJA": p.get("banco_caja"),
                                    "REGISTRÓ": p.get("usuario")} for p in pagos]),
                     use_container_width=True, hide_index=True,
                     column_config={"MONTO": st.column_config.NumberColumn(format="$%.2f")})
    if r["debe"] > 0.005:
        st.markdown(f"**Debe ${r['debe']:,.2f}**")
    elif r["debe"] < -0.005:
        st.markdown(f"**Tiene ${-r['debe']:,.2f} a favor**")
    else:
        st.markdown("**La cuenta está en cero.**")
    if rem.get("estado") == "Abierta":
        with st.expander("💵 Registrar un pago", expanded=r["debe"] > 0.005):
            c1, c2, c3, c4 = st.columns([2, 2, 3, 2])
            monto = c1.number_input("MONTO ($)", min_value=0.0, step=500.0, format="%.2f", key=k("pg_monto"))
            forma = c2.selectbox("FORMA DE PAGO", catalogo(sb, "forma_pago"), index=None, key=k("pg_forma"),
                                 placeholder="Elegir…")
            banco = c3.selectbox("BANCO / CAJA", catalogo(sb, "banco_caja"), index=None, key=k("pg_banco"),
                                 placeholder="Elegir…")
            fecha = c4.date_input("FECHA", value=_hoy(), key=k("pg_fecha"), format="DD/MM/YYYY")
            if monto > 0 and r["debe"] > 0.005 and monto > r["debe"] + 0.005:
                st.caption(f"Cubre lo que debe y le quedan **${monto - r['debe']:,.2f} a favor**.")
            if st.button("Registrar pago", key=k("pg_ok")):
                quien = codigos.confirmar(sb, q_v, q_c)
                if quien:
                    try:
                        registrar_pago(sb, rem, monto, forma, banco, fecha, quien)
                        for x in ("pg_monto", "pg_forma", "pg_banco"):
                            ss.pop(k(x), None)
                        nuevo = round(r["debe"] - monto, 2)
                        ss["rm_msgs"] = [("ok", f"Pago de ${monto:,.2f} registrado (ya cuenta en Caudal). "
                                          + (f"Debe ${nuevo:,.2f}." if nuevo > 0.005 else
                                             f"Le quedan ${-nuevo:,.2f} a favor." if nuevo < -0.005 else
                                             "La cuenta quedó en cero."))]
                        st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
        if pagos:
            with st.expander("Borrar un pago cargado por error"):
                ops = {f"{p.get('fecha') or ''} · ${_num(p.get('monto')):,.2f} · {p.get('forma_pago') or ''}": p
                       for p in sorted(pagos, key=lambda x: x["id"], reverse=True)}
                elegido = st.selectbox("Pago", list(ops), index=None, key=k("pg_del"), placeholder="Elegir…")
                st.caption("Al borrarlo deja de contar en Caudal.")
                if st.button("Borrar pago", key=k("pg_del_ok"), disabled=not elegido):
                    quien = codigos.confirmar(sb, q_v, q_c)
                    if quien:
                        borrar_pago(sb, rem, ops[elegido], quien)
                        ss.pop(k("pg_del"), None)
                        ss["rm_msgs"] = [("ok", f"Pago borrado: {elegido}.")]
                        st.rerun()

        st.divider()
        if st.button("Cerrar cuenta", key=k("cerrar"),
                     help="Cuando no quedan piezas apartadas y la cuenta está en cero."):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    cerrar(sb, rem, items, pagos, quien)
                    ss["rm_msgs"] = [("ok", "Cuenta cerrada.")]
                    st.rerun()
                except ValueError as ex:
                    st.error(str(ex))


def tab_remisiones(sb, user):
    """Ventas › Remisiones: las de pedido (las cuentas corrientes tienen su propia sección)."""
    ss = st.session_state
    if ss.get("rm_sel"):
        _detalle(sb, user, ss["rm_sel"], "rm_sel")
        return
    for m in ss.pop("rm_lista_msgs", []):
        (st.success if m[0] == "ok" else st.warning)(m[1])
    st.caption("Las remisiones se generan desde el detalle del pedido («📋 Generar remisión»).")
    _lista(sb, user, "pedido")


def tab_cuenta_corriente(sb, user):
    """Ventas › Cuenta corriente: clientes que se llevan, devuelven y pagan de a poco, con un solo saldo."""
    ss = st.session_state
    if ss.get("cc_sel"):
        _detalle(sb, user, ss["cc_sel"], "cc_sel")
        return
    for m in ss.pop("cc_lista_msgs", []):
        (st.success if m[0] == "ok" else st.warning)(m[1])
    vista = st.radio("Ver", ["Cuentas", "Nueva cuenta corriente"], horizontal=True, key="cc_vista",
                     label_visibility="collapsed")
    if vista == "Cuentas":
        _lista(sb, user, "abierta")
    else:
        _nueva_abierta(sb, user)
