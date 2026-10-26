"""
Permisos por rol: un solo lugar para decidir quién ve qué.

Para dar o quitar un acceso, agrega o quita el rol en la lista de ese permiso.
Los roles válidos son los que se usan en .streamlit/secrets.toml → [usuarios.*] role = "...".
"""

DIOS = "Dios"
ADMIN = "Administrador"
CONTA = "Contabilidad"
FACT = "Facturación"
INV = "Inventario"
VEND = "Vendedor"

PERMISOS = {
    # --- Inventario / app principal ---
    "alta_manual":       (DIOS, ADMIN, CONTA, INV),            # pestaña «Alta Manual»
    "editar_ficha":      (DIOS, ADMIN, CONTA, INV),            # botón «Editar Ficha Completa» (y eliminar producto)
    "shopify_botones":   (DIOS, INV),                          # «Sincronizar con Shopify» y «Crear borrador en Shopify»
    "herramientas":      (DIOS, INV),                          # pestaña «Herramientas & Scripts» y sus avisos
    "shopify_lista":     (DIOS, ADMIN, CONTA, FACT, INV),      # pestaña «Shopify» (lista)
    "sync_productos":    (DIOS, ADMIN, CONTA, FACT, INV),      # sincronización automática de productos (segundo plano)

    # --- Ventas ---
    "ver_costos":        (DIOS, ADMIN, CONTA, FACT, INV),      # costos y utilidad en pedidos
    "cancelar_pedidos":  (DIOS, ADMIN, CONTA, FACT, INV, VEND),
    "editar_pedidos":    (DIOS, ADMIN, CONTA, FACT, INV, VEND),  # status / observaciones / facturación y efectivo
    "cotizar":           (DIOS, ADMIN, CONTA, FACT, INV, VEND),
    "por_facturar":      (DIOS, ADMIN, CONTA, FACT),           # pestaña «Por facturar»
    "costos_rapidos":    (DIOS, ADMIN, CONTA, FACT),           # «⚡ Capturar costos pendientes»
    "resumen_mensual":   (DIOS, ADMIN, CONTA, FACT),
    "comisiones":        (DIOS, ADMIN, CONTA, FACT),
    "codigos":           (DIOS,),                              # pestaña «Códigos» de vendedores
}


def roles(permiso):
    """Tupla de roles que tienen ese permiso."""
    return PERMISOS.get(permiso, (DIOS,))


def puede(user, permiso):
    """True si el usuario (dict con "role") tiene el permiso. Dios siempre puede todo."""
    rol = (user or {}).get("role")
    return rol == DIOS or rol in roles(permiso)
