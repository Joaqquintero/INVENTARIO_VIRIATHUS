import sqlite3
import pandas as pd

def buscar(criterio):
    conn = sqlite3.connect("inventario.db")
    criterio_clean = str(criterio).strip()
    
    # Busca coincidencia de modelo o nombre
    query = """
    SELECT MODELO, NOMBRE, DESCRIPCIÓN
    FROM productos 
    WHERE MODELO = ? OR NOMBRE LIKE ?
    """
    
    df_resultado = pd.read_sql_query(query, conn, params=(criterio_clean, f"%{criterio_clean}%"))
    conn.close()
    
    if df_resultado.empty:
        print(f"\n❌ No se encontraron productos con el criterio: '{criterio}'")
    else:
        print(f"\n🔍 Encontrados {len(df_resultado)} resultado(s):")
        print(df_resultado.to_string(index=False))

if _name_ == "_main_":
    busqueda = input("Ingresa un MODELO o PALABRA CLAVE para buscar: ").strip()
    buscar(busqueda)