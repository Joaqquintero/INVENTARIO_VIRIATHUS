"""
Caudal: cuánto dinero tiene (o puso de su bolsa) cada persona, en efectivo y en transferencia.

Cuentas: Caja (efectivo de la tienda), Ensalada de Ideas (lo que entra al banco de la tienda: transferencias, TPV,
depósitos) y Ernesto, Viriato, Claudia y Mary — cada persona con efectivo y transferencia.
Verde = saldo a favor. Rojo = puso de su bolsa y la tienda le debe.

De dónde salen los movimientos:
  * VENTAS (se calculan solas de los pagos de los pedidos, sin los cancelados):
      - forma de pago «Efectivo» → Caja · efectivo
      - lo demás (depósito, transferencia, tarjeta) → transferencia de la persona del banco:
        BANCOMER CLAU → Claudia · BANCOMER VIRIATO → Viriato · BANCOMER ERNESTO → Ernesto ·
        otro banco (TPV BBVA, Bancomer / depósito Ensalada de Ideas…) → Ensalada de Ideas
    Si un pedido se cancela, sus cobros dejan de contar solos. Se corrigen desde el pedido.
  * COMPRAS, RESTAUROS, COSTO FINANCIERO, FLETES y OTROS COSTOS (al guardar la ficha): restan a «Quién pagó», en
    efectivo o transferencia según la forma de pago. Solo cuenta lo que se carga o cambia desde el encendido (si una compra vieja pasa de $5,000 a $6,000,
    se resta $1,000). «Ponerse al día» carga de una vez los costos completos de todas las fichas.
  * MANUALES: traspasos (Caja → Ernesto, o efectivo → transferencia), entradas y gastos.
  * CAJA · AUXILIAR: compras viejas que no se sabe bien quién pagó (se pasaron ahí desde Caja · efectivo). Se ve
    en la tarjeta de Caja pero NO suma al total: Caja · efectivo es lo que de verdad hay en la caja. Si se cambia el
    costo de una de esas fichas, lo ya cargado en auxiliar cuenta como pagado (no se vuelve a cobrar a efectivo).
  * APARTADOS (pendientes.py): plata separada en una cuenta para pagar varios pendientes. No es un movimiento: la
    tarjeta muestra lo LIBRE (saldo − apartado). Cada pago desde un apartado es una sola línea en Movimientos.
Los movimientos no se borran: se anulan (quién, cuándo y por qué).

Tablas: ver CAUDAL_SQL.
"""
import datetime as dt
import json
import uuid
from html import escape

import streamlit as st

from corte import pedido_desde

import codigos
from permisos import roles as _roles

ROLES_CAUDAL = _roles("caudal")
CAJA, ENSALADA = "Caja", "Ensalada de Ideas"
CUENTAS = (CAJA, ENSALADA, "Ernesto", "Viriato", "Claudia", "Mary")
MEDIOS = ("efectivo", "transferencia")
AUX = "auxiliar"
# Caja: efectivo (lo que de verdad hay en caja) y auxiliar (cosas viejas que no se sabe bien quién pagó: NO suma al
# total de Caja). Ensalada de Ideas solo banco; las personas tienen efectivo y transferencia.
MEDIOS_DE = {CAJA: ("efectivo", AUX), ENSALADA: ("transferencia",)}
TODOS_MEDIOS = MEDIOS + (AUX,)


def normalizar(cuenta, medio):
    """(cuenta, medio) con las reglas de hoy: lo que antes era «Caja · transferencia» es «Ensalada de Ideas»
    (también los movimientos viejos que quedaron guardados así), y Ensalada de Ideas siempre es transferencia."""
    if cuenta == CAJA and medio == "transferencia":
        return ENSALADA, "transferencia"
    if cuenta == ENSALADA:
        return ENSALADA, "transferencia"
    return cuenta, medio
VERDE, ROJO, AZUL, GRIS = "#5fcf8a", "#f08a78", "#9fb4d8", "#9a9284"

CAUDAL_SQL = """
create table if not exists caudal_movimientos (
  id bigserial primary key,
  creado timestamptz not null default now(),
  fecha date not null,
  cuenta text not null,                 -- Caja | Ernesto | Viriato | Claudia | Mary
  medio text not null,                  -- efectivo | transferencia
  monto numeric not null,               -- + entra · − sale
  tipo text not null,                   -- compra | restauro | entrada | gasto | traspaso
  concepto text, detalle text,
  origen text not null default 'manual',-- manual | ficha
  referencia text,                      -- ficha: «ficha:<modelo>:compra:<n>» / «ficha:<modelo>:restauro:<n>»
  grupo text,                           -- los dos lados de un traspaso
  usuario text,
  anulado boolean not null default false, anulado_por text, anulado_en timestamptz, motivo text
);
create table if not exists caudal_config (clave text primary key, valor text);
"""


def _num(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip()) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _hoy():
    try:
        from cotizaciones import hoy_mx
        return hoy_mx()
    except Exception:
        return dt.date.today()


def _fecha(v):
    if not v:
        return None
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def dinero(n):
    return ("−" if n < -0.004 else "") + f"${abs(n):,.2f}"


# ----------------------------------------------------------------------
# Reglas: a qué cuenta va cada cosa
# ----------------------------------------------------------------------
def cuenta_de_quien(qp):
    """«2 - ERNESTO» → Ernesto (lo de «Quién pagó» en la ficha). Sin reconocer → Caja.
    (Caja pagando por transferencia queda como Ensalada de Ideas: ver normalizar.)"""
    t = str(qp or "").upper()
    if "ENSALADA" in t:
        return ENSALADA
    for c in CUENTAS:
        if c.upper() in t:
            return c
    return CAJA


def medio_de_forma(fp):
    return "efectivo" if "EFECTIVO" in str(fp or "").upper() else "transferencia"


def es_auxiliar(texto):
    """«7 - CAJA AUXILIAR» (quién pagó) o «CAJA AUXILIAR» (banco / caja de un cobro) → Caja · auxiliar."""
    return "AUXILIAR" in str(texto or "").upper()


def cuenta_medio_de_ficha(qp, fp):
    """(cuenta, medio) de un costo de la ficha según «Quién pagó» y «Forma de pago»."""
    if es_auxiliar(qp):
        return CAJA, AUX
    return normalizar(cuenta_de_quien(qp), medio_de_forma(fp))


def cuenta_de_banco(banco):
    t = str(banco or "").upper()
    if "CLAU" in t:
        return "Claudia"
    if "VIRIATO" in t:
        return "Viriato"
    if "ERNESTO" in t:
        return "Ernesto"
    return ENSALADA                      # TPV BBVA, Bancomer / depósito Ensalada de Ideas y cualquier otro banco


def cuenta_de_venta(forma_pago, banco_caja):
    """(cuenta, medio) de un cobro de pedido."""
    if es_auxiliar(banco_caja):
        return CAJA, AUX
    if str(forma_pago or "").strip().lower().startswith("efectivo"):
        return CAJA, "efectivo"
    return cuenta_de_banco(banco_caja), "transferencia"


# ----------------------------------------------------------------------
# Configuración (fecha de inicio)
# ----------------------------------------------------------------------
def fecha_inicio(sb):
    try:
        r = sb.table("caudal_config").select("valor").eq("clave", "fecha_inicio").execute().data
        return _fecha(r[0]["valor"]) if r else None
    except Exception:
        return None


def poner_fecha_inicio(sb, fecha, usuario):
    sb.table("caudal_config").upsert({"clave": "fecha_inicio", "valor": fecha.isoformat()}).execute()
    codigos.registrar(sb, usuario, "Cambió inicio de Caudal", "caudal", "fecha_inicio", fecha.isoformat())


# ----------------------------------------------------------------------
# Movimientos guardados (ficha y manuales)
# ----------------------------------------------------------------------
def _insertar(sb, filas):
    if filas:
        sb.table("caudal_movimientos").insert(filas).execute()


def movimiento_manual(sb, tipo, monto, concepto, usuario, fecha=None, de=None, a=None):
    """tipo: 'traspaso' (de → a), 'entrada' (a), 'gasto' (de). de / a = (cuenta, medio)."""
    de = normalizar(*de) if de else None
    a = normalizar(*a) if a else None
    monto = round(_num(monto), 2)
    if monto <= 0:
        raise ValueError("El monto debe ser mayor que 0.")
    fecha = (fecha or _hoy()).isoformat()
    base = {"fecha": fecha, "tipo": tipo, "concepto": (concepto or "").strip() or None, "origen": "manual",
            "usuario": usuario}
    if tipo == "traspaso":
        if not de or not a:
            raise ValueError("Elige de qué cuenta sale y a cuál entra.")
        if tuple(de) == tuple(a):
            raise ValueError("La cuenta de origen y la de destino son la misma.")
        g = uuid.uuid4().hex[:12]
        det = f"{de[0]} · {de[1]} → {a[0]} · {a[1]}"
        filas = [{**base, "cuenta": de[0], "medio": de[1], "monto": -monto, "grupo": g, "detalle": det},
                 {**base, "cuenta": a[0], "medio": a[1], "monto": monto, "grupo": g, "detalle": det}]
    elif tipo == "entrada":
        if not a:
            raise ValueError("Elige a qué cuenta entra.")
        filas = [{**base, "cuenta": a[0], "medio": a[1], "monto": monto}]
    elif tipo == "gasto":
        if not de:
            raise ValueError("Elige de qué cuenta sale.")
        filas = [{**base, "cuenta": de[0], "medio": de[1], "monto": -monto}]
    else:
        raise ValueError("Tipo de movimiento desconocido.")
    _insertar(sb, filas)
    codigos.registrar(sb, usuario, "Movimiento en Caudal", "caudal", tipo, f"${monto:,.2f} · {concepto or ''}")


def anular(sb, mov, motivo, usuario):
    if not (motivo or "").strip():
        raise ValueError("Escribe por qué se anula.")
    datos = {"anulado": True, "anulado_por": usuario, "anulado_en": dt.datetime.now(dt.timezone.utc).isoformat(),
             "motivo": motivo.strip()}
    q = sb.table("caudal_movimientos").update(datos)
    if mov.get("grupo"):                        # un traspaso se anula completo (los dos lados)
        q.eq("grupo", mov["grupo"]).execute()
    else:
        q.eq("id", mov["id"]).execute()
    if mov.get("origen") == "pendiente":          # era el pago de un pendiente: vuelve a por pagar
        try:
            import pendientes
            pendientes.deshacer_pago(sb, mov)
        except Exception:
            pass
    codigos.registrar(sb, usuario, "Anuló movimiento de Caudal", "caudal", str(mov.get("id")), motivo.strip())


# ----------------------------------------------------------------------
# Ficha → compras y restauros
# ----------------------------------------------------------------------
# (tipo en Caudal, lista en la ficha, monto suelto de fichas anteriores, nombre)
GASTOS_FICHA = (("financiero", "COSTOS_FINANCIEROS", "COSTO_FINANCIERO", "Costo financiero"),
                ("flete", "FLETES", "COSTO_FLETES", "Flete"),
                ("otro", "OTROS_COSTOS_DETALLE", "OTROS_COSTOS", "Otro costo"))
NOMBRE_TIPO = {"compra": "Compra", "restauro": "Restauro", "financiero": "Costo financiero", "flete": "Flete",
               "otro": "Otro costo"}


def unidades_compradas(datos):
    """Cuántas piezas se compraron en el lote (UNIDADES COMPRADAS de la ficha; 1 si no dice).
    Los costos de la ficha son POR PIEZA; lo que salió de la cuenta es costo × unidades compradas.
    No es CANTIDAD: CANTIDAD baja con cada venta, las unidades compradas no cambian."""
    try:
        u = int(float(str((datos or {}).get("UNIDADES_COMPRADAS") or 1).replace(",", "")))
    except (TypeError, ValueError):
        u = 1
    return max(u, 1)


def _bloque(clave, tipo, b, costo, texto, unidades=1):
    """Un costo de la ficha para Caudal, o None si está «Pendiente por pagar» (se descuenta al pagarlo, desde
    Caudal → Pendientes). Si se pagó desde Pendientes cuentan el monto y la cuenta del pago, no lo que diga hoy la ficha.
    El costo de la ficha es por pieza: a Caudal va × `unidades` (las unidades compradas del lote)."""
    if b.get("pend"):
        return None
    if b.get("pagado_monto") is not None:
        if b.get("pagado_cuenta"):
            cm = normalizar(b["pagado_cuenta"], b.get("pagado_medio") or medio_de_forma(b.get("fp")))
        else:
            cm = cuenta_medio_de_ficha(b.get("qp"), b.get("fp"))
        return (clave, tipo, *cm, round(_num(b.get("pagado_monto")), 2), texto)
    if unidades > 1:
        texto += f" · {unidades} piezas × {dinero(_num(costo))}"
    return (clave, tipo, *cuenta_medio_de_ficha(b.get("qp"), b.get("fp")), round(_num(costo) * unidades, 2), texto)


def _bloques(datos):
    """[(clave, tipo, cuenta, medio, costo, texto)] de los costos de una ficha (sin los pendientes por pagar)."""
    datos = datos or {}
    out = []
    u = unidades_compradas(datos)
    compras = datos.get("COMPRAS") or []
    if not compras and (datos.get("COSTO_ADQUISICION") or datos.get("PROVEEDOR_COMPRA")):
        compras = [{"prov": datos.get("PROVEEDOR_COMPRA"), "fp": datos.get("FORMA_PAGO_COMPRA"),
                    "qp": datos.get("QUIEN_PAGO_COMPRA"), "costo": datos.get("COSTO_ADQUISICION")}]
    for n, c in enumerate(compras):
        out.append(_bloque(f"compra:{n}", "compra", c, c.get("costo"),
                           f"Compra #{n + 1}" + (f" · {c.get('prov')}" if c.get("prov") else ""), u))
    rest = datos.get("RESTAURADORES") or []
    for n, r in enumerate(rest):
        costo = r.get("costo")
        if costo in (None, "") and n == 0 and not any("costo" in x for x in rest):
            costo = datos.get("COSTO_RESTAURO")
        out.append(_bloque(f"restauro:{n}", "restauro", r, costo,
                           f"Restaurador #{n + 1}" + (f" · {r.get('prov')}" if r.get("prov") else ""), u))
    # Costo financiero, fletes y otros costos: igual que restauro. Fichas anteriores (solo el monto suelto, sin
    # quién pagó) cuentan como un bloque, para que al pasar a bloques no se registre como un costo nuevo.
    for tipo, lista_k, total_k, nombre in GASTOS_FICHA:
        lista = datos.get(lista_k) or []
        if not lista and _num(datos.get(total_k)):
            lista = [{"costo": datos.get(total_k)}]
        for n, b in enumerate(lista):
            out.append(_bloque(f"{tipo}:{n}", tipo, b, b.get("costo"),
                               f"{nombre} #{n + 1}" + (f" · {b.get('prov')}" if b.get("prov") else ""), u))
    return [b for b in out if b is not None]


def _cargado(sb, modelo):
    """{referencia: {(cuenta, medio): suma}} de lo que Caudal ya restó por la ficha (sin anulados)."""
    filas = (sb.table("caudal_movimientos").select("referencia,cuenta,medio,monto,anulado")
             .like("referencia", f"ficha:{modelo}:%").execute().data or [])
    out = {}
    for f in filas:
        if f.get("anulado"):
            continue
        d = out.setdefault(f["referencia"], {})
        k = normalizar(f["cuenta"], f["medio"])
        d[k] = round(d.get(k, 0.0) + _num(f["monto"]), 2)
    return out


def _es_pendiente(datos, clave):
    """¿El costo `clave` («restauro:0») de la ficha está marcado «Pendiente por pagar»?"""
    tipo, n = clave.split(":")
    lista_k = {"compra": "COMPRAS", "restauro": "RESTAURADORES"}.get(tipo) or next(
        (lk for t, lk, _, _ in GASTOS_FICHA if t == tipo), None)
    lista = (datos or {}).get(lista_k) or []
    return int(n) < len(lista) and bool(lista[int(n)].get("pend"))


def sincronizar_ficha(sb, modelo, previos, nuevos, usuario, completo=False):
    """Registra en Caudal los cambios de costo de compras y restauros de una ficha.
    Normal: solo la diferencia contra lo que tenía la ficha antes de guardar (lo viejo no cuenta).
    completo=True («Ponerse al día»): el costo entero de cada compra / restauro.
    Devuelve cuántos movimientos registró. No hace nada si Caudal no está encendido."""
    if not fecha_inicio(sb):
        return 0
    modelo = str(modelo)
    nombre = " ".join(str((nuevos or {}).get("NOMBRE", "") or "").split())
    antes = {b[0]: b for b in _bloques(previos)}
    ahora = {b[0]: b for b in _bloques(nuevos)}
    cargado = _cargado(sb, modelo)
    hoy = _hoy().isoformat()
    filas = []
    for clave in sorted(set(antes) | set(ahora)):
        ref = f"ficha:{modelo}:{clave}"
        ya = cargado.get(ref, {})
        total_ya = round(sum(ya.values()), 2)                  # negativo: lo que ya se restó
        nuevo_b = ahora.get(clave)
        costo_nuevo = nuevo_b[4] if nuevo_b else 0.0
        if completo:
            objetivo = -costo_nuevo
        else:
            costo_viejo = antes[clave][4] if clave in antes else 0.0
            objetivo = total_ya - (costo_nuevo - costo_viejo)
            objetivo = max(min(objetivo, 0.0), -costo_nuevo)   # nunca a favor, nunca más que el costo
        objetivo = round(objetivo, 2)
        destino = (nuevo_b[2], nuevo_b[3]) if nuevo_b else None
        tipo = clave.split(":")[0]
        texto = nuevo_b[5] if nuevo_b else (antes[clave][5] if clave in antes else clave)
        base = {"fecha": hoy, "tipo": tipo, "origen": "ficha", "referencia": ref, "usuario": usuario,
                "concepto": f"{NOMBRE_TIPO.get(tipo, tipo.capitalize())} · Mod {modelo}",
                "detalle": " · ".join(x for x in (texto, nombre) if x)}
        if ya and not completo:
            # Ya había algo cargado de este costo: lo que se registre ahora es una CORRECCIÓN (se ve en azul)
            costo_antes = antes[clave][4] if clave in antes else 0.0
            base["concepto"] = "Corrección · " + base["concepto"]
            base["detalle"] = (f"costo {dinero(costo_antes)} → {dinero(costo_nuevo)}"
                               + (" (pasó a pendiente por pagar)" if nuevo_b is None and _es_pendiente(nuevos, clave) else "")
                               + " · " + base["detalle"])
        # Lo que se pasó a Caja · auxiliar (compras viejas) cuenta como ya cargado en Caja · efectivo: no se mueve
        # (salvo que la ficha dijera «7 - CAJA AUXILIAR» y ahora diga Caja: ahí sí se pasa a efectivo)
        era_aux = clave in antes and tuple(antes[clave][2:4]) == (CAJA, AUX)
        aux = (CAJA, AUX) if destino == (CAJA, "efectivo") and not era_aux else None
        # Lo ya restado que quedó en otra cuenta (cambió quién pagó o la forma de pago) se devuelve allá
        for (cta, med), suma in ya.items():
            if (cta, med) not in (destino, aux) and abs(suma) > 0.004:
                filas.append({**base, "cuenta": cta, "medio": med, "monto": round(-suma, 2),
                              "detalle": base["detalle"] + (" · cambió quién pagó o la forma de pago" if destino else "")})
        en_aux = ya.get(aux, 0.0) if aux else 0.0
        en_destino = (ya.get(destino, 0.0) if destino else 0.0) + en_aux
        delta = round(objetivo - en_destino, 2)
        if destino and delta > 0.004 and en_aux < -0.004:
            # Bajó el costo y parte está en auxiliar: primero se devuelve lo cargado en efectivo, el resto al auxiliar
            en_ef = min(ya.get(destino, 0.0), 0.0)
            al_aux = round(min(max(delta + en_ef, 0.0), -en_aux), 2)
            if al_aux > 0.004:
                filas.append({**base, "cuenta": CAJA, "medio": AUX, "monto": al_aux})
                delta = round(delta - al_aux, 2)
        if destino and abs(delta) > 0.004:
            filas.append({**base, "cuenta": destino[0], "medio": destino[1], "monto": delta})
    _insertar(sb, filas)
    return len(filas)


def ponerse_al_dia(sb, usuario, progreso=None):
    """Carga en Caudal el costo completo de compras y restauros de todas las fichas (descontando lo ya cargado)."""
    from ventas import _parse
    total, n_mov, desde = 0, 0, 0
    while True:
        filas = (sb.table("productos").select("MODELO,datos_json").order("MODELO")
                 .range(desde, desde + 499).execute().data or [])
        for f in filas:
            d = _parse(f.get("datos_json"))
            if _bloques(d):
                n_mov += sincronizar_ficha(sb, f["MODELO"], d, d, usuario, completo=True)
            total += 1
        if progreso:
            progreso(total)
        if len(filas) < 500:
            break
        desde += 500
    codigos.registrar(sb, usuario, "Caudal: ponerse al día", "caudal", "fichas", f"{n_mov} movimiento(s), {total} fichas")
    return n_mov, total


# ----------------------------------------------------------------------
# Lectura: movimientos guardados + ventas calculadas
# ----------------------------------------------------------------------
def _todas(armar, por_pagina=1000):
    """Lee TODAS las filas de una consulta, de a 1,000 (Supabase no devuelve más por vez).
    `armar()` devuelve la consulta nueva cada vez (sin range)."""
    out, desde = [], 0
    while True:
        filas = armar().range(desde, desde + por_pagina - 1).execute().data or []
        out += filas
        if len(filas) < por_pagina:
            return out
        desde += por_pagina


def _fecha_venta(pago, ped, inicio):
    """Fecha con que cuenta un cobro, o None si es de antes del inicio.
    La fecha de pago si es desde el inicio; si no, la del pedido (si el pedido es desde el inicio)."""
    fp, fpe = _fecha(pago.get("fecha_pago")), _fecha(ped.get("fecha"))
    if fp and fp >= inicio:
        return fp
    if fpe and fpe >= inicio:
        return fpe
    return None


def _ventas(sb, inicio):
    """Cobros de pedidos hechos en la app (no cancelados, no importados) desde `inicio`, de solo lectura."""
    ini = inicio.isoformat()
    try:
        pagos = {p["id"]: p for p in _todas(lambda: sb.table("pedido_pagos").select("*")
                                            .gte("fecha_pago", ini).order("id"))}
        peds = {x["numero_pedido"]: x for x in _todas(lambda: sb.table("pedidos")
                .select("numero_pedido,cliente,fecha,status,estado_pedido,origen").gte("fecha", ini).order("numero_pedido"))}
        # Cobros de pedidos recientes (aunque la fecha de pago esté vacía o sea anterior)
        nums = sorted(peds)
        for i in range(0, len(nums), 300):
            for p in (sb.table("pedido_pagos").select("*").in_("numero_pedido", nums[i:i + 300])
                      .limit(5000).execute().data or []):
                pagos[p["id"]] = p
        # Pedidos de los cobros que faltan
        faltan = sorted({p["numero_pedido"] for p in pagos.values()} - set(peds))
        for i in range(0, len(faltan), 300):
            for x in (sb.table("pedidos").select("numero_pedido,cliente,fecha,status,estado_pedido,origen")
                      .in_("numero_pedido", faltan[i:i + 300]).execute().data or []):
                peds[x["numero_pedido"]] = x
    except Exception as ex:
        # Sin esto Caudal mostraría saldos sin las ventas y nadie se daría cuenta
        st.error("⚠️ No se pudieron leer los cobros de los pedidos: los saldos de abajo NO incluyen las ventas. "
                 f"Recarga la página en un momento; si sigue igual, avisa. ({ex})")
        return []
    out = []
    for p in pagos.values():
        ped = peds.get(p["numero_pedido"])
        if motivo_no_cuenta(p, ped, inicio):
            continue
        cta, med = cuenta_de_venta(p.get("forma_pago"), p.get("banco_caja"))
        out.append({"id": f"v{p.get('id')}", "fecha": _fecha_venta(p, ped, inicio).isoformat(), "cuenta": cta,
                    "medio": med, "monto": round(_num(p.get("monto")), 2), "tipo": "venta", "origen": "pedido",
                    "concepto": f"Venta · Pedido {p['numero_pedido']}",
                    "detalle": " · ".join(x for x in (ped.get("cliente"), p.get("forma_pago"), p.get("banco_caja")) if x),
                    "anulado": False})
    return out


def motivo_no_cuenta(pago, ped, inicio):
    """Por qué un cobro NO entra a Caudal ("" si sí entra)."""
    if not ped:
        return "el pedido no existe"
    if ped.get("status") == "Cancelado" or ped.get("estado_pedido") == "Cancelado":
        return "el pedido está cancelado"
    if ped.get("origen") == "historico":
        return "es un pedido viejo importado del Excel"
    if int(ped.get("numero_pedido") or 0) < pedido_desde():
        return f"es anterior al pedido {pedido_desde()} (los anteriores están ocultos)"
    if round(_num(pago.get("monto")), 2) <= 0:
        return "el cobro es de $0"
    if not _fecha_venta(pago, ped, inicio):
        return f"el cobro y el pedido son de antes del inicio de Caudal ({inicio:%d/%m/%Y})"
    return ""


def explicar_pedido(sb, numero, inicio):
    """Lista de líneas: cómo cuenta cada cobro de un pedido en Caudal."""
    ped = sb.table("pedidos").select("numero_pedido,cliente,fecha,status,estado_pedido,origen") \
        .eq("numero_pedido", int(numero)).execute().data
    if not ped:
        return [f"No existe el pedido {numero}."]
    ped = ped[0]
    pagos = sb.table("pedido_pagos").select("*").eq("numero_pedido", int(numero)).execute().data or []
    if not pagos:
        return [f"El pedido {numero} no tiene cobros registrados (sin forma de pago): no entra a Caudal."]
    out = []
    for p in pagos:
        cta, med = cuenta_de_venta(p.get("forma_pago"), p.get("banco_caja"))
        m = motivo_no_cuenta(p, ped, inicio)
        txt = f"{p.get('forma_pago') or '—'} · {p.get('banco_caja') or '—'} · ${_num(p.get('monto')):,.2f}"
        out.append(f"❌ {txt}: no cuenta porque {m}." if m else f"✅ {txt} → {cta} · {med}")
    return out


def movimientos(sb, inicio):
    guardados = _todas(lambda: sb.table("caudal_movimientos").select("*").gte("fecha", inicio.isoformat()).order("id"))
    for m in guardados:
        m["cuenta"], m["medio"] = normalizar(m.get("cuenta"), m.get("medio"))
    todos = guardados + _ventas(sb, inicio)
    todos.sort(key=lambda m: (str(m.get("fecha") or ""), str(m.get("creado") or "")), reverse=True)
    return todos


def saldos(movs):
    s = {c: {md: 0.0 for md in TODOS_MEDIOS} for c in CUENTAS}
    for m in movs:
        if m.get("anulado") or m.get("cuenta") not in s:
            continue
        s[m["cuenta"]][m.get("medio") if m.get("medio") in TODOS_MEDIOS else "transferencia"] += _num(m.get("monto"))
    return s


# ----------------------------------------------------------------------
# Pantalla
# ----------------------------------------------------------------------
def _color(n):
    return VERDE if n > 0.004 else (ROJO if n < -0.004 else "#d9d1c3")


def _tarjetas(s, apartado=None):
    """`apartado` = {(cuenta, medio): monto} de los apartados abiertos: la tarjeta muestra lo LIBRE."""
    apartado = apartado or {}
    cols = st.columns(len(CUENTAS))
    for col, c in zip(cols, CUENTAS):
        ef, tr, aux = s[c]["efectivo"], s[c]["transferencia"], s[c].get(AUX, 0.0)
        tot = ef + tr                                  # el auxiliar NO suma: no es plata que esté en la caja
        ap = sum(v for (cta, _), v in apartado.items() if cta == c)
        medios = MEDIOS_DE.get(c, MEDIOS)
        fila_ef = f"""<div style="display:flex;justify-content:space-between;font-size:.95rem;"><span style="opacity:.75">Efectivo</span>
  <span style="color:{_color(ef)};font-variant-numeric:tabular-nums">{dinero(ef)}</span></div>""" if "efectivo" in medios else ""
        fila_tr = f"""<div style="display:flex;justify-content:space-between;font-size:.95rem;"><span style="opacity:.75">Transferencia</span>
  <span style="color:{_color(tr)};font-variant-numeric:tabular-nums">{dinero(tr)}</span></div>""" if "transferencia" in medios else ""
        fila_aux = f"""<div style="display:flex;justify-content:space-between;font-size:.85rem;opacity:.8;" title="Cosas viejas que no se sabe bien quién pagó. No suma al total.">
  <span style="opacity:.75">Auxiliar <span style="font-size:.72rem">(no suma)</span></span>
  <span style="color:{_color(aux)};font-variant-numeric:tabular-nums">{dinero(aux)}</span></div>""" if AUX in medios else ""
        fila_ap = (f"""<div style="font-size:.78rem;opacity:.8;text-align:right;margin-top:2px;">
  <span style="color:{AZUL}">📦 Apartado {dinero(ap)}</span> · Total {dinero(tot)}</div>""" if ap > 0.004 else "")
        col.markdown(f"""
<div style="border:1px solid rgba(160,150,135,.35);border-radius:12px;padding:14px 16px;">
 <div style="font-size:1.35rem;font-weight:600;margin-bottom:8px;">{c}</div>
 {fila_ef}
 {fila_tr}
 {fila_aux}
 <div style="border-top:1px solid rgba(160,150,135,.35);margin-top:8px;padding-top:8px;display:flex;justify-content:space-between;align-items:baseline;">
  <span style="opacity:.75;font-size:.8rem;letter-spacing:1px">{'LIBRE' if ap > 0.004 else 'TOTAL'}</span>
  <span style="color:{_color(tot - ap)};font-size:1.3rem;font-weight:600;font-variant-numeric:tabular-nums">{dinero(tot - ap)}</span></div>
 {fila_ap}
</div>""", unsafe_allow_html=True)


def es_correccion(m):
    return (str(m.get("concepto") or "").startswith("Corrección")
            or (m.get("origen") == "ficha" and _num(m.get("monto")) > 0))


def _clase(m):
    if m.get("anulado"):
        return "anulado"
    if m.get("tipo") == "traspaso":
        return "traspaso"
    if es_correccion(m):
        return "correccion"
    return "venta" if _num(m.get("monto")) > 0 else "gasto"


def _juntar_apartados(movs):
    """Los pagos hechos desde un apartado en una misma vez (mismo «lote») se ven como UNA línea que se abre."""
    out, lotes = [], {}
    for m in movs:
        lote = str(m.get("lote") or "")
        if not lote.startswith("apartado:"):
            out.append(m)
            continue
        g = lotes.get(lote)
        if g is None:
            nombre = str(m.get("concepto") or "").split(" · Pago", 1)[0]
            g = lotes[lote] = {"fecha": m.get("fecha"), "concepto": nombre, "cuenta": m.get("cuenta"),
                               "medio": m.get("medio"), "origen": "pendiente", "tipo": "pago", "_hijos": []}
            out.append(g)
        g["_hijos"].append(m)
    for g in lotes.values():
        vivos = [h for h in g["_hijos"] if not h.get("anulado")]
        g["monto"] = round(sum(_num(h.get("monto")) for h in (vivos or g["_hijos"])), 2)
        g["anulado"] = not vivos
        if g["anulado"]:
            g["anulado_por"], g["motivo"] = g["_hijos"][0].get("anulado_por"), g["_hijos"][0].get("motivo")
    return out


def _detalle_apartado(m):
    """Lo de adentro de la línea de un apartado: cada pieza pagada (las anuladas, tachadas)."""
    n = len(m["_hijos"])
    filas = "".join(
        f"""<div style="display:flex;justify-content:space-between;gap:12px;{'text-decoration:line-through;opacity:.6;' if h.get('anulado') else ''}">
<span>{escape(str(h.get('concepto') or '').split(' · ', 1)[-1])}{(' · ' + escape(str(h.get('detalle')))) if h.get('detalle') else ''}</span>
<span style="white-space:nowrap;font-variant-numeric:tabular-nums">{dinero(_num(h.get('monto')))}</span></div>"""
        for h in m["_hijos"])
    return (f"""<details><summary style="cursor:pointer;font-size:.8rem;opacity:.75">{n} pieza(s) · ver</summary>
<div style="font-size:.8rem;opacity:.85;margin-top:4px">{filas}</div></details>""")


def _tabla(movs, limite=300):
    movs = _juntar_apartados(movs)
    colores = {"venta": VERDE, "gasto": ROJO, "traspaso": AZUL, "correccion": AZUL, "anulado": GRIS}
    fondos = {"venta": "rgba(95,207,138,.08)", "gasto": "rgba(240,138,120,.08)", "traspaso": "transparent",
              "correccion": "rgba(159,180,216,.08)", "anulado": "transparent"}
    origenes = {"pedido": "Pedido", "ficha": "Ficha", "manual": "Manual", "pendiente": "Pendiente"}
    filas = []
    for m in movs[:limite]:
        k = _clase(m)
        c = colores[k]
        fe = _fecha(m.get("fecha"))
        det = escape(str(m.get("detalle") or ""))
        if m.get("_hijos"):
            det = _detalle_apartado(m)
        if m.get("anulado"):
            det = f"Anulado por {escape(str(m.get('anulado_por') or ''))}: {escape(str(m.get('motivo') or ''))}" + (
                f" · {det}" if det else "")
        monto = _num(m.get("monto"))
        filas.append(f"""<tr style="background:{fondos[k]};{'opacity:.6;' if k == 'anulado' else ''}">
<td style="border-left:3px solid {c};padding:8px 10px;white-space:nowrap">{fe.strftime('%d/%m/%Y') if fe else ''}</td>
<td style="padding:8px 10px"><div>{escape(str(m.get('concepto') or ''))}</div><div style="font-size:.8rem;opacity:.7">{det}</div></td>
<td style="padding:8px 10px;white-space:nowrap">{escape(str(m.get('cuenta')))} · {escape(str(m.get('medio')))}</td>
<td style="padding:8px 10px;font-size:.85rem;opacity:.75">{origenes.get(m.get('origen'), '')}</td>
<td style="padding:8px 10px;text-align:right;white-space:nowrap;font-weight:600;font-variant-numeric:tabular-nums;color:{c};{'text-decoration:line-through;' if k == 'anulado' else ''}">{'+' if monto > 0 else ''}{dinero(monto)}</td>
</tr>""")
    st.markdown(f"""
<div style="overflow-x:auto;max-height:620px;overflow-y:auto;border:1px solid rgba(160,150,135,.35);border-radius:10px">
<table style="width:100%;border-collapse:collapse;font-size:.92rem;min-width:640px">
<thead><tr style="text-align:left;font-size:.75rem;letter-spacing:1px;opacity:.75">
<th style="padding:8px 10px">FECHA</th><th style="padding:8px 10px">CONCEPTO</th><th style="padding:8px 10px">CUENTA</th>
<th style="padding:8px 10px">ORIGEN</th><th style="padding:8px 10px;text-align:right">MONTO</th></tr></thead>
<tbody>{''.join(filas)}</tbody></table></div>""", unsafe_allow_html=True)
    if len(movs) > limite:
        st.caption(f"Se muestran los {limite} más recientes de {len(movs)}. Afina los filtros para ver otros.")


def _opciones_cuenta():
    return [f"{c} · {m}" for c in CUENTAS for m in MEDIOS_DE.get(c, MEDIOS)]


def _partir(op):
    c, m = op.split(" · ")
    return c, m


def _encender(sb):
    st.info("Caudal todavía no está encendido. Al encenderlo, empieza a contar desde la fecha que elijas: las "
            "ventas desde esa fecha, y las compras, restauros y demás costos que se carguen o cambien en las fichas desde ahora.")
    f = st.date_input("Contar desde", value=_hoy(), key="cd_enc_fecha", format="DD/MM/YYYY")
    q_v, q_c = codigos.pedir_identidad(sb, "cd_enc_id")
    if st.button("Encender Caudal", type="primary", key="cd_enc_ok"):
        quien = codigos.confirmar(sb, q_v, q_c)
        if quien:
            try:
                poner_fecha_inicio(sb, f, quien)
                st.rerun()
            except Exception as ex:
                st.error(f"No se pudo encender: {ex}. ¿Ya corriste en Supabase el SQL de Caudal?")


def render(sb, user):
    ss = st.session_state
    st.header("Caudal")
    for m in ss.pop("cd_msgs", []):
        (st.success if m[0] == "ok" else st.warning)(m[1])
    inicio = fecha_inicio(sb)
    if not inicio:
        _encender(sb)
        return
    st.caption(f"Dinero que entra y sale por persona desde el **{inicio:%d/%m/%Y}**. Verde: saldo a favor · "
               "Rojo: puso de su bolsa y la tienda le debe.")
    try:
        movs = movimientos(sb, inicio)
    except Exception as ex:
        st.error(f"No se pudieron leer los movimientos. ¿Ya corriste en Supabase el SQL de Caudal? ({ex})")
        return

    sal = saldos(movs)
    try:
        import pendientes
        apartado = pendientes.apartado_por_cuenta(sb)
    except Exception:
        apartado = {}
    _tarjetas(sal, apartado)
    st.write("")
    st.markdown("**Para registrar o anular, identifícate:**")
    q_v, q_c = codigos.pedir_identidad(sb, "cd_id")

    # Movimientos (lo que ya entró y salió) | Pendientes por pagar (lo que se debe)
    from navegacion import selector
    vista = selector(["Movimientos", "Pendientes por pagar"], "nav_caudal", "Vista de Caudal")
    if vista == "Pendientes por pagar":
        import pendientes
        pendientes.render_lista(sb, user, q_v, q_c, saldos=sal)
        return

    izq, der = st.columns([2, 1])
    with izq:
        st.subheader("Movimientos")
        f1, f2, f3, f4 = st.columns(4)
        persona = f1.selectbox("Persona", ["Todas"] + list(CUENTAS), key="cd_f_persona")
        tipo = f2.selectbox("Tipo", ["Todos", "Ventas", "Gastos", "Traspasos", "Correcciones", "Anulados"],
                            key="cd_f_tipo")
        desde = f3.date_input("Desde", value=inicio, key="cd_f_desde", format="DD/MM/YYYY")
        hasta = f4.date_input("Hasta", value=None, key="cd_f_hasta", format="DD/MM/YYYY")
        st.markdown(f"<span style='color:{VERDE}'>●</span> Venta (entra) &nbsp; <span style='color:{ROJO}'>●</span> "
                    f"Gasto (compra / restauro) &nbsp; <span style='color:{AZUL}'>●</span> Traspaso o corrección &nbsp; "
                    f"<span style='color:{GRIS}'>●</span> Anulado", unsafe_allow_html=True)
        filtro = {"Ventas": "venta", "Gastos": "gasto", "Traspasos": "traspaso", "Correcciones": "correccion",
                  "Anulados": "anulado"}.get(tipo)
        vis = []
        for m in movs:
            fe = _fecha(m.get("fecha"))
            if persona != "Todas" and m.get("cuenta") != persona:
                continue
            if filtro and _clase(m) != filtro:
                continue
            if desde and fe and fe < desde:
                continue
            if hasta and fe and fe > hasta:
                continue
            vis.append(m)
        if vis:
            _tabla(vis)
        else:
            st.info("No hay movimientos con ese filtro.")

        anulables = [m for m in vis if not m.get("anulado") and m.get("origen") in ("manual", "ficha", "pendiente")]
        with st.expander("Anular un movimiento"):
            st.caption("Los movimientos no se borran: quedan tachados con quién los anuló y por qué. Un traspaso se "
                       "anula completo. Las ventas se corrigen desde el pedido (si se cancela, dejan de contar solas). "
                       "Anular el pago de un pendiente lo regresa a «Pendientes por pagar».")
            if not anulables:
                st.caption("No hay movimientos para anular con este filtro.")
            else:
                etiquetas = {m["id"]: f"{_fecha(m.get('fecha')):%d/%m} · {m.get('concepto')} · {m.get('cuenta')} · "
                                      f"{dinero(_num(m.get('monto')))}" for m in anulables}
                sel = st.selectbox("Movimiento", list(etiquetas), format_func=etiquetas.get, index=None,
                                   placeholder="Elegir…", key="cd_an_sel")
                motivo = st.text_input("¿Por qué se anula? *", key="cd_an_motivo")
                if st.button("Anular", key="cd_an_ok", disabled=sel is None):
                    quien = codigos.confirmar(sb, q_v, q_c)
                    if quien:
                        try:
                            anular(sb, next(m for m in anulables if m["id"] == sel), motivo, quien)
                            for x in ("cd_an_sel", "cd_an_motivo"):
                                ss.pop(x, None)
                            ss["cd_msgs"] = [("ok", "Movimiento anulado.")]
                            st.rerun()
                        except Exception as ex:
                            st.error(str(ex))

    with der:
        st.subheader("Nuevo movimiento")
        tipo_n = st.radio("Tipo", ["Traspaso", "Entrada", "Gasto"], horizontal=True, key="cd_n_tipo",
                          label_visibility="collapsed")
        ops = _opciones_cuenta()
        de = a = None
        if tipo_n in ("Traspaso", "Gasto"):
            de = st.selectbox("DE", ops, index=None, key="cd_n_de", placeholder="Elegir cuenta…")
        if tipo_n in ("Traspaso", "Entrada"):
            a = st.selectbox("A", ops, index=None, key="cd_n_a", placeholder="Elegir cuenta…")
        monto = st.number_input("MONTO ($)", min_value=0.0, step=500.0, format="%.2f", key="cd_n_monto")
        concepto = st.text_input("CONCEPTO", key="cd_n_concepto",
                                 placeholder={"Traspaso": "Para compras de la semana",
                                              "Gasto": "Botella de agua – supermercado",
                                              "Entrada": "Préstamo, devolución…"}.get(tipo_n, ""))
        fecha = st.date_input("FECHA", value=_hoy(), key="cd_n_fecha", format="DD/MM/YYYY")
        if st.button(f"Registrar {tipo_n.lower()}", type="primary", key="cd_n_ok", use_container_width=True):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                try:
                    movimiento_manual(sb, tipo_n.lower(), monto, concepto, quien, fecha,
                                      _partir(de) if de else None, _partir(a) if a else None)
                    for x in ("cd_n_de", "cd_n_a", "cd_n_monto", "cd_n_concepto"):
                        ss.pop(x, None)
                    ss["cd_msgs"] = [("ok", f"{tipo_n} de ${monto:,.2f} registrado.")]
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))
        st.caption("Gasto: dinero que sale de una cuenta (p. ej. $100 de Caja · efectivo en una botella de agua). "
                   "Entrada: dinero que entra a una cuenta. Traspaso: sale de una cuenta y entra en otra (p. ej. "
                   "depositar el efectivo de Caja en Ensalada de Ideas).")

    st.divider()
    with st.expander("Cómo se arma cada cuenta"):
        st.markdown("- **Ventas** (solas, de los pagos de los pedidos): «Efectivo» → Caja · efectivo. Depósito o "
                    "transferencia → según el banco: BANCOMER CLAU → Claudia, BANCOMER VIRIATO → Viriato, BANCOMER "
                    "ERNESTO → Ernesto; los demás (TPV BBVA, Bancomer Ensalada de Ideas…) → Ensalada de Ideas. "
                    "Si el pedido se cancela, deja de contar.\n"
                    "- **Compras, restauros, costo financiero, fletes y otros costos** (al guardar la ficha): "
                    "restan a «Quién pagó», en efectivo o "
                    "transferencia según la forma de pago (Caja pagando por transferencia = Ensalada de Ideas). Solo "
                    "cuenta lo que se carga o cambia desde el encendido.\n"
                    "- **Manuales**: traspasos, entradas y gastos de este panel.")
    with st.expander("¿Falta una venta? Revisar un pedido"):
        n = st.text_input("N° de pedido", key="cd_exp_num")
        if n.strip().isdigit():
            for linea in explicar_pedido(sb, n.strip(), inicio):
                st.write(linea)
    with st.expander("Fecha de inicio y ponerse al día"):
        st.caption("Cambiar la fecha de inicio cambia desde cuándo se cuentan ventas y movimientos. «Ponerse al "
                   "día» carga el costo completo de las compras, restauros y demás costos de TODAS las fichas (lo ya cargado no se "
                   "repite): úsalo cuando termines de capturar los costos.")
        c1, c2 = st.columns(2)
        nueva = c1.date_input("Contar desde", value=inicio, key="cd_ini", format="DD/MM/YYYY")
        if c1.button("Cambiar fecha de inicio", key="cd_ini_ok", disabled=nueva == inicio):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                poner_fecha_inicio(sb, nueva, quien)
                ss["cd_msgs"] = [("ok", f"Caudal cuenta desde el {nueva:%d/%m/%Y}.")]
                st.rerun()
        seguro = c2.checkbox("Sí, cargar los costos de todas las fichas", key="cd_aldia_ok")
        if c2.button("Ponerse al día", key="cd_aldia", disabled=not seguro):
            quien = codigos.confirmar(sb, q_v, q_c)
            if quien:
                barra = st.progress(0.0, text="Revisando fichas…")
                try:
                    n_mov, total = ponerse_al_dia(sb, quien, lambda t: barra.progress(min(t / 3000, 1.0),
                                                                                      text=f"{t} fichas revisadas…"))
                    ss.pop("cd_aldia_ok", None)
                    ss["cd_msgs"] = [("ok", f"Listo: {n_mov} movimiento(s) de {total} fichas.")]
                    st.rerun()
                except Exception as ex:
                    st.error(f"No se pudo completar: {ex}")
