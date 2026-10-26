"""
Diferencias de disponibilidad entre el inventario y Shopify (con aviso, nada se cambia solo).

  * Cada 10 minutos, en segundo plano, compara el stock de Shopify con la disponibilidad del inventario.
  * Dos casos:
      - «Shopify agotado»:  Shopify en 0 y el inventario dice DISPONIBLE  (¿se vendió? ¿se apagó a mano?)
      - «Shopify a la venta»: Shopify en 1 o más y el inventario dice VENDIDO o RESERVADO  (riesgo de doble venta)
  * aviso arriba de la app (roles con permiso «shopify_botones») y lista para resolver cada pieza con un clic,
    en la pestaña «Shopify».

Solo productos únicos (CANTIDAD ≤ 1 en el inventario) y que no estén archivados en Shopify.
"""
import datetime as dt

import streamlit as st

import codigos
from segundo_plano import TareaPeriodica
from sync_shopify import _num, _parse

INTERVALO_SEG = 600
MAX_FILAS = 60


def _estado_inv(d):
    v = str(d.get("DISP", d.get("DISPONIBILIDAD", "")) or "").strip().lower()
    return v if v in ("v", "r") else ""


def calcular_diferencias(sb):
    from shopify_lista import _leer_shopify
    shop = [p for p in _leer_shopify() if p.get("estado") != "ARCHIVED"]
    por_sku = {p["sku"]: p for p in shop}
    skus = list(por_sku)
    inventario = {}
    for i in range(0, len(skus), 100):
        for f in (sb.table("productos").select("MODELO,datos_json").in_("MODELO", skus[i:i + 100]).execute().data or []):
            inventario[str(f["MODELO"]).strip()] = _parse(f.get("datos_json"))

    difs = []
    for sku, d in inventario.items():
        p = por_sku[sku]
        stock = p.get("stock")
        if stock is None or _num(d.get("CANTIDAD"), 1.0) > 1:
            continue
        est = _estado_inv(d)
        if est == "" and stock <= 0:
            tipo = "shopify_agotado"
        elif est in ("v", "r") and stock >= 1:
            tipo = "shopify_a_la_venta"
        else:
            continue
        difs.append({"sku": sku, "nombre": str(d.get("NOMBRE", "") or p.get("titulo") or ""), "tipo": tipo,
                     "inv": est, "stock": stock, "estado_shop": p.get("estado"), "id": p.get("id"), "venta": ""})

    # ¿Hubo una venta en Shopify de esa pieza? (ayuda a decidir quién tiene razón)
    if difs:
        try:
            ordenes = (sb.table("shopify_ordenes").select("nombre,modelos,cancelada")
                       .order("creado", desc=True).limit(1000).execute().data)
        except Exception:
            ordenes = []
        for x in difs:
            o = next((o for o in ordenes if x["sku"] in (o.get("modelos") or [])), None)
            if o:
                x["venta"] = f"Pedido de Shopify {o.get('nombre')}" + (" (cancelado)" if o.get("cancelada") else "")
    difs.sort(key=lambda x: (x["tipo"] != "shopify_a_la_venta", x["sku"]))
    return {"ok": True, "difs": difs, "hora": dt.datetime.now().strftime("%H:%M")}


_TAREA = TareaPeriodica(calcular_diferencias, INTERVALO_SEG)


def diferencias_en_segundo_plano(sb):
    """Último resultado al instante; cada 10 min lanza otra revisión sin hacer esperar la página."""
    return _TAREA.obtener(sb)


def pendientes(sb):
    """Diferencias sin resolver en esta sesión (para el aviso de arriba)."""
    r = diferencias_en_segundo_plano(sb)
    if not r.get("ok") or r.get("pendiente"):
        return []
    hechos = st.session_state.get("disp_resueltos", set())
    return [x for x in r.get("difs", []) if x["sku"] not in hechos]


def _resolver(sb, user, x, accion):
    """accion: 'inv_vendido' | 'inv_disponible' | 'shop_1' | 'shop_0'. Devuelve (ok, mensaje)."""
    from shopify_borrador import marcar_disponibilidad_shopify
    sku = x["sku"]
    if accion in ("inv_vendido", "inv_disponible"):
        from ventas import marcar_disponibilidad
        aviso = marcar_disponibilidad(sb, sku, accion == "inv_vendido")
        if aviso and ("no se encontró" in aviso or "cantidad" in aviso):
            return False, aviso
        texto = "marcado VENDIDO" if accion == "inv_vendido" else "marcado DISPONIBLE"
        msg = f"Modelo {sku}: {texto} en el inventario." + (f" {aviso}" if aviso else "")
    else:
        est, m = marcar_disponibilidad_shopify(sku, accion == "shop_0")
        if est != "ok":
            return False, f"Modelo {sku}: {m}"
        msg = f"Modelo {sku}: {m}"
    codigos.registrar(sb, user.get("nombre") or user.get("role"), "Corrigió disponibilidad Shopify/inventario",
                      "producto", sku, msg)
    return True, msg


def render_diferencias(sb, user):
    """Sección de la pestaña «Shopify» para resolver diferencias de disponibilidad."""
    ss = st.session_state
    ss.setdefault("disp_resueltos", set())
    for m in ss.pop("disp_msgs", []):
        (st.success if m[0] else st.error)(m[1])

    r = diferencias_en_segundo_plano(sb)
    c1, c2 = st.columns([4, 1])
    if c2.button("Revisar ahora", key="disp_ahora", use_container_width=True):
        with st.spinner("Comparando inventario y Shopify…"):
            _TAREA.ejecutar_ahora(sb)
        ss["disp_resueltos"] = set()
        st.rerun()
    if r.get("pendiente"):
        c1.caption("Comparando la disponibilidad del inventario con Shopify… recarga en unos segundos.")
        return
    if not r.get("ok"):
        c1.warning(f"No se pudo comparar con Shopify: {r.get('error')}")
        return
    difs = [x for x in r.get("difs", []) if x["sku"] not in ss["disp_resueltos"]]
    if not difs:
        c1.caption(f"✅ La disponibilidad coincide en el inventario y Shopify (revisado a las {r['hora']}).")
        return

    c1.subheader(f"⚠️ {len(difs)} pieza(s) con disponibilidad distinta")
    st.caption(f"Revisado a las {r['hora']}. Decide en cada pieza cuál de los dos tiene razón; nada se cambia solo.")
    try:
        from shopify_borrador import _secret
        tienda = _secret("SHOPIFY_SHOP").replace(".myshopify.com", "")
    except Exception:
        tienda = ""
    for x in difs[:MAX_FILAS]:
        with st.container(border=True):
            a, b, c = st.columns([3, 3, 3])
            a.markdown(f"**{x['sku']}** — {x['nombre'] or 'Sin nombre'}")
            if x["tipo"] == "shopify_agotado":
                b.markdown("Inventario: **DISPONIBLE** · Shopify: **0 (agotado)**")
            else:
                b.markdown(f"Inventario: **{'RESERVADO' if x['inv'] == 'r' else 'VENDIDO'}** · "
                           f"Shopify: **{int(x['stock'])} (a la venta)**")
            info = x["venta"] or "Sin pedidos de Shopify registrados de esta pieza."
            if tienda and x.get("id"):
                info += f" · [Abrir en Shopify](https://admin.shopify.com/store/{tienda}/products/{x['id'].split('/')[-1]})"
            a.caption(info)
            botones = []
            if x["tipo"] == "shopify_agotado":
                botones = [("Marcar VENDIDA en inventario", "inv_vendido"), ("Regresar a 1 en Shopify", "shop_1")]
            else:
                botones = [("Poner 0 en Shopify", "shop_0")]
                if x["inv"] == "v":
                    botones.append(("Marcar DISPONIBLE en inventario", "inv_disponible"))
                else:
                    b.caption("Está apartada por una cotización: en Shopify debe estar agotada.")
            for etiqueta, accion in botones:
                if c.button(etiqueta, key=f"disp_{accion}_{x['sku']}", use_container_width=True):
                    ok, msg = _resolver(sb, user, x, accion)
                    ss.setdefault("disp_msgs", []).append((ok, msg))
                    if ok:
                        ss["disp_resueltos"].add(x["sku"])
                        try:
                            from shopify_lista import _tabla
                            _tabla.clear()
                        except Exception:
                            pass
                    st.rerun()
    if len(difs) > MAX_FILAS:
        st.caption(f"Se muestran {MAX_FILAS} de {len(difs)}. Al resolver estas aparecerán las siguientes.")
