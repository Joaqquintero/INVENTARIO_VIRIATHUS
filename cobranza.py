"""
Ventas → «Por cobrar»: pedidos con saldo pendiente (lo que los clientes todavía deben), del más viejo al más nuevo.

  * Cuenta los pedidos con saldo > 0 que no están Pagados / Obsequio / Cancelados (desde PEDIDO_DESDE, corte.py).
  * Antigüedad = días desde la fecha del pedido. También muestra la fecha del último cobro.
  * Por renglón: 👁 abre el pedido (y «Volver» regresa aquí) y «💬 Avisar» abre WhatsApp con un recordatorio.
  * Aviso arriba de la app: cuántos pedidos tienen saldo de más de 30 días.
Lo ven los roles del permiso «por_cobrar» (permisos.py). No necesita tablas nuevas.
"""
import datetime as dt
import urllib.parse

import pandas as pd
import streamlit as st

from caches import guardar_en_memoria
from corte import pedido_desde
from permisos import roles

ROLES_COBRAR = roles("por_cobrar")
VIEJO = 30                      # días: a partir de aquí sale en el aviso
SIN_SALDO = ("pagado", "obsequio", "cancelado")
FILTROS = {"Todas": 0, "Más de 30 días": 30, "Más de 60 días": 60, "Más de 90 días": 90}


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _fecha(v):
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _todas(consulta):
    filas, desde = [], 0
    while True:
        r = consulta().range(desde, desde + 999).execute().data or []
        filas += r
        if len(r) < 1000:
            return filas
        desde += 1000


@guardar_en_memoria(ttl=120)
def por_cobrar(_sb):
    """Pedidos con saldo pendiente: [{numero_pedido, fecha, cliente, telefono, total, cobrado, saldo, dias,
    ultimo_cobro, vendedores, estado_pedido, status}], el más viejo primero."""
    peds = _todas(lambda: _sb.table("pedidos")
                  .select("numero_pedido,fecha,cliente,telefono,total,anticipo,saldo,status,estado_pedido")
                  .gte("numero_pedido", pedido_desde()).gt("saldo", 0.5).order("numero_pedido"))
    peds = [p for p in peds if str(p.get("status") or "").strip().lower() not in SIN_SALDO
            and str(p.get("estado_pedido") or "").strip().lower() != "cancelado"]
    if not peds:
        return []
    nums = [p["numero_pedido"] for p in peds]
    ultimo, vends = {}, {}
    for i in range(0, len(nums), 200):
        parte = nums[i:i + 200]
        for g in _sb.table("pedido_pagos").select("numero_pedido,fecha_pago").in_("numero_pedido", parte) \
                .execute().data or []:
            f = _fecha(g.get("fecha_pago"))
            if f and (g["numero_pedido"] not in ultimo or f > ultimo[g["numero_pedido"]]):
                ultimo[g["numero_pedido"]] = f
        for v in _sb.table("pedido_vendedores").select("numero_pedido,vendedor").in_("numero_pedido", parte) \
                .execute().data or []:
            vends.setdefault(v["numero_pedido"], []).append(str(v.get("vendedor") or "").strip())
    hoy = dt.date.today()
    out = []
    for p in peds:
        f = _fecha(p.get("fecha"))
        out.append({
            "numero_pedido": p["numero_pedido"], "fecha": f, "cliente": p.get("cliente") or "",
            "telefono": p.get("telefono") or "", "total": _num(p.get("total")), "cobrado": _num(p.get("anticipo")),
            "saldo": _num(p.get("saldo")), "dias": (hoy - f).days if f else None,
            "ultimo_cobro": ultimo.get(p["numero_pedido"]), "vendedores": sorted(set(vends.get(p["numero_pedido"], []))),
            "estado_pedido": p.get("estado_pedido") or "", "status": p.get("status") or ""})
    out.sort(key=lambda x: (x["fecha"] or dt.date.max, x["numero_pedido"]))
    return out


def _dinero(n):
    return f"${_num(n):,.2f}"


def _fc(f):
    return f.strftime("%d/%m/%Y") if f else "—"


def _whatsapp(p):
    from ventas import _telefono_whatsapp
    tel = _telefono_whatsapp(p.get("telefono"))
    if not tel:
        return "—"
    msg = (f"Hola {p['cliente'] or ''}, te escribimos de Viriathus. Te recordamos que tu pedido N° {p['numero_pedido']} "
           f"tiene un saldo pendiente de {_dinero(p['saldo'])}. Quedamos atentos, ¡gracias!")
    return f"[💬 Avisar](https://wa.me/{tel}?text={urllib.parse.quote(msg)})"


def _ir():
    ss = st.session_state
    ss["nav_principal"] = ss["nav_principal__ultimo"] = "Ventas"
    ss["nav_ventas_ir"] = "Por cobrar"


def render_aviso(sb, user):
    """Arriba de la app: pedidos con saldo de más de 30 días."""
    if user.get("role") not in ROLES_COBRAR and user.get("role") != "Dios":
        return
    try:
        viejos = [p for p in por_cobrar(sb) if (p["dias"] or 0) > VIEJO]
    except Exception:
        return
    if viejos:
        c1, c2 = st.columns([4, 1])
        c1.warning(f"🧾 {len(viejos)} pedido(s) con saldo de más de {VIEJO} días · "
                   f"{_dinero(sum(p['saldo'] for p in viejos))} por cobrar")
        c2.button("Ver por cobrar →", key="cob_ir", on_click=_ir, use_container_width=True)


def tab_por_cobrar(sb, user):
    import lista_acciones as la
    from ventas import abrir_pedido
    try:
        filas = por_cobrar(sb)
    except Exception as ex:
        st.error(f"No se pudo leer la cobranza: {ex}")
        return
    if not filas:
        st.success("✓ No hay pedidos con saldo por cobrar.")
        return

    tot = sum(p["saldo"] for p in filas)
    m30 = [p for p in filas if (p["dias"] or 0) > 30]
    m90 = [p for p in filas if (p["dias"] or 0) > 90]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("POR COBRAR", _dinero(tot), help="Suma de los saldos (con IVA, como los totales de los pedidos).")
    m2.metric("PEDIDOS", len(filas))
    m3.metric("MÁS DE 30 DÍAS", _dinero(sum(p["saldo"] for p in m30)), f"{len(m30)} pedido(s)", delta_color="off")
    m4.metric("MÁS DE 90 DÍAS", _dinero(sum(p["saldo"] for p in m90)), f"{len(m90)} pedido(s)", delta_color="off")

    c1, c2, c3 = st.columns([2, 1.5, 1.5])
    buscar = c1.text_input("Buscar cliente o N° de pedido", key="cob_buscar", placeholder="Cliente o número…")
    todos_v = sorted({v for p in filas for v in p["vendedores"]})
    vend = c2.selectbox("Vendedor", ["Todos"] + todos_v, key="cob_vend")
    antig = c3.selectbox("Antigüedad", list(FILTROS), key="cob_antig")
    t = buscar.strip().lower()
    vista = [p for p in filas
             if (not t or t in str(p["numero_pedido"]) or t in p["cliente"].lower())
             and (vend == "Todos" or vend in p["vendedores"])
             and (FILTROS[antig] == 0 or (p["dias"] or 0) > FILTROS[antig])]
    if not vista:
        st.info("Nada coincide con ese filtro.")
        return
    if len(vista) != len(filas):
        st.caption(f"{len(vista)} pedido(s) · {_dinero(sum(p['saldo'] for p in vista))} por cobrar con este filtro")

    columnas = [("N° PEDIDO", 0.8, lambda p: f"**{p['numero_pedido']}**"),
                ("FECHA", 0.9, lambda p: _fc(p["fecha"])),
                ("DÍAS", 0.55, lambda p: ("🔴 " if (p["dias"] or 0) > 90 else "🟠 " if (p["dias"] or 0) > 30 else "")
                 + (str(p["dias"]) if p["dias"] is not None else "—")),
                ("CLIENTE", 2.0, lambda p: p["cliente"]),
                ("VENDEDOR", 1.1, lambda p: ", ".join(p["vendedores"]) or "—"),
                ("TOTAL", 1.0, lambda p: _dinero(p["total"])),
                ("SALDO", 1.0, lambda p: f"**{_dinero(p['saldo'])}**"),
                ("ÚLTIMO COBRO", 0.95, lambda p: _fc(p["ultimo_cobro"])),
                ("OBSERVACIONES", 1.3, lambda p: p["estado_pedido"] or p["status"] or "—"),
                ("CLIENTE ✆", 0.9, _whatsapp)]
    la.lista([dict(p, _id=p["numero_pedido"]) for p in vista], columnas, "cob_lista",
             al_ver=lambda p: abrir_pedido(p["numero_pedido"], volver_a="Por cobrar"),
             ayuda_ver="Abrir el pedido (para registrar el cobro)", por_pagina=100)

    with st.expander("Por vendedor"):
        df = pd.DataFrame([{"VENDEDOR": v, "SALDO": p["saldo"], "VIEJO": p["saldo"] if (p["dias"] or 0) > 30 else 0.0}
                           for p in vista for v in (p["vendedores"] or ["(sin vendedor)"])])
        res = (df.groupby("VENDEDOR").agg(PEDIDOS=("SALDO", "count"), POR_COBRAR=("SALDO", "sum"),
                                          MAS_30=("VIEJO", "sum")).reset_index()
               .sort_values("POR_COBRAR", ascending=False)
               .rename(columns={"POR_COBRAR": "POR COBRAR", "MAS_30": "MÁS DE 30 DÍAS"}))
        st.dataframe(res, use_container_width=True, hide_index=True, column_config={
            "POR COBRAR": st.column_config.NumberColumn(format="$%.2f"),
            "MÁS DE 30 DÍAS": st.column_config.NumberColumn(format="$%.2f")})
        st.caption("Un pedido con dos vendedores cuenta para los dos.")
    csv = pd.DataFrame([{"PEDIDO": p["numero_pedido"], "FECHA": _fc(p["fecha"]), "DIAS": p["dias"],
                         "CLIENTE": p["cliente"], "TELEFONO": p["telefono"], "VENDEDOR": ", ".join(p["vendedores"]),
                         "TOTAL": p["total"], "COBRADO": p["cobrado"], "SALDO": p["saldo"],
                         "ULTIMO COBRO": _fc(p["ultimo_cobro"]), "OBSERVACIONES": p["estado_pedido"] or p["status"]}
                        for p in vista])
    from navegacion import descargar
    descargar(st, "Descargar (CSV)", csv.to_csv(index=False).encode("utf-8-sig"),
              file_name=f"por_cobrar_{dt.date.today():%Y%m%d}.csv", mime="text/csv")
