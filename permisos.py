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
LOG = "Logística"          # como Vendedor: Consultar / Buscar y Ventas (Nuevo pedido, Cotizaciones, Pedidos, Remisiones)
INV2 = "Inventario II"     # los mismos permisos que Inventario

PERMISOS = {
    # --- Inventario / app principal ---
    "alta_manual":       (DIOS, ADMIN, CONTA, INV, INV2),            # pestaña «Alta Manual»
    "editar_ficha":      (DIOS, ADMIN, CONTA, INV, INV2),            # botón «Editar Ficha Completa» (y eliminar producto)
    "shopify_botones":   (DIOS, INV, INV2),                          # «Sincronizar con Shopify» y «Crear borrador en Shopify»
    "herramientas":      (DIOS, INV, INV2),                          # pestaña «Herramientas & Scripts» y sus avisos
    "shopify_lista":     (DIOS, ADMIN, CONTA, FACT, INV, INV2),      # pestaña «Shopify» (lista)
    "sync_productos":    (DIOS, ADMIN, CONTA, FACT, INV, INV2),      # sincronización automática de productos (segundo plano)

    # --- Ventas ---
    "ver_costos":        (DIOS, ADMIN, CONTA, FACT, INV, INV2),      # costos y utilidad en pedidos
    "cancelar_pedidos":  (DIOS, ADMIN, CONTA, FACT, INV, INV2, VEND, LOG),
    "editar_pedidos":    (DIOS, ADMIN, CONTA, FACT, INV, INV2, VEND, LOG),  # status / observaciones / facturación y efectivo
    "cotizar":           (DIOS, ADMIN, CONTA, FACT, INV, INV2, VEND, LOG),
    "remisiones":        (DIOS, ADMIN, CONTA, FACT, INV, INV2, VEND, LOG),  # pestaña «Remisiones» (solo PDF, no guarda nada)
    "por_facturar":      (DIOS, ADMIN, CONTA, FACT),           # pestaña «Por facturar»
    "costos_rapidos":    (DIOS, ADMIN, CONTA, FACT),           # «⚡ Capturar costos pendientes»
    "resumen_mensual":   (DIOS, ADMIN, CONTA, FACT),
    "comisiones":        (DIOS, ADMIN, CONTA, FACT),
    "caudal":            (DIOS, ADMIN, CONTA, FACT),           # pestaña «Caudal» (ver, registrar y anular)
    "codigos":           (DIOS,),                              # pestaña «Códigos» de vendedores
    "avisos_precio":     (DIOS, ADMIN),                        # aviso de cambios de precio autorizados
}


def roles(permiso):
    """Tupla de roles que tienen ese permiso."""
    return PERMISOS.get(permiso, (DIOS,))


def puede(user, permiso):
    """True si el usuario (dict con "role") tiene el permiso. Dios siempre puede todo."""
    rol = (user or {}).get("role")
    return rol == DIOS or rol in roles(permiso)
