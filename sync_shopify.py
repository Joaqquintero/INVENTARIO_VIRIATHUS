"""
Ventas de Shopify -> inventario.

Revisa los pedidos recientes de Shopify y:
  * pedido PAGADO      -> marca sus modelos (SKU = MODELO) como VENDIDOS en el inventario;
  * pedido ANULADO     -> (cancelado / reembolsado) devuelve a DISPONIBLE los modelos que marcó ese pedido;
  * modelo ya vendido  -> deja un AVISO (posible doble venta) para revisar.
Es seguro correrlo muchas veces: cada pedido se procesa una sola vez (tabla shopify_ordenes).

Se usa de dos formas:
  1) Desde la app (shopify_ventas.py), cada pocos minutos mientras alguien la usa.
  2) Como programa aparte:  python sync_shopify.py   (variables de entorno: SHOPIFY_SHOP,
     SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SUPABASE_URL, SUPABASE_KEY)  -> ver sync_shopify.yml
"""
import datetime as dt
import json
import os
import sys

Q_ORDENES = """
query($q: String!, $cursor: String) {
  orders(first: 50, after: $cursor, query: $q, sortKey: UPDATED_AT) {
    nodes {
      id name createdAt cancelledAt displayFinancialStatus
      customer { displayName }
      currentTotalPriceSet { shopMoney { amount } }
      lineItems(first: 50) { nodes { sku title quantity } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""

VENDIDA = {"PAID", "PARTIALLY_REFUNDED"}
ANULADA = {"REFUNDED", "VOIDED", "EXPIRED"}


def _parse(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def leer_ordenes(gql, horas):
    desde = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=horas)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ordenes, cursor = [], None
    while True:
        d = gql(Q_ORDENES, {"q": f"updated_at:>={desde}", "cursor": cursor})["orders"]
        ordenes += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return sorted(ordenes, key=lambda o: o["createdAt"])
        cursor = d["pageInfo"]["endCursor"]


def clasificar(o):
    """'vendida' | 'anulada' | 'otra' (pendiente de pago, etc.)"""
    estado = str(o.get("displayFinancialStatus") or "").upper()
    if o.get("cancelledAt") or estado in ANULADA:
        return "anulada"
    if estado in VENDIDA:
        return "vendida"
    return "otra"


def _poner_disponibilidad(sb, modelo, vendido):
    """Cambia DISP del modelo. Devuelve 'ok' | 'no_existe' | 'ya_estaba' | 'cantidad'."""
    r = sb.table("productos").select("datos_json").eq("MODELO", modelo).limit(1).execute().data
    if not r:
        return "no_existe"
    d = _parse(r[0].get("datos_json"))
    actual = str(d.get("DISP", d.get("DISPONIBILIDAD", ""))).strip().lower() == "v"
    if actual == vendido:
        return "ya_estaba"
    if vendido and _num(d.get("CANTIDAD"), 1.0) > 1:
        return "cantidad"
    d["DISPONIBILIDAD"] = "v" if vendido else ""
    d["DISP"] = "v" if vendido else ""
    sb.table("productos").update({"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", modelo).execute()
    return "ok"


def _en_pedido_activo(sb, modelo):
    """Número de pedido de Ventas activo (no cancelado) que incluye el modelo, o None."""
    nums = [x["numero_pedido"] for x in
            sb.table("pedido_items").select("numero_pedido").eq("modelo", modelo).execute().data]
    if not nums:
        return None
    peds = (sb.table("pedidos").select("numero_pedido,estado_pedido,status")
            .in_("numero_pedido", nums).execute().data)
    for p in peds:
        if p.get("estado_pedido") != "Cancelado" and p.get("status") != "Cancelado":
            return p["numero_pedido"]
    return None


def sincronizar(sb, gql, horas=72, log=lambda *a: None):
    """Procesa los pedidos de Shopify de las últimas `horas`. Devuelve un resumen (dict)."""
    ordenes = leer_ordenes(gql, horas)
    resumen = {"revisados": len(ordenes), "nuevos": 0, "marcados": 0, "liberados": 0, "avisos": 0}
    if not ordenes:
        return resumen

    ids = [o["id"] for o in ordenes]
    existentes = {}
    for i in range(0, len(ids), 100):
        for f in sb.table("shopify_ordenes").select("*").in_("order_id", ids[i:i + 100]).execute().data:
            existentes[f["order_id"]] = f
    # Primera vez (tabla vacía): se registran los pedidos sin avisar de modelos ya vendidos
    inicial = not existentes and not sb.table("shopify_ordenes").select("order_id").limit(1).execute().data

    for o in ordenes:
        oid, tipo, fila = o["id"], clasificar(o), existentes.get(o["id"])
        items = [(str(li.get("sku") or "").strip(), li) for li in o["lineItems"]["nodes"]]

        if fila is None and tipo == "vendida":
            marcados, avisos = [], []
            for sku, li in items:
                if not sku:
                    avisos.append(f"«{li.get('title')}» no tiene SKU en Shopify; no se pudo marcar en el inventario.")
                    continue
                r = _poner_disponibilidad(sb, sku, True)
                if r == "ok":
                    marcados.append(sku)
                elif r == "no_existe":
                    avisos.append(f"El SKU {sku} no existe como modelo en el inventario.")
                elif r == "cantidad":
                    avisos.append(f"El modelo {sku} tiene cantidad > 1 en el inventario: no se marcó como vendido.")
                elif r == "ya_estaba" and not inicial:
                    avisos.append(f"El modelo {sku} YA figuraba como vendido en el inventario: posible doble venta.")
            sb.table("shopify_ordenes").insert({
                "order_id": oid, "nombre": o.get("name"), "creado": o.get("createdAt"),
                "cliente": ((o.get("customer") or {}).get("displayName")),
                "total": _num(((o.get("currentTotalPriceSet") or {}).get("shopMoney") or {}).get("amount")),
                "modelos": [s for s, _ in items if s], "marcados": marcados,
                "avisos": " | ".join(avisos) or None,
                "nota": "Registro inicial" if inicial else None,
            }).execute()
            resumen["nuevos"] += 1
            resumen["marcados"] += len(marcados)
            resumen["avisos"] += 1 if avisos else 0
            log(f"{o.get('name')}: marcados {marcados} avisos {avisos}")

        elif fila is not None and tipo == "anulada" and not fila.get("cancelada"):
            devueltos, quedan = [], []
            otras = [f for f in sb.table("shopify_ordenes").select("order_id,modelos")
                     .eq("cancelada", False).neq("order_id", oid).execute().data]
            for sku in (fila.get("marcados") or []):
                if any(sku in (x.get("modelos") or []) for x in otras):
                    quedan.append(f"{sku} (está en otro pedido de Shopify)")
                    continue
                pedido = _en_pedido_activo(sb, sku)
                if pedido:
                    quedan.append(f"{sku} (está en el pedido {pedido} de Ventas)")
                    continue
                if _poner_disponibilidad(sb, sku, False) == "ok":
                    devueltos.append(sku)
            nota = f"Anulado en Shopify. Devueltos a disponible: {', '.join(devueltos) or 'ninguno'}."
            if quedan:
                nota += f" Se dejaron vendidos: {'; '.join(quedan)}."
            sb.table("shopify_ordenes").update({"cancelada": True, "nota": nota}).eq("order_id", oid).execute()
            resumen["liberados"] += len(devueltos)
            log(f"{o.get('name')}: {nota}")
    return resumen


# ----------------------------------------------------------------------
# Uso como programa aparte (p. ej. GitHub Actions)
# ----------------------------------------------------------------------
def _gql_desde_entorno():
    import requests
    shop = os.environ["SHOPIFY_SHOP"]
    version = os.environ.get("SHOPIFY_API_VERSION", "2026-04")
    r = requests.post(f"https://{shop}/admin/oauth/access_token", data={
        "grant_type": "client_credentials", "client_id": os.environ["SHOPIFY_CLIENT_ID"],
        "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"]}, timeout=20)
    r.raise_for_status()
    token = r.json()["access_token"]

    def gql(query, variables=None):
        resp = requests.post(f"https://{shop}/admin/api/{version}/graphql.json",
                             json={"query": query, "variables": variables or {}},
                             headers={"X-Shopify-Access-Token": token}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"Shopify: {data['errors']}")
        return data["data"]
    return gql


if __name__ == "__main__":
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    horas = int(os.environ.get("SYNC_HORAS", "72"))
    print(sincronizar(sb, _gql_desde_entorno(), horas, log=print))
    sys.exit(0)