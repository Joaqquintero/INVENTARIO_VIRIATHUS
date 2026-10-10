import os
import json
import toml
from supabase import create_client

def cargar_secrets():
    path_secrets = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(path_secrets):
        secrets = toml.load(path_secrets)
        return secrets.get("SUPABASE_URL"), secrets.get("SUPABASE_KEY")
    else:
        print("⚠️ No se encontró .streamlit/secrets.toml")
        return None, None

def actualizar_todo_con_paginacion():
    url, key = cargar_secrets()
    if not url or not key:
        print("❌ Error: Faltan SUPABASE_URL o SUPABASE_KEY en secrets.toml")
        return

    supabase = create_client(url, key)

    print("🔄 Iniciando descarga y actualización completa de STOCK en Supabase...\n")
    
    offset = 0
    limite_pagina = 1000
    total_procesados = 0
    total_vendidos = 0
    total_disponibles = 0

    while True:
        # Paginación: traer de 1,000 en 1,000
        response = supabase.table("productos").select("*").range(offset, offset + limite_pagina - 1).execute()
        productos = response.data
        
        if not productos:
            break  # No hay más registros que procesar
            
        print(f"📦 Procesando grupo del registro {offset + 1} al {offset + len(productos)}...")
        
        actualizados = []
        for item in productos:
            modelo = item.get("MODELO")
            ruta_imagen = item.get("RUTA_IMAGEN")
            
            raw_json = item.get("datos_json", "{}")
            if isinstance(raw_json, str):
                try:
                    datos = json.loads(raw_json)
                except Exception:
                    datos = {}
            else:
                datos = raw_json if isinstance(raw_json, dict) else {}

            # Detección flexible de disponibilidad
            disp = str(datos.get("DISP", datos.get("DISPONIBILIDAD", ""))).strip().lower()
            
            if "v" in disp or "vendido" in disp:
                stock = 0
                datos["DISP"] = "v"
                datos["DISPONIBILIDAD"] = "v"
                total_vendidos += 1
            else:
                stock = 1
                datos["DISP"] = ""
                datos["DISPONIBILIDAD"] = ""
                total_disponibles += 1
                
            datos["STOCK"] = stock

            payload = {
                "MODELO": modelo,
                "datos_json": json.dumps(datos)
            }
            if ruta_imagen:
                payload["RUTA_IMAGEN"] = ruta_imagen

            actualizados.append(payload)

        # Guardar en lotes de 200 para maximizar rendimiento
        lote_size = 200
        for i in range(0, len(actualizados), lote_size):
            lote = actualizados[i:i + lote_size]
            supabase.table("productos").upsert(lote, on_conflict="MODELO").execute()
            
        total_procesados += len(productos)
        offset += limite_pagina

    print(f"\n🎉 ¡Proceso GLOBAL de Stock completado exitosamente!")
    print(f"   📊 Total de productos analizados: {total_procesados}")
    print(f"   🔴 Vendidos (Stock = 0): {total_vendidos}")
    print(f"   🟢 Disponibles (Stock = 1): {total_disponibles}")

if __name__ == "__main__":
    actualizar_todo_con_paginacion()


