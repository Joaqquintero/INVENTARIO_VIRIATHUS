import os
import glob
import pandas as pd
import sqlite3

def consolidar_inventarios():
    carpeta = "inventarios_excel"
    archivos = glob.glob(os.path.join(carpeta, "*.xlsx"))
    
    if not archivos:
        print(f"⚠️ No se encontraron archivos Excel en '{carpeta}'.")
        return

    print(f"📦 Procesando {len(archivos)} archivo(s) Excel...\n")
    
    lista_dfs = []

    for i, archivo in enumerate(archivos, 1):
        nombre = os.path.basename(archivo)
        print(f"[{i}/{len(archivos)}] Leyendo {nombre}...", end="", flush=True)
        
        try:
            try:
                df = pd.read_excel(archivo, header=5, engine="calamine")
            except Exception:
                df = pd.read_excel(archivo, header=5, engine="openpyxl")
            
            # Limpiar filas vacías y sin MODELO
            df = df.dropna(how='all')
            
            # Buscar la columna MODELO
            col_modelo = next((c for c in df.columns if str(c).strip().upper() == 'MODELO'), None)
            
            if col_modelo:
                df = df.dropna(subset=[col_modelo])
                df['MODELO'] = df[col_modelo].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
                lista_dfs.append(df)
                print(f" -> ✅ ({len(df)} productos)", flush=True)
            else:
                print(f" -> ⚠️ Omitido: No se encontró la columna 'MODELO'", flush=True)
            
        except Exception as e:
            print(f" -> ❌ Error: {e}", flush=True)

    if not lista_dfs:
        print("\n❌ No se extrajeron datos de ningún archivo.")
        return

    print("\n🔄 Unificando archivos y filtrando duplicados...")
    df_completo = pd.concat(lista_dfs, ignore_index=True)
    
    filas_antes = len(df_completo)
    df_completo = df_completo.drop_duplicates(subset=['MODELO'], keep='last')
    filas_despues = len(df_completo)
    
    # --- CONVERSIÓN CRÍTICA: Convertir todas las columnas a texto/compatibles con SQLite ---
    for col in df_completo.columns:
        if pd.api.types.is_datetime64_any_dtype(df_completo[col]):
            df_completo[col] = df_completo[col].dt.strftime('%Y-%m-%d')
        else:
            df_completo[col] = df_completo[col].astype(str)

    print("💾 Escribiendo en la base de datos 'inventario.db'...")
    conn = sqlite3.connect("inventario.db")
    df_completo.to_sql("productos", conn, if_exists="replace", index=False)
    
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_modelo ON productos(MODELO);")
    conn.commit()
    conn.close()

    print("\n🎉 ¡Proceso completado con éxito!")
    print(f"  • Total de productos unificados en BD: {filas_despues}")
    if filas_antes - filas_despues > 0:
        print(f"  • Duplicados eliminados: {filas_antes - filas_despues}")

if __name__ == "__main__":
    consolidar_inventarios()