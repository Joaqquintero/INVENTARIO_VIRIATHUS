"""
COTIZACIONES: presupuestos con N° de pedido «PENDIENTE» que apartan los productos del inventario por unos días.

  * Los productos quedan RESERVADOS (en el inventario = «r», en Shopify = agotado) mientras la cotización esté vigente.
  * Si vence (o se cancela) sin pasar a pedido, los productos vuelven a estar disponibles.
  * Fotos, medidas, precios y descuentos que se capturan aquí viven SOLO en la cotización: no tocan el inventario.
  * «Pasar a pedido» abre el mismo formulario de Nuevo pedido, ya lleno, y al guardar genera el pedido normal.

Se usa desde ventas.render_ventas (pestaña «Cotizaciones»). Requiere cotizaciones_schema.sql.
"""
import datetime as dt
import io
import json
import uuid

import pandas as pd
import streamlit as st

import codigos
from caches import guardar_en_memoria
import precios
import reservas
from segundo_plano import TareaPeriodica
from ventas import (calcular, buscar_modelo, catalogo, _num, _nn, _parse, _mostrar_ultimo,
                    iniciar_conversion, tab_nuevo_pedido, texto_reservado)

from permisos import roles as _roles
from navegacion import descargar
ROLES_COTIZAR = _roles("cotizar")
DIAS_VIGENCIA = 3
ESTADOS = ("Vigente", "Convertida", "Vencida", "Cancelada")
ICONO = {}


# ----------------------------------------------------------------------
# Fechas (la tienda está en Ciudad de México)
# ----------------------------------------------------------------------
def hoy_mx():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Mexico_City")).date()
    except Exception:
        return (dt.datetime.utcnow() - dt.timedelta(hours=6)).date()


def _fecha(v):
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return hoy_mx()


# ----------------------------------------------------------------------
# Lectura
# ----------------------------------------------------------------------
def listar_cotizaciones(sb, estados=None, limite=300):
    q = sb.table("cotizaciones").select("*")
    if estados:
        q = q.in_("estado", list(estados))
    return q.order("id", desc=True).limit(limite).execute().data


def items_de(sb, cot_id):
    return sb.table("cotizacion_items").select("*").eq("cotizacion_id", cot_id).order("orden").order("id").execute().data


@guardar_en_memoria(ttl=60)
def _totales_lista(_sb, ids, estados):
    """Totales de la lista de cotizaciones (con memoria de 60 s: revisar lo vendido en cada clic era pesado).
    Se borra sola al guardar cualquier cambio."""
    return totales_por_cotizacion(_sb, list(ids), {i: {"estado": e} for i, e in estados})


def totales_por_cotizacion(sb, ids, cots_por_id=None):
    """{id: {total, piezas, bajas}} sin contar lo dado de baja (vendido en otro lado)."""
    if not ids:
        return {}
    filas = []
    for k in range(0, len(ids), 200):
        filas += sb.table("cotizacion_items").select("*").in_("cotizacion_id", ids[k:k + 200]).execute().data or []
    if cots_por_id:
        filas = revisar_bajas(sb, cots_por_id, filas)
    tot = {}
    for f in filas:
        t = tot.setdefault(f["cotizacion_id"], {"total": 0.0, "piezas": 0, "bajas": 0})
        if f.get("baja"):
            t["bajas"] += 1
            continue
        t["total"] += _num(f.get("total_linea"))
        t["piezas"] += int(_num(f.get("cantidad"), 1))
    return tot


# ----------------------------------------------------------------------
# Producto del inventario → datos para la cotización
# ----------------------------------------------------------------------
def _medidas_de(d):
    partes = []
    for clave, etiqueta in (("ALTO", "Alto"), ("LARGO", "Largo"), ("ANCHO_PROF", "Prof.")):
        v = str(d.get(clave, "") or "").strip()
        if v and v not in ("0", "0.0"):
            partes.append(f"{etiqueta} {v}")
    return (" · ".join(partes) + " cm") if partes else ""


def _url_foto_inventario(sb, modelo):
    base = getattr(sb, "supabase_url", None)
    if not base:
        try:
            base = st.secrets.get("SUPABASE_URL", "")
        except Exception:
            base = ""
    return f"{str(base).rstrip('/')}/storage/v1/object/public/imagenes/{modelo}.jpg"


@st.cache_data(ttl=600, show_spinner=False)
def _foto_existe(url):
    try:
        from shopify_borrador import http
        r = http("head", url, timeout=5, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def info_producto(sb, modelo):
    """Datos del inventario para armar el renglón: nombre, precio, costo, medidas y foto. None si no existe."""
    r = sb.table("productos").select("MODELO,datos_json").eq("MODELO", modelo).limit(1).execute()
    if not r.data:
        return None
    d = _parse(r.data[0].get("datos_json"))
    disp = str(d.get("DISP", d.get("DISPONIBILIDAD", "")) or "").strip().lower()
    return {
        "modelo": str(r.data[0]["MODELO"]),
        "nombre": str(d.get("NOMBRE", "")).strip(),
        "precio": _num(d.get("REDONDEO_SHOWROOM")),
        "costo": _num(d.get("TOTAL_COSTO", d.get("COSTO_REGISTRADO"))),
        "utilidad_bruta": _num(d.get("UTILIDAD_BRUTA", d.get("UTILIDAD_SHOWROOM"))),
        "medidas": _medidas_de(d),
        "stock": _num(d.get("CANTIDAD"), 1.0),
        "disp": disp,
        "foto_url": _url_foto_inventario(sb, str(r.data[0]["MODELO"])),
    }


# ----------------------------------------------------------------------
# Reservas y vencimiento
# ----------------------------------------------------------------------
def _liberar_items(sb, items):
    """Devuelve a disponible lo que la cotización tenía apartado. Devuelve avisos."""
    avisos = []
    for it in items:
        if not it.get("apartado") or not it.get("modelo"):
            continue
        try:
            a = reservas.liberar(sb, it["modelo"], int(_num(it.get("reservado"))))
            if a:
                avisos.append(a)
            sb.table("cotizacion_items").update({"apartado": False, "reservado": 0}).eq("id", it["id"]).execute()
            if not a:                                   # quedó libre: ¿otra cotización lo tiene sin apartar?
                p = pasar_reserva(sb, it["modelo"], excluir_cot=it.get("cotizacion_id"))
                if p:
                    avisos.append(p)
        except Exception as ex:
            avisos.append(f"Modelo {it['modelo']}: no se pudo liberar ({ex}).")
    return avisos


def cotizacion_que_aparta(sb, modelo, excluir_cot=None):
    """La cotización vigente que tiene apartado el modelo: {id, codigo, vendedor, cliente} o None."""
    try:
        its = (sb.table("cotizacion_items").select("cotizacion_id").eq("modelo", str(modelo))
               .eq("apartado", True).execute().data or [])
        ids = [i["cotizacion_id"] for i in its if i["cotizacion_id"] != excluir_cot]
        if not ids:
            return None
        c = (sb.table("cotizaciones").select("id,codigo,vendedor,cliente").in_("id", ids)
             .eq("estado", "Vigente").limit(1).execute().data or [])
        return c[0] if c else None
    except Exception:
        return None


def texto_en_cotizacion(c, modelo):
    vend = str(c.get("vendedor") or "").split("/")[0].strip()
    return (f"El modelo {modelo} ya está en la cotización {c.get('codigo')}"
            + (f" de {vend}" if vend else "") + (f" (cliente: {c.get('cliente')})" if c.get("cliente") else ""))


def _apartar_si_se_puede(sb, it_id, modelo, cantidad, cot_id=None):
    """Aparta el renglón. Si el modelo ya lo tiene apartado OTRA cotización, queda compartido (sin apartar).
    Devuelve (apartado: bool, aviso o None). Lanza ValueError si está vendido o en una remisión abierta."""
    ok, msg, aviso = _apartar_item(sb, it_id, modelo, cantidad)
    if ok:
        return True, aviso
    otra = cotizacion_que_aparta(sb, modelo, excluir_cot=cot_id)
    if otra:
        return False, (texto_en_cotizacion(otra, modelo) + ": se agregó sin apartar. Si esa cotización vence o se "
                       "cancela, la reserva pasa a ésta; si se vende, aquí queda dado de baja.")
    if "reservado" in str(msg):
        raise ValueError(texto_reservado(sb, modelo) + ".")
    raise ValueError(msg)


def pasar_reserva(sb, modelo, excluir_cot=None):
    """El modelo quedó libre: lo aparta la siguiente cotización vigente (la más vieja) que lo tenga sin apartar.
    Devuelve un aviso o None."""
    try:
        its = (sb.table("cotizacion_items").select("*").eq("modelo", str(modelo)).execute().data or [])
    except Exception:
        return None
    cands = [i for i in its if not i.get("apartado") and not i.get("baja") and i["cotizacion_id"] != excluir_cot]
    if not cands:
        return None
    cots = {c["id"]: c for c in (sb.table("cotizaciones").select("id,codigo,estado,vigencia_dias")
                                  .in_("id", list({i["cotizacion_id"] for i in cands})).execute().data or [])}
    for i in sorted(cands, key=lambda x: x["cotizacion_id"]):
        c = cots.get(i["cotizacion_id"])
        if not c or c.get("estado") != "Vigente" or int(_num(c.get("vigencia_dias"), 1)) <= 0:
            continue
        ok, _, _ = _apartar_item(sb, i["id"], modelo, int(_num(i.get("cantidad"), 1)))
        if ok:
            return f"La reserva del modelo {modelo} pasó a la cotización {c['codigo']}."
        return None
    return None


def _vendido_en(sb, modelo):
    try:
        r = (sb.table("pedido_items").select("numero_pedido").eq("modelo", str(modelo))
             .order("numero_pedido", desc=True).limit(1).execute().data)
        return f"Vendido en el pedido {r[0]['numero_pedido']}" if r else "Vendido"
    except Exception:
        return "Vendido"


def revisar_bajas(sb, cots_por_id, items):
    """Marca «dado de baja» los renglones de cotizaciones vigentes o vencidas cuyo modelo ya se vendió
    (en otra cotización, un pedido o Shopify). Devuelve los items con «baja» puesto."""
    pend = [i for i in items if i.get("modelo") and not i.get("baja")
            and (cots_por_id.get(i["cotizacion_id"]) or {}).get("estado") in ("Vigente", "Vencida")]
    if not pend:
        return items
    disp = {}
    modelos = sorted({str(i["modelo"]) for i in pend})
    for k in range(0, len(modelos), 100):
        for f in (sb.table("productos").select("MODELO,datos_json").in_("MODELO", modelos[k:k + 100])
                  .execute().data or []):
            disp[str(f["MODELO"]).strip()] = reservas.estado_disp(_parse(f.get("datos_json")))
    motivos = {}
    for i in pend:
        if disp.get(str(i["modelo"])) != "v":
            continue
        m = motivos.setdefault(str(i["modelo"]), _vendido_en(sb, i["modelo"]))
        i["baja"] = m
        i["apartado"], i["reservado"] = False, 0
        try:
            sb.table("cotizacion_items").update({"baja": m, "apartado": False, "reservado": 0}).eq("id", i["id"]).execute()
        except Exception:
            pass                       # sin la columna «baja» se calcula igual cada vez
    return items


def vigentes(items):
    """Renglones que cuentan (sin los dados de baja)."""
    return [i for i in items if not i.get("baja")]


def _apartar_item(sb, it_id, modelo, cantidad):
    """Reserva un renglón ya guardado y anota lo apartado. Devuelve (ok, mensaje, aviso)."""
    ok, msg, aviso, descontadas = reservas.reservar(sb, modelo, int(cantidad))
    if ok:
        sb.table("cotizacion_items").update({"apartado": True, "reservado": descontadas}).eq("id", it_id).execute()
    _apartar_item.ultimas = descontadas          # unidades descontadas (para deshacer si algo falla después)
    return ok, msg, aviso


def vencer_cotizaciones(sb):
    """Pasa a «Vencida» las cotizaciones vigentes cuya fecha límite ya pasó y libera sus productos.
    Segura si varias sesiones la corren a la vez: solo gana quien logra cambiar el estado."""
    hoy = hoy_mx()
    try:
        filas = (sb.table("cotizaciones").select("id,codigo").eq("estado", "Vigente")
                 .lt("vence", hoy.isoformat()).execute().data)
    except Exception as ex:
        return {"ok": False, "error": f"cotizaciones: {ex}", "hora": dt.datetime.now().strftime("%H:%M")}
    vencidas, avisos = 0, []
    for f in filas:
        r = (sb.table("cotizaciones").update({"estado": "Vencida"})
             .eq("id", f["id"]).eq("estado", "Vigente").execute())
        if not r.data:
            continue                      # otra sesión ya la procesó
        vencidas += 1
        avisos += _liberar_items(sb, items_de(sb, f["id"]))
    return {"ok": True, "vencidas": vencidas, "avisos": avisos, "hora": dt.datetime.now().strftime("%H:%M")}


# Revisión en segundo plano: como máximo cada 5 minutos, compartida entre sesiones
TAREA_VENCIMIENTO = TareaPeriodica(vencer_cotizaciones, 300)


def revisar_vencimientos(sb):
    """Para llamar desde app.py en cada recarga: no hace esperar a la página."""
    return TAREA_VENCIMIENTO.obtener(sb)


# ----------------------------------------------------------------------
# Crear / cancelar / renovar / editar
# ----------------------------------------------------------------------
def _subir_foto(sb, codigo, n, datos):
    ruta = f"cotizaciones/{codigo}/{n}_{uuid.uuid4().hex[:6]}.jpg"
    sb.storage.from_("imagenes").upload(path=ruta, file=datos,
                                        file_options={"content-type": "image/jpeg", "upsert": "true"})
    return f"{_url_foto_inventario(sb, 'x').rsplit('/', 1)[0]}/{ruta}"


def miniatura(datos, lado=300):
    """Vista previa chica (JPEG ~20 KB) de una foto subida, para mostrar en pantalla sin cargar la original."""
    from PIL import Image as PILImage, ImageOps
    try:
        im = PILImage.open(io.BytesIO(datos))
        im.draft("RGB", (lado, lado))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((lado, lado))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return None


def _foto_a_jpeg(datos):
    """Reduce y convierte la foto subida a JPEG (ligera para el PDF)."""
    from PIL import Image as PILImage, ImageOps
    im = PILImage.open(io.BytesIO(datos))
    im.draft("RGB", (1200, 1200))                       # abre ya reducido (1/2, 1/4…): ~4 veces menos memoria
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((1200, 1200))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def crear_cotizacion(sb, cab, items, usuario):
    """Guarda la cotización y aparta los productos. Si algún producto no se puede apartar, no se guarda nada.
    Con vigencia 0 días no aparta nada: es solo la cotización (vale por hoy).
    cab = {cliente, vendedor, telefono, email, fecha, vigencia_dias, notas}
    items = [{es_sn, modelo, descripcion, medidas, cantidad, precio_unitario, descuento_pct, costo_unitario,
              utilidad_bruta_inv, foto_url, _foto (bytes o None)}]
    Devuelve (id, codigo, avisos). Lanza ValueError si no se pudo."""
    calc = calcular(items, 0.0, False, 0.0)
    vence = hoy_mx() + dt.timedelta(days=int(cab["vigencia_dias"]))
    r = sb.table("cotizaciones").insert({
        "fecha": _fecha(cab.get("fecha")).isoformat(), "cliente": cab["cliente"], "vendedor": cab["vendedor"],
        "telefono": _nn(cab.get("telefono")), "email": _nn(cab.get("email")),
        "vigencia_dias": int(cab["vigencia_dias"]), "vence": vence.isoformat(),
        "notas": _nn(cab.get("notas")), "created_by": usuario,
    }).execute()
    cot = r.data[0]
    cot_id, codigo = cot["id"], cot["codigo"]
    avisos, apartados = [], []
    try:
        filas = []
        for n, it in enumerate(items):
            foto = it.get("foto_url")
            if it.get("_foto"):
                foto = _subir_foto(sb, codigo, n, _foto_a_jpeg(it["_foto"]))
            filas.append({
                "cotizacion_id": cot_id, "orden": n, "modelo": None if it["es_sn"] else it["modelo"],
                "es_sn": bool(it["es_sn"]), "descripcion": it["descripcion"], "medidas": _nn(it.get("medidas")),
                "cantidad": int(it["cantidad"]), "precio_unitario": it["precio_unitario"],
                "descuento_pct": _num(it.get("descuento_pct")), "total_linea": calc["lineas"][n],
                "costo_unitario": it.get("costo_unitario", 0.0),
                "utilidad_bruta_inv": it.get("utilidad_bruta_inv", 0.0), "foto_url": foto,
            })
        guardadas = sb.table("cotizacion_items").insert(filas).execute().data
        for fila in sorted(guardadas, key=lambda x: x["orden"]):
            if fila["es_sn"] or int(cab["vigencia_dias"]) == 0:
                continue
            hecho, aviso = _apartar_si_se_puede(sb, fila["id"], fila["modelo"], fila["cantidad"], cot_id)
            if hecho:
                apartados.append((fila["modelo"], _apartar_item.ultimas))
            if aviso:
                avisos.append(aviso)
    except Exception as ex:
        # deshacer: liberar lo que ya se apartó y borrar la cotización
        for modelo, desc in apartados:
            try:
                reservas.liberar(sb, modelo, desc)
            except Exception:
                pass
        sb.table("cotizaciones").delete().eq("id", cot_id).execute()
        if isinstance(ex, ValueError):
            raise
        raise ValueError(f"No se pudo guardar la cotización: {ex}")
    return cot_id, codigo, avisos


def cancelar_cotizacion(sb, cot):
    r = (sb.table("cotizaciones").update({"estado": "Cancelada"})
         .eq("id", cot["id"]).eq("estado", "Vigente").execute())
    if not r.data:
        raise ValueError("La cotización ya no está vigente.")
    return _liberar_items(sb, items_de(sb, cot["id"]))


def renovar_cotizacion(sb, cot, dias):
    """Amplía la vigencia. Si ya estaba vencida, vuelve a apartar los productos (si siguen disponibles).
    Devuelve avisos. Lanza ValueError si algún producto ya no está disponible (no cambia nada)."""
    vence = hoy_mx() + dt.timedelta(days=int(dias))
    sin_apartar = int(_num(cot.get("vigencia_dias"), 1)) == 0      # se hizo con vigencia 0: ahora sí aparta
    if cot["estado"] == "Vigente" and not sin_apartar:
        sb.table("cotizaciones").update({"vence": vence.isoformat(), "vigencia_dias": int(dias)}).eq("id", cot["id"]).execute()
        return []
    if cot["estado"] not in ("Vencida", "Vigente"):
        raise ValueError("Solo se pueden renovar cotizaciones vigentes o vencidas.")
    items = items_de(sb, cot["id"])
    hechos, avisos = [], []
    items = revisar_bajas(sb, {cot["id"]: cot}, items)
    for it in items:
        if it["es_sn"] or not it.get("modelo") or it.get("baja") or it.get("apartado"):
            continue
        try:
            hecho, aviso = _apartar_si_se_puede(sb, it["id"], it["modelo"], it["cantidad"], cot["id"])
        except ValueError as ex:
            for h in hechos:                      # deshacer lo ya apartado
                reservas.liberar(sb, h["modelo"], h["_desc"])
                sb.table("cotizacion_items").update({"apartado": False, "reservado": 0}).eq("id", h["id"]).execute()
            raise ValueError(f"No se puede reactivar: {ex}")
        if hecho:
            hechos.append({**it, "_desc": _apartar_item.ultimas})
        if aviso:
            avisos.append(aviso)
    r = (sb.table("cotizaciones").update({"estado": "Vigente", "vence": vence.isoformat(), "vigencia_dias": int(dias)})
         .eq("id", cot["id"]).eq("estado", cot["estado"]).execute())
    if not r.data:
        raise ValueError("La cotización cambió mientras se reactivaba. Revísala.")
    return avisos


def guardar_edicion(sb, cot, filas, quitar_ids, permitir_precio=False, autorizo=None):
    """Cambia descripción, medidas, precio y descuento de los renglones (y quita los marcados).
    filas = {id: {descripcion, medidas, precio_unitario, descuento_pct}}
    Sin `permitir_precio` el precio y el descuento de los modelos no cambian; con permiso, si cambian hace falta
    `autorizo`. Devuelve (avisos, cambios_precio)."""
    if cot["estado"] != "Vigente":
        raise ValueError("Solo se pueden editar cotizaciones vigentes.")
    actuales = items_de(sb, cot["id"])
    restantes = [i for i in actuales if i["id"] not in quitar_ids]
    if not restantes:
        raise ValueError("La cotización debe quedar con al menos un producto. Si ya no va, cancélala.")
    cambios_precio = []
    for it in restantes:
        f = filas.get(it["id"])
        if not f:
            continue
        if not permitir_precio:
            f["precio_unitario"] = it["precio_unitario"]          # sin la casilla el precio no cambia
        c = precios.cambio(None if it.get("es_sn") else it.get("modelo"), f.get("descripcion"), it["precio_unitario"],
                           it.get("descuento_pct"), f.get("precio_unitario"), f.get("descuento_pct"))
        if c:
            cambios_precio.append(c)
    if cambios_precio and not autorizo:
        raise ValueError("Hay precios o descuentos de más de 10% que necesitan autorización: marca la casilla y elige quién autorizó.")
    avisos = _liberar_items(sb, [i for i in actuales if i["id"] in quitar_ids])
    if quitar_ids:
        sb.table("cotizacion_items").delete().in_("id", list(quitar_ids)).execute()
    for it in restantes:
        f = filas.get(it["id"])
        if not f:
            continue
        precio = round(max(_num(f.get("precio_unitario")), 0.0), 2)
        dsc = min(max(_num(f.get("descuento_pct")), 0.0), 100.0)
        linea = calcular([{"precio_unitario": precio, "cantidad": int(it["cantidad"]), "descuento_pct": dsc,
                           "costo_unitario": 0.0}], 0.0, False, 0.0)["lineas"][0]
        sb.table("cotizacion_items").update({
            "descripcion": _nn(f.get("descripcion")), "medidas": _nn(f.get("medidas")),
            "precio_unitario": precio, "descuento_pct": dsc, "total_linea": linea}).eq("id", it["id"]).execute()
    return avisos, cambios_precio


def agregar_a_cotizacion(sb, cot, it, foto_bytes=None):
    """Agrega un renglón a una cotización vigente (aparta el producto). Devuelve avisos. Lanza ValueError."""
    if cot["estado"] != "Vigente":
        raise ValueError("Solo se pueden editar cotizaciones vigentes.")
    actuales = items_de(sb, cot["id"])
    if (not it["es_sn"]) and any(a.get("modelo") == it["modelo"] for a in actuales):
        raise ValueError(f"El modelo {it['modelo']} ya está en la cotización.")
    calc = calcular([it], 0.0, False, 0.0)
    foto = it.get("foto_url")
    if foto_bytes:
        foto = _subir_foto(sb, cot["codigo"], len(actuales), _foto_a_jpeg(foto_bytes))
    fila = sb.table("cotizacion_items").insert({
        "cotizacion_id": cot["id"], "orden": len(actuales), "modelo": None if it["es_sn"] else it["modelo"],
        "es_sn": bool(it["es_sn"]), "descripcion": it["descripcion"], "medidas": _nn(it.get("medidas")),
        "cantidad": int(it["cantidad"]), "precio_unitario": it["precio_unitario"],
        "descuento_pct": _num(it.get("descuento_pct")), "total_linea": calc["lineas"][0],
        "costo_unitario": it.get("costo_unitario", 0.0), "utilidad_bruta_inv": it.get("utilidad_bruta_inv", 0.0),
        "foto_url": foto}).execute().data[0]
    avisos = []
    if not it["es_sn"] and int(_num(cot.get("vigencia_dias"), 1)) > 0:     # vigencia 0: no aparta
        try:
            _, aviso = _apartar_si_se_puede(sb, fila["id"], it["modelo"], it["cantidad"], cot["id"])
        except ValueError:
            sb.table("cotizacion_items").delete().eq("id", fila["id"]).execute()
            raise
        if aviso:
            avisos.append(aviso)
    return avisos


def pdf_de_cotizacion(sb, cot_id):
    from cotizacion_pdf import generar_pdf_cotizacion
    cot = sb.table("cotizaciones").select("*").eq("id", cot_id).execute().data[0]
    try:
        items = revisar_bajas(sb, {cot["id"]: cot}, items_de(sb, cot_id))
        return generar_pdf_cotizacion(cot, vigentes(items))          # lo vendido en otro lado no va
    finally:
        try:                                   # devolver al sistema la memoria de las fotos
            from segundo_plano import liberar_memoria
            liberar_memoria()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Pantalla: NUEVA COTIZACIÓN
# ----------------------------------------------------------------------
def _reset_nueva():
    ss = st.session_state
    ss["ct_ver"] = ss.get("ct_ver", 0) + 1
    ss["ct_items"] = []


def _agregar_modelo_form(sb, items, ver, clave, avisar_reservado=True):
    """Formulario «MODELO + CANT.». Agrega a `items`. Devuelve True si se agregó algo."""
    with st.form(f"ct_form_modelo_{clave}_{ver}", clear_on_submit=True):
        m1, m2, m3 = st.columns([3, 1, 1])
        modelo_txt = m1.text_input("MODELO")
        cant = m2.number_input("CANT.", min_value=1, value=1, step=1)
        m3.write("")
        m3.write("")
        ok = m3.form_submit_button("Agregar", use_container_width=True)
    if not ok:
        return False
    m = modelo_txt.strip()
    if m.endswith(".0"):
        m = m[:-2]
    if not m:
        st.warning("Escribe un número de modelo.")
        return False
    if any((not i["es_sn"]) and i["modelo"] == m for i in items):
        st.warning(f"El modelo {m} ya está en la cotización.")
        return False
    try:
        p = info_producto(sb, m)
    except Exception as ex:
        st.error(f"Error consultando inventario: {ex}")
        return False
    if p is None:
        st.error(f"El modelo {m} no existe en el inventario. Si no tiene número, usa «Producto sin número (S/N)».")
        return False
    if p["disp"] == "v":
        st.error(f"El modelo {m} figura como VENDIDO: no se puede cotizar.")
        return False
    if p["disp"] == "r":
        otra = cotizacion_que_aparta(sb, m)
        if not otra:                                # reservado en una remisión abierta: no se puede
            st.error(f"{texto_reservado(sb, m)}.")
            return False
        st.warning(texto_en_cotizacion(otra, m) + ". Se agregó de todos modos, sin apartar: si esa cotización vence "
                   "o se cancela, la reserva pasa a ésta; si se vende, aquí queda dado de baja.")
    if cant > max(p["stock"], 1):
        st.error(f"El modelo {m} solo tiene {max(p['stock'], 1):g} unidad(es) disponible(s).")
        return False
    if p["precio"] <= 0:
        st.warning(f"El modelo {m} no tiene REDONDEO SHOWROOM: captura el precio en la lista (solo cambia en esta cotización).")
    existe = _foto_existe(p["foto_url"])
    items.append({
        "id": uuid.uuid4().hex[:8], "es_sn": False, "modelo": p["modelo"], "descripcion": p["nombre"] or "Sin nombre",
        "medidas": p["medidas"], "cantidad": int(cant), "precio_unitario": p["precio"],
        "precio_lista": p["precio"], "desc_lista": 0.0,
        "descuento_pct": 0.0, "costo_unitario": p["costo"], "utilidad_bruta_inv": p["utilidad_bruta"],
        "foto_url": p["foto_url"] if existe else None, "foto_inv": existe,
    })
    return True


def _agregar_sn_form(items, ver, clave):
    with st.expander("Producto sin número de modelo (S/N)"):
        with st.form(f"ct_form_sn_{clave}_{ver}", clear_on_submit=True):
            s1, s2 = st.columns(2)
            nombre = s1.text_input("NOMBRE *")
            medidas = s2.text_input("MEDIDAS")
            s3, s4 = st.columns(2)
            precio = s3.number_input("PRECIO ($) *", min_value=0.0, step=50.0, format="%.2f")
            cant = s4.number_input("CANT.", min_value=1, value=1, step=1)
            st.caption("El precio de un S/N necesita autorización de Viriato o Ernesto al guardar.")
            ok = st.form_submit_button("Agregar S/N")
    if not ok:
        return False
    if not nombre.strip() or precio <= 0:
        st.warning("El S/N necesita nombre y precio.")
        return False
    items.append({"id": uuid.uuid4().hex[:8], "es_sn": True, "modelo": None, "descripcion": nombre.strip(),
                  "medidas": medidas.strip(), "cantidad": int(cant), "precio_unitario": float(precio),
                  "descuento_pct": 0.0, "costo_unitario": 0.0, "utilidad_bruta_inv": 0.0,
                  "foto_url": None, "foto_inv": False})
    return True


def _mostrar_ultima(prefijo="ct"):
    ss = st.session_state
    ult = ss.get("ct_ultimo")
    if not ult:
        return
    st.success(ult["msg"])
    for a in ult.get("avisos", []):
        st.warning(a)
    c1, c2 = st.columns([2, 1])
    if ult.get("pdf"):
        descargar(c1, f"Descargar cotización {ult['codigo']} (PDF)", ult["pdf"],
                           file_name=f"cotizacion_{ult['codigo']}.pdf", mime="application/pdf",
                           type="primary", key=f"ct_dl_{ult['codigo']}")
    if c2.button("Ocultar", key=f"ct_hide_{ult['codigo']}"):
        ss.pop("ct_ultimo", None)
        st.rerun()


def tab_nueva(sb, user):
    ss = st.session_state
    ss.setdefault("ct_ver", 0)
    ss.setdefault("ct_items", [])
    ver = ss["ct_ver"]
    items = ss["ct_items"]

    def k(n):
        return f"ct{ver}_{n}"

    _mostrar_ultima()
    st.caption("N° de pedido: **PENDIENTE** hasta que la cotización se pase a pedido. Los productos quedan apartados "
               "mientras esté vigente.")

    c1, c2 = st.columns([3, 1])
    cliente = c1.text_input("CLIENTE *", key=k("cliente"))
    fecha = c2.date_input("FECHA", value=hoy_mx(), key=k("fecha"), format="DD/MM/YYYY")
    c3, c4, c5 = st.columns(3)
    vendedores = c3.multiselect("VENDEDOR/ES *", catalogo(sb, "vendedor"), key=k("vend"),
                                placeholder="Elegir uno o varios…")
    telefono = c4.text_input("TELÉFONO", key=k("tel"))
    email = c5.text_input("E-MAIL", key=k("email"))
    v1, v2 = st.columns([1, 3])
    dias = v1.number_input("VIGENCIA (días) *", min_value=0, max_value=60, value=DIAS_VIGENCIA, step=1, key=k("dias"),
                           help="Los productos quedan apartados hasta esta fecha; después vuelven a estar disponibles. "
                                "Con 0 no se aparta nada: solo se hace la cotización.")
    vence = hoy_mx() + dt.timedelta(days=int(dias))
    if int(dias) == 0:
        v2.caption("**Vigencia 0: no se aparta ningún producto.** Solo se genera la cotización; si después se pasa a "
                   "pedido, se revisa que los modelos sigan disponibles.")
    else:
        v2.caption(f"Se aparta hasta el **{vence.strftime('%d/%m/%Y')}** (inclusive).")

    st.divider()
    st.subheader("Productos")
    if _agregar_modelo_form(sb, items, ver, "n"):
        st.rerun()
    if _agregar_sn_form(items, ver, "n"):
        st.rerun()

    quitar = None
    precio_hab = precios.habilitado(k("precio"))
    for it in items:
        with st.container(border=True):
            etiqueta = "S/N" if it["es_sn"] else f"Modelo {it['modelo']}"
            top1, top2 = st.columns([5, 1])
            top1.markdown(f"**{etiqueta}**")
            if top2.button("Quitar", key=k(f"del_{it['id']}")):
                quitar = it["id"]
            a1, a2 = st.columns(2)
            it["descripcion"] = a1.text_input("Nombre", value=it["descripcion"], key=k(f"nom_{it['id']}"))
            it["medidas"] = a2.text_input("Medidas", value=it.get("medidas") or "", key=k(f"med_{it['id']}"))
            b1, b2, b3, b4 = st.columns([1, 2, 1, 2])
            it["cantidad"] = b1.number_input("Cant.", min_value=1, value=int(it["cantidad"]), step=1,
                                             key=k(f"cant_{it['id']}"))
            if not precio_hab:
                # Precio bloqueado (un S/N conserva el capturado; al guardar pide autorización). Descuento libre hasta 10%.
                if _num(it.get("precio_lista")) > 0:
                    it["precio_unitario"] = float(it["precio_lista"])
                b2.number_input("Precio unit. ($) 🔒", value=float(it["precio_unitario"]), format="%.2f", disabled=True,
                                key=k(f"precio_b_{it['id']}"))
                tope = max(precios.DESC_LIBRE, _num(it.get("desc_lista")))
                it["descuento_pct"] = b3.number_input(f"Desc. % (hasta {tope:g})", min_value=0.0, max_value=float(tope),
                                                      value=float(min(_num(it.get("descuento_pct")), tope)), step=5.0,
                                                      format="%.1f", key=k(f"dsc_{it['id']}"))
            else:
                it["precio_unitario"] = b2.number_input("Precio unit. ($)", min_value=0.0,
                                                        value=float(it["precio_unitario"]), step=50.0, format="%.2f",
                                                        key=k(f"precio_{it['id']}"))
                it["descuento_pct"] = b3.number_input("Desc. %", min_value=0.0, max_value=100.0,
                                                      value=float(it.get("descuento_pct") or 0.0), step=5.0,
                                                      format="%.1f", key=k(f"dsc_h_{it['id']}"))
            b4.metric("Total", f"${calcular([it], 0.0, False, 0.0)['total']:,.2f}")
            subida = st.file_uploader(
                "Foto para esta cotización" + (" (opcional: ya hay una en el inventario)" if it.get("foto_inv")
                                                  else " (este modelo no tiene foto en el inventario)"),
                type=["jpg", "jpeg", "png", "webp"], key=k(f"foto_{it['id']}"),
                help="Solo se usa en esta cotización; no cambia la foto del inventario.")
            it["_foto"] = subida.getvalue() if subida is not None else None
    if quitar:
        ss["ct_items"] = [i for i in items if i["id"] != quitar]
        st.rerun()

    precio_hab, precio_aut = precios.control(k("precio")) if items else (False, None)
    calc = calcular(items, 0.0, False, 0.0)
    if items:
        t1, t2, t3 = st.columns(3)
        t1.metric("PIEZAS", sum(int(i["cantidad"]) for i in items))
        t2.metric("IMPORTE", f"${calc['bruto']:,.2f}")
        t3.metric("TOTAL", f"${calc['total']:,.2f}")
    notas = st.text_area("NOTAS (internas)", key=k("notas"), height=60)

    if vendedores:
        _, cod_resp = codigos.pedir_identidad(sb, k("resp"), fijo=vendedores[0])
    else:
        cod_resp = ""
        st.caption("Elige al menos un vendedor: el primero es el responsable y escribe su código para guardar.")
    if st.button("Guardar cotización" + (" (sin apartar)" if int(dias) == 0 else " y apartar productos"),
                 type="primary", use_container_width=True, key=k("guardar")):
        errores = []
        if not cliente.strip():
            errores.append("Falta el cliente.")
        if not vendedores:
            errores.append("Elige al menos un vendedor.")
        if not items:
            errores.append("Agrega al menos un producto.")
        if any(i["precio_unitario"] <= 0 for i in items):
            errores.append("Hay productos sin precio. Captura el precio de cada uno.")
        cambios_precio = precios.cambios_de_items(items)
        if cambios_precio and not precio_aut:
            errores.append("Hay precios o descuentos de más de 10% que necesitan autorización: marca la casilla y elige quién autorizó.")
        if errores:
            for e in errores:
                st.error("" + e)
            return
        responsable = codigos.confirmar(sb, vendedores[0], cod_resp)
        if not responsable:
            return
        cab = {"cliente": cliente.strip(), "vendedor": " / ".join(vendedores), "telefono": telefono, "email": email,
               "fecha": fecha, "vigencia_dias": int(dias), "notas": notas}
        with st.spinner("Guardando y apartando productos…"):
            try:
                cot_id, codigo, avisos = crear_cotizacion(sb, cab, items, responsable)
            except ValueError as ex:
                st.error(f"{ex}")
                return
            except Exception as ex:
                st.error(f"No se pudo guardar la cotización: {ex}")
                return
            codigos.registrar(sb, responsable, "Cotización creada", "cotizacion", codigo,
                              f"{cliente.strip()} · total ${calc['total']:,.2f}"
                              + (" · sin apartar (vigencia 0)" if int(dias) == 0 else ""))
            a = precios.registrar(sb, f"Cotización {codigo}", cambios_precio, precio_aut, responsable)
            if a:
                avisos.append(a)
            pdf = None
            try:
                pdf = pdf_de_cotizacion(sb, cot_id)
            except Exception as ex:
                avisos.append(f"La cotización se guardó, pero no se pudo generar el PDF ({ex}). "
                              "Puedes descargarlo desde «Guardadas».")
        ss["ct_ultimo"] = {"codigo": codigo, "pdf": pdf, "avisos": avisos,
                           "msg": f"Cotización {codigo} guardada. " + (
                               "No se apartó ningún producto (vigencia 0)." if int(dias) == 0
                               else f"Productos apartados hasta el {vence.strftime('%d/%m/%Y')}.")}
        _reset_nueva()
        st.rerun()


# ----------------------------------------------------------------------
# Pantalla: COTIZACIONES GUARDADAS
# ----------------------------------------------------------------------
def _restan(vence, estado):
    if estado != "Vigente":
        return ""
    d = (_fecha(vence) - hoy_mx()).days
    return "vence HOY" if d == 0 else (f"{d} día(s)" if d > 0 else "vencida")


def tab_guardadas(sb, user):
    ss = st.session_state
    f1, f2 = st.columns([1, 3])
    filtro = f1.selectbox("Mostrar", ["Vigentes", "Todas", "Vencidas", "Convertidas", "Canceladas"], key="ctg_filtro")
    estados = {"Vigentes": ["Vigente"], "Vencidas": ["Vencida"], "Convertidas": ["Convertida"],
               "Canceladas": ["Cancelada"], "Todas": None}[filtro]
    buscar = f2.text_input("Buscar (cliente, vendedor o código)", key="ctg_buscar")
    cots = listar_cotizaciones(sb, estados)
    if buscar.strip():
        b = buscar.strip().lower()
        cots = [c for c in cots if b in " ".join(str(c.get(x) or "") for x in ("cliente", "vendedor", "codigo")).lower()]
    if not cots:
        st.info("No hay cotizaciones con ese filtro.")
        return
    tot = _totales_lista(sb, tuple(c["id"] for c in cots), tuple((c["id"], c["estado"]) for c in cots))
    df = pd.DataFrame([{
        "Código": c["codigo"], "Cliente": c["cliente"], "Vendedor": c["vendedor"],
        "Fecha": _fecha(c["fecha"]).strftime("%d/%m/%Y"), "Vence": _fecha(c["vence"]).strftime("%d/%m/%Y"),
        "Restan": _restan(c["vence"], c["estado"]), "Estado": c["estado"],
        "Piezas": tot.get(c["id"], {}).get("piezas", 0), "Total": tot.get(c["id"], {}).get("total", 0.0),
        "N° pedido": c.get("numero_pedido") or ("PENDIENTE" if c["estado"] == "Vigente" else ""),
        "Avisos": (f"⚠ {tot[c['id']]['bajas']} vendido(s)" if tot.get(c["id"], {}).get("bajas") else ""),
    } for c in cots])
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config={"Total": st.column_config.NumberColumn(format="$%.2f")})

    sel = st.selectbox("Abrir cotización", [c["codigo"] for c in cots], key="ctg_sel")
    cot = next(c for c in cots if c["codigo"] == sel)
    cid = cot["id"]
    items = revisar_bajas(sb, {cid: cot}, items_de(sb, cid))
    st.markdown(f"### {cot['codigo']} — {cot['cliente']}  · {cot['estado']}")
    bajas = [i for i in items if i.get("baja")]
    if bajas:
        st.warning(f"⚠ {len(bajas)} producto(s) de esta cotización ya se vendieron: "
                   + "; ".join(f"Modelo {i['modelo']} ({i['baja']})" for i in bajas)
                   + ". Quedan dados de baja: fuera del total y del PDF.")
    st.caption(f"Vendedor: {cot['vendedor']} · Tel: {cot.get('telefono') or '—'} · E-mail: {cot.get('email') or '—'} · "
               f"Fecha: {_fecha(cot['fecha']).strftime('%d/%m/%Y')} · Válida hasta: {_fecha(cot['vence']).strftime('%d/%m/%Y')}"
               + (f" · Pedido N° {cot['numero_pedido']}" if cot.get("numero_pedido") else ""))
    if cot.get("notas"):
        st.caption(f"Notas: {cot['notas']}")
    def _estado_reserva(i):
        if i.get("baja"):
            return f"❌ {i['baja']}"
        if i.get("apartado"):
            return "sí"
        if i.get("modelo") and cot["estado"] == "Vigente" and int(_num(cot.get("vigencia_dias"), 1)) > 0:
            otra = cotizacion_que_aparta(sb, i["modelo"], excluir_cot=cid)
            return f"no · apartado en {otra['codigo']}" if otra else "no"
        return ""
    dfi = pd.DataFrame([{"Modelo": i.get("modelo") or "S/N", "Nombre": i.get("descripcion"), "Medidas": i.get("medidas"),
                         "Cant.": i["cantidad"], "Precio": _num(i["precio_unitario"]),
                         "Desc. %": _num(i.get("descuento_pct")), "Total": 0.0 if i.get("baja") else _num(i["total_linea"]),
                         "Apartado": _estado_reserva(i)} for i in items])
    st.dataframe(dfi, use_container_width=True, hide_index=True,
                 column_config={"Precio": st.column_config.NumberColumn(format="$%.2f"),
                                "Total": st.column_config.NumberColumn(format="$%.2f")})
    st.markdown(f"**Total: ${sum(_num(i['total_linea']) for i in vigentes(items)):,.2f}**")

    # ---- PDF ----
    clave_pdf = f"ctg_pdf_{cid}"
    p1, p2 = st.columns(2)
    if p1.button("Preparar PDF para enviar", key=f"ctg_prep_{cid}"):
        with st.spinner("Armando el PDF (descarga las fotos)…"):
            try:
                for k in [k for k in ss.keys() if str(k).startswith("ctg_pdf_")]:
                    ss.pop(k, None)          # solo un PDF de cotización en memoria a la vez
                ss[clave_pdf] = pdf_de_cotizacion(sb, cid)
            except Exception as ex:
                st.error(f"No se pudo generar el PDF: {ex}")
    if ss.get(clave_pdf):
        descargar(p2, "Descargar PDF", ss[clave_pdf], file_name=f"cotizacion_{cot['codigo']}.pdf",
                           mime="application/pdf", type="primary", key=f"ctg_dl_{cid}")

    avisos_msg = ss.pop("ctg_avisos", None)
    if avisos_msg:
        for a in avisos_msg:
            st.warning(a)
    if ss.get("ctg_msg"):
        st.success(ss.pop("ctg_msg"))

    # ---- Acciones ----
    q_v, q_c = ("", "")
    if cot["estado"] in ("Vigente", "Vencida"):
        st.markdown("**Para renovar o editar, identifícate:**")
        q_v, q_c = codigos.pedir_identidad(sb, f"ctg_id_{cid}")
    if cot["estado"] == "Vigente" and int(_num(cot.get("vigencia_dias"), 1)) == 0:
        st.info("Cotización **sin apartar** (vigencia 0): los productos siguen disponibles para cualquiera. "
                "«Renovar» la convierte en una cotización normal y aparta los productos.")
    if cot["estado"] == "Vigente":
        a1, a2 = st.columns(2)
        if a1.button("Pasar a pedido", type="primary", use_container_width=True, key=f"ctg_conv_{cid}"):
            iniciar_conversion(sb, cot, vigentes(items))          # lo vendido en otro lado no pasa
            st.rerun()
        with a2:
            r1, r2 = st.columns([1, 1])
            dias = r1.number_input("Renovar (días desde hoy)", min_value=1, max_value=60, value=DIAS_VIGENCIA,
                                   key=f"ctg_dias_{cid}")
            r2.write("")
            r2.write("")
            if r2.button("Renovar", key=f"ctg_ren_{cid}") and (quien := codigos.confirmar(sb, q_v, q_c)):
                try:
                    ss["ctg_avisos"] = renovar_cotizacion(sb, cot, dias)
                    codigos.registrar(sb, quien, "Cotización renovada", "cotizacion", cot["codigo"], f"{int(dias)} día(s)")
                    ss["ctg_msg"] = f"Cotización {cot['codigo']} renovada {int(dias)} día(s) desde hoy."
                    ss.pop(clave_pdf, None)
                    st.rerun()
                except Exception as ex:
                    st.error(f"{ex}")
        with st.expander("Editar productos (precio, descuento, medidas, nombre, quitar o agregar)"):
            _editor_items(sb, cot, items, clave_pdf, q_v, q_c)
        with st.expander("Cancelar cotización"):
            st.caption("Los productos apartados vuelven a estar disponibles.")
            if st.checkbox("Sí, cancelar esta cotización", key=f"ctg_cc_{cid}"):
                c_v, c_c = codigos.pedir_identidad(sb, f"ctg_idc_{cid}")      # el código, junto al botón
                if st.button("Cancelar cotización", key=f"ctg_cancel_{cid}") and (quien := codigos.confirmar(sb, c_v, c_c)):
                    try:
                        ss["ctg_avisos"] = cancelar_cotizacion(sb, cot)
                        codigos.registrar(sb, quien, "Cotización cancelada", "cotizacion", cot["codigo"])
                        ss["ctg_msg"] = f"Cotización {cot['codigo']} cancelada; productos liberados."
                        ss.pop(clave_pdf, None)
                        st.rerun()
                    except Exception as ex:
                        st.error(f"{ex}")
    elif cot["estado"] == "Vencida":
        st.info("Esta cotización venció y sus productos se liberaron. Puedes reactivarla si siguen disponibles.")
        d1, d2 = st.columns([1, 2])
        dias = d1.number_input("Vigencia (días desde hoy)", min_value=1, max_value=60, value=DIAS_VIGENCIA,
                               key=f"ctg_dias_{cid}")
        d2.write("")
        d2.write("")
        if d2.button("Reactivar y volver a apartar", key=f"ctg_react_{cid}") and (quien := codigos.confirmar(sb, q_v, q_c)):
            try:
                ss["ctg_avisos"] = renovar_cotizacion(sb, cot, dias)
                codigos.registrar(sb, quien, "Cotización reactivada", "cotizacion", cot["codigo"])
                ss["ctg_msg"] = f"Cotización {cot['codigo']} reactivada."
                ss.pop(clave_pdf, None)
                st.rerun()
            except Exception as ex:
                st.error(f"{ex}")


def _editor_items(sb, cot, items, clave_pdf, q_v="", q_c=""):
    ss = st.session_state
    cid = cot["id"]
    st.caption("Los cambios aplican solo a esta cotización (no tocan el inventario). La cantidad no se edita: "
               "quita el renglón y agrégalo de nuevo con otra cantidad.")
    base = pd.DataFrame([{"id": i["id"], "modelo": i.get("modelo") or "S/N", "descripcion": i.get("descripcion") or "",
                          "medidas": i.get("medidas") or "", "cantidad": i["cantidad"],
                          "precio_unitario": _num(i["precio_unitario"]), "descuento_pct": _num(i.get("descuento_pct")),
                          "quitar": False} for i in items])
    clave_precio = f"ctg_ed_p_{cid}"
    ed_hab = precios.habilitado(clave_precio)
    ed = st.data_editor(
        base, hide_index=True, use_container_width=True, key=f"ctg_ed_{cid}",
        disabled=["id", "modelo", "cantidad"] + ([] if ed_hab else ["precio_unitario"]),
        column_config={"id": None, "modelo": st.column_config.TextColumn("MODELO"),
                       "descripcion": st.column_config.TextColumn("NOMBRE"),
                       "medidas": st.column_config.TextColumn("MEDIDAS"),
                       "cantidad": st.column_config.NumberColumn("CANT."),
                       "precio_unitario": st.column_config.NumberColumn("PRECIO UNIT.", min_value=0.0, format="$%.2f"),
                       "descuento_pct": st.column_config.NumberColumn("DESC. %", min_value=0.0, max_value=100.0, format="%.1f"),
                       "quitar": st.column_config.CheckboxColumn("QUITAR")})
    if not ed_hab:
        st.caption("🔒 Precio bloqueado; descuento libre hasta 10%. Para cambiar un precio, dar más descuento o "
                   "agregar un S/N, marca la casilla de abajo.")
    ed_hab, ed_aut = precios.control(clave_precio)
    st.caption("Usa el código de arriba («Identifícate») para guardar estos cambios.")
    if st.button("Guardar cambios", key=f"ctg_ed_guardar_{cid}") and (quien := codigos.confirmar(sb, q_v, q_c)):
        filas = {int(r["id"]): r for r in ed.to_dict("records")}
        quitar = [i for i, r in filas.items() if r.get("quitar")]
        try:
            ss["ctg_avisos"], cambios_p = guardar_edicion(sb, cot, filas, quitar, ed_hab, ed_aut)
            a = precios.registrar(sb, f"Cotización {cot['codigo']}", cambios_p, ed_aut, quien)
            if a:
                ss["ctg_avisos"] = list(ss["ctg_avisos"]) + [a]
            codigos.registrar(sb, quien, "Editó cotización", "cotizacion", cot["codigo"],
                              f"{len(quitar)} producto(s) quitado(s)" if quitar else "precios/descuentos/medidas")
            ss["ctg_msg"] = "Cotización actualizada."
            ss.pop(clave_pdf, None)
            st.rerun()
        except Exception as ex:
            st.error(f"{ex}")

    # ---- Foto de un producto (agregar o cambiar; solo para esta cotización) ----
    st.markdown("**Foto de un producto**")
    con_modelo = [i for i in items if not i.get("baja")]
    if con_modelo:
        etiquetas = {i["id"]: f"{i.get('modelo') or 'S/N'} — {i.get('descripcion') or ''}"
                              + ("" if i.get("foto_url") else "  · ⚠ sin foto") for i in con_modelo}
        f1, f2 = st.columns([1, 2])
        sel_f = f1.selectbox("Producto", list(etiquetas), format_func=etiquetas.get, key=f"ctg_fsel_{cid}")
        it_f = next(i for i in con_modelo if i["id"] == sel_f)
        if it_f.get("foto_url"):
            f1.image(it_f["foto_url"], width=130, caption="Foto actual")
        else:
            f1.caption("Este producto no tiene foto en la cotización.")
        vf = ss.get(f"ctg_fver_{cid}", 0)
        sub = f2.file_uploader("Nueva foto (solo para esta cotización; no cambia el inventario)",
                               type=["jpg", "jpeg", "png", "webp"], key=f"ctg_fup_{cid}_{vf}")
        if sub is not None:
            mini = miniatura(sub.getvalue())
            if mini:
                f2.image(mini, width=130, caption="Nueva")
        if f2.button("Guardar foto", key=f"ctg_fok_{cid}", disabled=sub is None) \
                and (quien_f := codigos.confirmar(sb, q_v, q_c)):
            try:
                url = _subir_foto(sb, cot["codigo"], f"e{it_f['id']}", _foto_a_jpeg(sub.getvalue()))
                sb.table("cotizacion_items").update({"foto_url": url}).eq("id", it_f["id"]).execute()
                codigos.registrar(sb, quien_f, "Cambió foto de cotización", "cotizacion", cot["codigo"],
                                  it_f.get("modelo") or it_f.get("descripcion") or "")
                ss[f"ctg_fver_{cid}"] = vf + 1                 # vacía el campo de la foto
                ss.pop(clave_pdf, None)                        # el PDF anterior ya no sirve
                ss["ctg_msg"] = "Foto guardada. Vuelve a preparar el PDF para verla."
                st.rerun()
            except Exception as ex:
                st.error(f"No se pudo guardar la foto: {ex}")

    st.markdown("**Agregar un producto**")
    ver = f"e{cid}"
    nuevos = ss.setdefault(f"ctg_nuevos_{cid}", [])
    pend_k = f"ctg_sinprecio_{cid}"
    if _agregar_modelo_form(sb, nuevos, ver, "e"):
        it = nuevos.pop()
        if it["precio_unitario"] <= 0:
            ss[pend_k] = it                 # sin precio en el inventario: se captura abajo, con autorización
            st.rerun()
        elif not (quien := codigos.confirmar(sb, q_v, q_c)):
            pass
        else:
            try:
                av = agregar_a_cotizacion(sb, cot, it)
                codigos.registrar(sb, quien, "Agregó producto a cotización", "cotizacion", cot["codigo"], it["modelo"])
                ss["ctg_avisos"] = av
                ss["ctg_msg"] = f"Modelo {it['modelo']} agregado" + (
                    " (sin apartar: vigencia 0)." if int(_num(cot.get("vigencia_dias"), 1)) == 0 else " y apartado.")
                ss.pop(clave_pdf, None)
                st.rerun()
            except Exception as ex:
                st.error(f"{ex}")
    # Modelo sin precio en el inventario: se le pone precio aquí, con autorización de Viriato o Ernesto
    pend = ss.get(pend_k)
    if pend:
        with st.container(border=True):
            st.markdown(f"**Modelo {pend['modelo']} — {pend.get('descripcion') or ''}**")
            st.caption("No tiene REDONDEO SHOWROOM en el inventario. Captura el precio para esta cotización; "
                       "necesita autorización y les llega el aviso de cambio de precio.")
            p1, p2 = st.columns([1, 2])
            precio_n = p1.number_input("Precio ($)", min_value=0.0, step=50.0, format="%.2f", key=f"{pend_k}_precio")
            aut_n = p2.radio("¿Quién autorizó el precio? *", precios.AUTORIZAN, index=None, horizontal=True,
                             key=f"{pend_k}_aut")
            b1, b2 = st.columns([1, 1])
            if b1.button("Agregar con este precio", type="primary", key=f"{pend_k}_ok"):
                if precio_n <= 0:
                    st.error("Escribe el precio.")
                elif not aut_n:
                    st.error("Elige quién autorizó el precio.")
                elif (quien := codigos.confirmar(sb, q_v, q_c)):
                    it = dict(pend, precio_unitario=float(precio_n))
                    try:
                        av = agregar_a_cotizacion(sb, cot, it)
                        a = precios.registrar(sb, f"Cotización {cot['codigo']}",
                                              [precios.cambio(it["modelo"], it.get("descripcion"), 0, 0, precio_n, 0)],
                                              aut_n, quien)
                        codigos.registrar(sb, quien, "Agregó producto a cotización", "cotizacion", cot["codigo"],
                                          f"{it['modelo']} · precio ${precio_n:,.2f} autorizado por {aut_n}")
                        ss["ctg_avisos"] = list(av) + ([a] if a else [])
                        ss["ctg_msg"] = f"Modelo {it['modelo']} agregado con precio ${precio_n:,.2f}."
                        for x in (pend_k, f"{pend_k}_precio", f"{pend_k}_aut"):
                            ss.pop(x, None)
                        ss.pop(clave_pdf, None)
                        st.rerun()
                    except Exception as ex:
                        st.error(f"{ex}")
            if b2.button("Cancelar", key=f"{pend_k}_no"):
                for x in (pend_k, f"{pend_k}_precio", f"{pend_k}_aut"):
                    ss.pop(x, None)
                st.rerun()

    if _agregar_sn_form(nuevos, ver, "e"):
        it = nuevos.pop()
        aut = precios.autorizo_de(f"ctg_ed_p_{cid}")
        quien = None
        if not aut:
            st.error("El precio de un S/N necesita autorización: marca arriba la casilla «Cambiar precio…», elige quién "
                     "autorizó y vuelve a agregarlo.")
        else:
            quien = codigos.confirmar(sb, q_v, q_c)
        if quien:
            try:
                agregar_a_cotizacion(sb, cot, it)
                codigos.registrar(sb, quien, "Agregó producto S/N a cotización", "cotizacion", cot["codigo"], it["descripcion"])
                a = precios.registrar(sb, f"Cotización {cot['codigo']}",
                                      [precios.cambio(None, it["descripcion"], 0, 0, it["precio_unitario"], 0)], aut, quien)
                if a:
                    ss["ctg_avisos"] = [a]
                ss["ctg_msg"] = "Producto S/N agregado."
                ss.pop(clave_pdf, None)
                st.rerun()
            except Exception as ex:
                st.error(f"{ex}")


# ----------------------------------------------------------------------
# PESTAÑA
# ----------------------------------------------------------------------
def tab_cotizaciones(sb, user):
    ss = st.session_state
    # Al abrir la pestaña también se revisan los vencimientos (la página espera solo si hay algo que liberar)
    try:
        res = vencer_cotizaciones(sb)
        if res.get("ok") and res.get("vencidas"):
            st.info(f"{res['vencidas']} cotización(es) vencieron y sus productos volvieron a estar disponibles.")
            for a in res.get("avisos", []):
                st.warning(a)
        elif not res.get("ok"):
            st.error(f"No se pudieron revisar las cotizaciones. ¿Ya corriste cotizaciones_schema.sql en Supabase? "
                     f"Detalle: {res.get('error')}")
            return
    except Exception as ex:
        st.error(f"No se pudieron revisar las cotizaciones: {ex}")
        return

    if ss.get("vt_desde_cot"):
        tab_nuevo_pedido(sb, user, desde_cot=True)           # el mismo formulario de Nuevo pedido, ya lleno
        return
    _mostrar_ultimo("cot_top")
    from navegacion import selector
    if selector(["Nueva cotización", "Guardadas"], "nav_cot", "Cotizaciones") == "Nueva cotización":
        tab_nueva(sb, user)
    else:
        tab_guardadas(sb, user)