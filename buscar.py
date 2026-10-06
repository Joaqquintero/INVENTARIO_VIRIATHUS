import sqlite3
import pandas as pd
import os

def buscar_en_terminal(criterio):
    if not os.path.exists("inventario.db"):
        print("\n❌ La base de datos 'inventario.db' no existe. Ejecuta primero cargar_excel.py.")
        return

    conn = sqlite3.connect("inventario.db")
    df_todos = pd.read_sql_query("SELECT * FROM productos", conn)
    conn.close()

    if df_todos.empty:
        print("\n⚠️ La base de datos está vacía.")
        return

    # Buscar coincidencia en CUALQUIER columna (MODELO, NOMBRE, DESCRIPCIÓN, etc.)
    coincidencias = df_todos[
        df_todos.astype(str).apply(
            lambda fila: fila.str.contains(criterio, case=False, na=False)
        ).any(axis=1)
    ]

    if coincidencias.empty:
        print(f"\n❌ No se encontraron resultados para: '{criterio}'")
        return

    print(f"\n🔍 Se encontraron {len(coincidencias)} resultado(s) para: '{criterio}'\n" + "="*50)

    for index, fila in coincidencias.iterrows():
        print(f"📌 RESULTADO {index + 1}:")
        for columna, valor in fila.items():
            val_str = str(valor).strip()
            # Ignorar valores vacíos
            if val_str and val_str.lower() not in ['none', 'nan', 'nat', '']:
                print(f"  • {columna}: {val_str}")
        print("-" * 50)

if __name__ == "__main__":
    busqueda = input("Ingresa MODELO, NOMBRE o PALABRA CLAVE para buscar: ").strip()
    if busqueda:
        buscar_en_terminal(busqueda)