"""
Crea un BORRADOR de producto en Shopify a partir de un producto del inventario.

Uso desde app.py:
    from shopify_borrador import crear_borrador_shopify
    ok, mensaje, url = crear_borrador_shopify(modelo, pdata)
"""
import html
import re
import uuid

import requests
import streamlit as st

# ----------------------------------------------------------------------
# Ajustes
# ----------------------------------------------------------------------
SHOPIFY_API_VERSION = "2026-04"
SHOPIFY_UBICACION = "Merida 22"      # nombre exacto de la ubicación en Shopify
INCLUIR_FOTO = True                  # False = el borrador se crea sin foto

# Tu tabla: número de la categoría -> Type en Shopify
TIPO_POR_CATEGORIA = {
    "1": "Mobiliario",
    "2": "Objetos",
    "3": "Libros",
    "4": "Arte",
    "5": "Gráfica",
}


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _a_float(val, default=0.0):
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return float(val)
    limpio = re.sub(r"[^\d.-]", "", str(val).strip())
    try:
        return float(limpio)
    except ValueError:
        return default


def _descripcion_a_html(texto):
    """Texto plano -> HTML: línea en blanco = párrafo, salto simple = <br>."""
    texto = str(texto or "").strip()
    if not texto:
        return ""
    parrafos = re.split(r"\n\s*\n", texto)
    return "".join("<p>" + html.escape(p.strip()).replace("\n", "<br>") + "</p>" for p in parrafos if p.strip())


def _esta_vendido(pdata):
    v = str(pdata.get("DISP", pdata.get("DISPONIBILIDAD", ""))).strip().lower()
    return v == "v"


def _secret(nombre):
    valor = st.secrets.get(nombre, "")
    if not valor:
        raise RuntimeError(f"Falta {nombre} en .streamlit/secrets.toml")
    return str(valor).strip()


# ----------------------------------------------------------------------
# Conexión con Shopify
# ----------------------------------------------------------------------
@st.cache_data(ttl=3000, show_spinner=False)
def _token_shopify():
    """Pide un token con Client ID + Client Secret. Se guarda ~50 min y se renueva solo."""
    shop = _secret("SHOPIFY_SHOP")
    r = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": _secret("SHOPIFY_CLIENT_ID"),
            "client_secret": _secret("SHOPIFY_CLIENT_SECRET"),
        },
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"No se pudo obtener el token de Shopify ({r.status_code}): {r.text[:300]}")
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"Shopify no devolvió token: {r.text[:300]}")
    return token


def _gql(query, variables=None):
    shop = _secret("SHOPIFY_SHOP")
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    for intento in range(2):
        r = requests.post(
            url,
            json={"query": query, "variables": variables or {}},
            headers={"X-Shopify-Access-Token": _token_shopify(), "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code == 401 and intento == 0:
            _token_shopify.clear()   # token vencido: se pide uno nuevo y se reintenta
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"Shopify respondió {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"Shopify: {data['errors']}")
    return data["data"]


def _errores(resultado):
    errs = (resultado or {}).get("userErrors") or []
    return "; ".join(f"{e.get('message')}" for e in errs)


# ----------------------------------------------------------------------
# Consultas / mutaciones
# ----------------------------------------------------------------------
Q_SKU = """
query($q: String!) {
  productVariants(first: 5, query: $q) {
    nodes { sku product { id title status } }
  }
}"""

Q_UBICACIONES = """
{ locations(first: 50) { nodes { id name } } }"""

M_CREAR = """
mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
  productCreate(product: $product, media: $media) {
    product { id variants(first: 1) { nodes { id inventoryItem { id } } } }
    userErrors { field message }
  }
}"""

M_VARIANTE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id }
    userErrors { field message }
  }
}"""

# Desde 2026-04 Shopify exige una clave de idempotencia (@idempotent) y el campo
# changeFromQuantity (null = no comparar). Ya no existe ignoreCompareQuantity.
M_INVENTARIO = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) @idempotent(key: "%s") {
    userErrors { field message }
  }
}"""

M_ACTIVAR = """
mutation($itemId: ID!, $locId: ID!) {
  inventoryActivate(inventoryItemId: $itemId, locationId: $locId) {
    userErrors { field message }
  }
}"""


def _fijar_inventario(entrada):
    """Cada intento lleva su propia clave de idempotencia."""
    return _gql(M_INVENTARIO % uuid.uuid4(), {"input": entrada})["inventorySetQuantities"]


def _url_admin(product_gid):
    shop = _secret("SHOPIFY_SHOP").replace(".myshopify.com", "")
    return f"https://admin.shopify.com/store/{shop}/products/{product_gid.split('/')[-1]}"


def _foto_publica(modelo):
    """URL de la foto en Supabase si existe; si no, None."""
    base = str(st.secrets.get("SUPABASE_URL", "")).rstrip("/")
    if not base:
        return None
    url = f"{base}/storage/v1/object/public/imagenes/{modelo}.jpg"
    try:
        if requests.head(url, timeout=10, allow_redirects=True).status_code == 200:
            return url
    except requests.RequestException:
        pass
    return None


# ----------------------------------------------------------------------
# Función principal
# ----------------------------------------------------------------------
def crear_borrador_shopify(modelo, pdata):
    """Devuelve (ok, mensaje, url_en_shopify)."""
    url = None
    try:
        modelo = str(modelo).strip()
        nombre = str(pdata.get("NOMBRE", "")).strip()
        if not nombre:
            return False, "El producto no tiene NOMBRE. Complétalo y guarda antes de enviarlo.", None

        precio = _a_float(pdata.get("REDONDEO_SHOWROOM", 0))
        if precio <= 0:
            return False, "REDONDEO SHOWROOM está en 0 o vacío. Captura el precio y guarda antes de enviarlo.", None

        # 1) ¿Ya existe un producto con ese SKU?
        d = _gql(Q_SKU, {"q": f'sku:"{modelo}"'})
        for v in d["productVariants"]["nodes"]:
            if str(v.get("sku", "")).strip() == modelo:
                p = v["product"]
                return False, f"Ya existe en Shopify: «{p['title']}» ({p['status']}). No se creó otro.", _url_admin(p["id"])

        # 2) Ubicación del inventario
        locs = _gql(Q_UBICACIONES)["locations"]["nodes"]
        loc = next((l for l in locs if l["name"].strip().lower() == SHOPIFY_UBICACION.lower()), None)
        if not loc:
            nombres = ", ".join(l["name"] for l in locs) or "(ninguna)"
            return False, f"No encontré la ubicación «{SHOPIFY_UBICACION}». Ubicaciones disponibles: {nombres}", None

        # 3) Crear el producto en borrador
        cat_num = str(pdata.get("CATEGORIA", "")).strip()[:1]
        producto = {
            "title": nombre,
            "descriptionHtml": _descripcion_a_html(pdata.get("DESCRIPCION", "")),
            "status": "DRAFT",
        }
        if cat_num in TIPO_POR_CATEGORIA:
            producto["productType"] = TIPO_POR_CATEGORIA[cat_num]

        media = None
        foto = _foto_publica(modelo) if INCLUIR_FOTO else None
        if foto:
            media = [{"originalSource": foto, "mediaContentType": "IMAGE", "alt": nombre}]

        r = _gql(M_CREAR, {"product": producto, "media": media})["productCreate"]
        if r["userErrors"]:
            return False, f"Shopify no pudo crear el producto: {_errores(r)}", None
        prod = r["product"]
        url = _url_admin(prod["id"])
        variante = prod["variants"]["nodes"][0]

        # 4) Precio y SKU
        r = _gql(M_VARIANTE, {
            "productId": prod["id"],
            "variants": [{
                "id": variante["id"],
                "price": f"{precio:.2f}",
                "inventoryItem": {"sku": modelo, "tracked": True},
            }],
        })["productVariantsBulkUpdate"]
        if r["userErrors"]:
            return False, f"El borrador se creó pero falló el precio/SKU: {_errores(r)}. Revísalo en Shopify.", url

        # 5) Inventario: 1 disponible, 0 vendido
        cantidad = 0 if _esta_vendido(pdata) else 1
        entrada = {
            "name": "available",
            "reason": "correction",
            "quantities": [{
                "inventoryItemId": variante["inventoryItem"]["id"],
                "locationId": loc["id"],
                "quantity": cantidad,
                "changeFromQuantity": None,
            }],
        }
        r = _fijar_inventario(entrada)
        if r["userErrors"]:
            # Si el producto aún no está "activo" en la ubicación, se activa y se reintenta
            _gql(M_ACTIVAR, {"itemId": variante["inventoryItem"]["id"], "locId": loc["id"]})
            r = _fijar_inventario(entrada)
            if r["userErrors"]:
                return False, f"El borrador se creó pero falló el inventario: {_errores(r)}. Revísalo en Shopify.", url

        extra = "" if foto or not INCLUIR_FOTO else " (sin foto: no encontré la imagen en Supabase)"
        estado = "vendido, inventario 0" if cantidad == 0 else "disponible, inventario 1"
        return True, f"Borrador creado ({estado}){extra}.", url

    except Exception as ex:
        if url:
            return False, f"El borrador se creó pero quedó incompleto. Error: {ex}", url
        return False, f"Error: {ex}", None