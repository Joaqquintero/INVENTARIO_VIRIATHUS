"""
Captura rápida de costos (solo Administrador), arriba de la lista de «Pedidos».

Junta en una sola tabla todos los renglones de pedidos que no tienen costo (costo unitario en 0 o vacío),
de pedidos no cancelados. Se escribe el costo, se pone el código y se guarda todo de una vez:
se actualiza pedido_items.costo_unitario y se recalcula pedidos.costo_total (la utilidad y la comisión
se recalculan solas en la base a partir del costo).

Si el modelo tiene costo en el inventario, la tabla ya viene prellenada con ese costo para solo revisar y guardar.
Los pedidos importados del Excel (sin renglones) aparecen como un solo renglón donde se captura el costo total.
"""
import json
import re

import pandas as pd
import streamlit as st

from corte import pedido_desde

import codigos

from permisos import roles as _roles
ROLES_COSTOS_RAPIDOS = _roles("costos_rapidos")
LIMITE = 500


def _num(v, default=0.0):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)))
    except ValueError:
        return default


def _cancelado(p):
    return p.get("status") == "Cancelado" or p.get("estado_pedido") == "Cancelado"


def _costos_inventario(sb, modelos):
    """{modelo: costo} desde productos.datos_json (TOTAL_COSTO o COSTO_REGISTRADO), solo los que tienen costo > 0."""
    out = {}
    modelos = sorted({str(m) for m in modelos if m})
    for i in range(0, len(modelos), 100):
        rows = sb.table("productos").select("MODELO,datos_json").in_("MODELO", modelos[i:i + 100]).execute().data
        for r in rows:
            d = r.get("datos_json")
            if not isinstance(d, dict):
                try:
                    d = json.loads(d or "{}")
                except (TypeError, ValueError):
                    d = {}
            c = _num(d.get("TOTAL_COSTO", d.get("COSTO_REGISTRADO")))
            if c > 0:
                out[str(r["MODELO"])] = round(c, 2)
    return out


_COSTO_NOTA_RX = re.compile(r"COSTO[^$\n]{0,40}\$\s*([\d,]+(?:\.\d+)?)", re.I)


def costo_en_nota(texto):
    """Suma de los «COSTO ... $1,337» escritos en el texto del pedido importado (0 si no hay)."""
    return round(sum(_num(m) for m in _COSTO_NOTA_RX.findall(texto or "")), 2)


def modelos_del_texto(texto):
    """[(modelo o None si S/N, cantidad)] a partir del texto libre del pedido importado del Excel."""
    try:
        from pedido_pdf import items_desde_texto
        return [(i.get("modelo"), int(i.get("cantidad") or 1)) for i in items_desde_texto(texto)]
    except Exception:
        return []


def sugerencia_importado(texto, inv):
    """(costo sugerido, origen) para un pedido importado sin renglones. Solo sugiere si es confiable."""
    nota = costo_en_nota(texto)
    if nota > 0:
        return nota, "Nota del pedido"
    mods = modelos_del_texto(texto)
    if not mods:
        return 0.0, ""
    con = [(m, c) for m, c in mods if m and str(m) in inv]
    if len(con) == len(mods):
        return round(sum(inv[str(m)] * c for m, c in con), 2), "Inventario"
    if con:
        parcial = round(sum(inv[str(m)] * c for m, c in con), 2)
        return 0.0, f"Inventario parcial ({len(con)} de {len(mods)}): ${parcial:,.2f}"
    return 0.0, ""


def pendientes_de_costo(sb):
    """Devuelve (renglones, pedidos) de lo que falta costear, sin cancelados (con memoria de 60 s)."""
    return _pendientes_mem(sb)


def _pendientes_mem_sin_cache(sb):
    """Devuelve (renglones, pedidos) de lo que falta costear, sin cancelados."""
    items = (sb.table("pedido_items")
             .select("id,numero_pedido,modelo,descripcion,cantidad,precio_unitario,total_linea,costo_unitario")
             .or_("costo_unitario.eq.0,costo_unitario.is.null").gte("numero_pedido", pedido_desde())
             .order("numero_pedido", desc=True).limit(LIMITE).execute().data)
    # Pedidos con costo total 0 que no tienen renglones (importados del Excel)
    peds0 = (sb.table("pedidos").select("numero_pedido").eq("costo_total", 0).gte("numero_pedido", pedido_desde())
             .order("numero_pedido", desc=True).limit(LIMITE).execute().data)
    nums = {i["numero_pedido"] for i in items} | {p["numero_pedido"] for p in peds0}
    if not nums:
        return [], {}
    pedidos = {}
    lista = sorted(nums)
    for i in range(0, len(lista), 200):
        for p in (sb.table("pedidos")
                  .select("numero_pedido,fecha,cliente,status,estado_pedido,costo_total,total,modelos_texto")
                  .in_("numero_pedido", lista[i:i + 200]).execute().data):
            if not _cancelado(p):
                pedidos[p["numero_pedido"]] = p
    items = [i for i in items if i["numero_pedido"] in pedidos]
    con_items = set()
    if peds0:
        sin = [p["numero_pedido"] for p in peds0 if p["numero_pedido"] in pedidos]
        for i in range(0, len(sin), 200):
            con_items |= {r["numero_pedido"] for r in sb.table("pedido_items").select("numero_pedido")
                          .in_("numero_pedido", sin[i:i + 200]).execute().data}
        for n in sin:
            if n not in con_items:
                texto = (pedidos[n].get("modelos_texto") or "").strip()
                mods = modelos_del_texto(texto)
                items.append({"id": None, "numero_pedido": n,
                              "modelo": ", ".join(m or "S/N" for m, _ in mods) or None,
                              "descripcion": texto or "Pedido sin renglones", "texto": texto,
                              "cantidad": 1, "precio_unitario": _num(pedidos[n].get("total")) or None,
                              "total_linea": None, "costo_unitario": 0})
    items.sort(key=lambda r: (-r["numero_pedido"], r["id"] or 0))
    return items, pedidos


from caches import guardar_en_memoria


@guardar_en_memoria(ttl=60)
def _pendientes_mem(_sb):
    return _pendientes_mem_sin_cache(_sb)


def guardar_costos(sb, cambios):
    """cambios = [{"id": id de pedido_items o None, "numero_pedido", "costo"}]. Devuelve lista de pedidos tocados."""
    tocados = set()
    for c in cambios:
        if c["id"] is not None:
            sb.table("pedido_items").update({"costo_unitario": c["costo"]}).eq("id", c["id"]).execute()
        else:   # pedido sin renglones: el costo capturado es el costo total
            sb.table("pedidos").update({"costo_total": c["costo"]}).eq("numero_pedido", c["numero_pedido"]).execute()
        tocados.add(c["numero_pedido"])
    con_renglones = sorted({c["numero_pedido"] for c in cambios if c["id"] is not None})
    for n in con_renglones:
        its = sb.table("pedido_items").select("cantidad,costo_unitario").eq("numero_pedido", n).execute().data
        total = round(sum(_num(i.get("costo_unitario")) * int(_num(i.get("cantidad"), 1)) for i in its), 2)
        sb.table("pedidos").update({"costo_total": total}).eq("numero_pedido", n).execute()
    return sorted(tocados)


def captura_rapida_costos(sb, user):
    if user["role"] not in ROLES_COSTOS_RAPIDOS:
        return
    ss = st.session_state
    msg = ss.pop("cr_msg", None)
    if msg:
        st.success(msg)
    try:
        items, pedidos = pendientes_de_costo(sb)
    except Exception as ex:
        st.caption(f"No se pudieron revisar los costos pendientes: {ex}")
        return
    if not items:
        return

    n_ped = len({i["numero_pedido"] for i in items})
    caja = st.container(border=True)
    t1, t2 = caja.columns([2, 3])
    abierto = t1.toggle("⚡ Capturar costos pendientes", key="cr_on")
    t2.caption(f"{len(items)} producto(s) sin costo en {n_ped} pedido(s).")
    if not abierto:
        return
    with caja:
        n_imp = sum(1 for i in items if i["id"] is None)
        f1, f2 = st.columns([3, 2])
        if n_imp and n_imp < len(items):
            ver = f1.radio("Mostrar", ["Todos", f"Pedidos de la app ({len(items) - n_imp})",
                                       f"Importados del Excel ({n_imp})"], horizontal=True, key="cr_filtro")
            if ver.startswith("Pedidos"):
                items = [i for i in items if i["id"] is not None]
            elif ver.startswith("Importados"):
                items = [i for i in items if i["id"] is None]
        prellenar = f2.checkbox("Prellenar costos sugeridos", value=True, key="cr_prellenar",
                                help="Del inventario (por modelo) o de la nota «COSTO … $» del pedido importado.")
        inv = {}
        if prellenar:
            modelos = [i["modelo"] for i in items if i["id"] is not None]
            for i in items:
                if i["id"] is None:
                    modelos += [m for m, _ in modelos_del_texto(i.get("texto"))]
            try:
                inv = _costos_inventario(sb, modelos)
            except Exception as ex:
                st.caption(f"No se pudo leer el costo del inventario: {ex}")
        filas = []
        for i in items:
            if i["id"] is None:
                costo, origen = sugerencia_importado(i.get("texto"), inv) if prellenar else (0.0, "")
            else:
                costo = inv.get(str(i.get("modelo") or ""), 0.0)
                origen = "Inventario" if costo else ""
            filas.append({
                "N° PEDIDO": i["numero_pedido"],
                "FECHA": pedidos[i["numero_pedido"]].get("fecha"),
                "CLIENTE": pedidos[i["numero_pedido"]].get("cliente"),
                "TIPO": "Excel (total)" if i["id"] is None else "Producto",
                "MODELO": i.get("modelo") or ("S/N" if i["id"] is not None else "—"),
                "DESCRIPCIÓN": i.get("descripcion") or "",
                "CANT.": int(_num(i.get("cantidad"), 1)),
                "PRECIO VENTA": _num(i.get("precio_unitario")) if i.get("precio_unitario") is not None else None,
                "SUGERIDO DE": origen,
                "COSTO UNIT.": costo,
            })
        df = pd.DataFrame(filas)
        n_sug = sum(1 for f in filas if f["COSTO UNIT."] > 0)
        st.caption("Escribe el costo de cada renglón y guarda; lo que dejes en 0 se queda pendiente. "
                   "En los importados del Excel (TIPO «Excel (total)») el precio es el total del pedido y el costo "
                   "que escribas es el costo total del pedido."
                   + (f" {n_sug} renglón(es) ya vienen con un costo sugerido: revísalos." if n_sug else ""))
        ed = st.data_editor(
            df, hide_index=True, use_container_width=True, key=f"cr_tabla_{ss.get('cr_ver', 0)}_{prellenar}_{ss.get('cr_filtro', '')}",
            disabled=[c for c in df.columns if c != "COSTO UNIT."],
            column_config={
                "N° PEDIDO": st.column_config.NumberColumn("N° PEDIDO", format="%d", width="small"),
                "CANT.": st.column_config.NumberColumn("CANT.", width="small"),
                "PRECIO VENTA": st.column_config.NumberColumn("PRECIO VENTA", format="$%.2f"),
                "COSTO UNIT.": st.column_config.NumberColumn("COSTO UNIT. ✏️", min_value=0.0, step=10.0,
                                                             format="$%.2f", required=True),
            })
        if len(items) >= LIMITE:
            st.caption(f"Se muestran los {LIMITE} más recientes; al guardar aparecerán los siguientes.")

        cambios = []
        for i, costo in zip(items, ed["COSTO UNIT."].tolist()):
            costo = round(_num(costo), 2)
            if costo > 0:
                cambios.append({"id": i["id"], "numero_pedido": i["numero_pedido"], "costo": costo})
        # Aviso de costo mayor que el precio de venta (posible error de dedo)
        raros = [f"{i['numero_pedido']}/{i.get('modelo') or 'S/N'}" for i, c in zip(items, ed["COSTO UNIT."].tolist())
                 if i.get("precio_unitario") and _num(c) > _num(i["precio_unitario"])]
        if raros:
            st.warning("Costo mayor que el precio de venta en: " + ", ".join(raros) + ". Revisa que esté bien.")

        q_v, q_c = codigos.pedir_identidad(sb, "cr_ident")
        if st.button(f"Guardar {len(cambios)} costo(s)", type="primary", key="cr_guardar", disabled=not cambios) \
                and (quien := codigos.confirmar(sb, q_v, q_c)):
            try:
                tocados = guardar_costos(sb, cambios)
            except Exception as ex:
                st.error(f"No se pudo guardar: {ex}")
                return
            for n in tocados:
                det = " · ".join(f"{c['id'] or 'total'}: ${c['costo']:,.2f}" for c in cambios if c["numero_pedido"] == n)
                codigos.registrar(sb, quien, "Capturó costos", "pedido", n, det)
            ss["cr_msg"] = f"Costos guardados: {len(cambios)} producto(s) en {len(tocados)} pedido(s)."
            ss["cr_ver"] = ss.get("cr_ver", 0) + 1
            st.rerun()