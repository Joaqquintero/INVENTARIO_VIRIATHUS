"""
Reservas de inventario para las cotizaciones.

  * Un producto ÚNICO (cantidad 1) reservado queda con DISP = "r" (RESERVADO): no disponible, y en Shopify agotado.
  * Si el producto tiene varias unidades, se descuentan las unidades reservadas de CANTIDAD; si llegan a 0 queda "r".
  * Lo que cada cotización apartó se anota en cotizacion_items (apartado / reservado).
  * Nunca se libera algo que ya no esté como "r" (p. ej. si se vendió por otro lado mientras tanto).
"""
import json


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _parse(raw):
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def estado_disp(d):
    """'v' vendido, 'r' reservado o '' disponible."""
    v = str(d.get("DISP", d.get("DISPONIBILIDAD", "")) or "").strip().lower()
    return v if v in ("v", "r") else ""


def _leer(sb, modelo):
    r = sb.table("productos").select("datos_json").eq("MODELO", modelo).limit(1).execute()
    return _parse(r.data[0].get("datos_json")) if r.data else None


def _guardar(sb, modelo, d):
    sb.table("productos").update({"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", modelo).execute()


def _shopify(modelo, agotado):
    """Refleja agotado/disponible en Shopify. Devuelve un aviso solo si falla."""
    try:
        from shopify_borrador import marcar_disponibilidad_shopify
    except Exception:
        return None
    try:
        estado, msg = marcar_disponibilidad_shopify(modelo, agotado)
    except Exception as ex:
        return f"Modelo {modelo}: no se pudo reflejar en Shopify ({ex})."
    if estado == "error":
        return (f"Modelo {modelo}: el inventario se actualizó, pero no se pudo reflejar en Shopify ({msg}). "
                "Usa Sincronizar con Shopify en la ficha del producto.")
    return None


def reservar(sb, modelo, unidades):
    """Aparta `unidades` del modelo.
    Devuelve (ok, mensaje, aviso, descontadas): `descontadas` = unidades que se restaron de CANTIDAD
    (0 en un producto único: solo se marca 'r')."""
    d = _leer(sb, modelo)
    if d is None:
        return False, f"El modelo {modelo} no existe en el inventario.", None, 0
    est = estado_disp(d)
    if est == "v":
        return False, f"El modelo {modelo} ya está vendido.", None, 0
    if est == "r":
        return False, f"El modelo {modelo} ya está reservado (cotización o remisión abierta).", None, 0
    stock = _num(d.get("CANTIDAD"), 1.0)
    if stock <= 0:
        stock = 1.0
    if unidades > stock:
        return False, f"El modelo {modelo} solo tiene {stock:g} unidad(es) disponible(s).", None, 0
    descontadas, agotado = 0, False
    if stock <= 1:                                  # producto único: solo se marca reservado
        d["DISP"] = d["DISPONIBILIDAD"] = "r"
        agotado = True
    else:                                           # varias unidades: se descuentan las apartadas
        descontadas = int(unidades)
        resta = stock - descontadas
        d["CANTIDAD"] = int(resta) if float(resta).is_integer() else resta
        if resta <= 0:
            d["DISP"] = d["DISPONIBILIDAD"] = "r"
            agotado = True
    _guardar(sb, modelo, d)
    return True, "ok", (_shopify(modelo, True) if agotado else None), descontadas


def liberar(sb, modelo, descontadas=0):
    """Deshace la reserva: devuelve las unidades descontadas y, si el modelo sigue reservado ('r'), lo deja
    disponible. Si ya se vendió por otro lado ('v') no lo toca. Devuelve un aviso o None."""
    d = _leer(sb, modelo)
    if d is None:
        return f"Modelo {modelo}: ya no existe en el inventario."
    if descontadas:
        nuevo = _num(d.get("CANTIDAD"), 0.0) + descontadas
        d["CANTIDAD"] = int(nuevo) if float(nuevo).is_integer() else nuevo
    era_r = estado_disp(d) == "r"
    if era_r:
        d["DISP"] = d["DISPONIBILIDAD"] = ""
    if descontadas or era_r:
        _guardar(sb, modelo, d)
    if era_r:
        return _shopify(modelo, False)
    if estado_disp(d) == "v":
        return f"Modelo {modelo}: ya figura como vendido, no se volvió a poner disponible."
    return None


def consumir(sb, modelo):
    """La cotización pasó a pedido: lo apartado se queda vendido. Si estaba reservado ('r') pasa a vendido ('v').
    Las unidades descontadas de un producto con varias unidades se quedan descontadas. Devuelve un aviso o None."""
    d = _leer(sb, modelo)
    if d is None:
        return f"Modelo {modelo}: no se encontró en el inventario."
    if estado_disp(d) == "r":
        d["DISP"] = d["DISPONIBILIDAD"] = "v"
        _guardar(sb, modelo, d)
        return _shopify(modelo, True)
    return None