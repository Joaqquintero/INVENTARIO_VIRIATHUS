"""
Importa las ventas de los Excel (hojas 'CONSECUTIVO VENTAS 2025', '... 2026') a las tablas de pedidos.

Uso (desde la carpeta del proyecto, con los Excel dentro de la carpeta  ventas_excel/ ):
    python importar_ventas.py --prueba              # revisa todo y genera el reporte, sin subir nada
    python importar_ventas.py                       # importa lo que falte
    python importar_ventas.py --actualizar          # re-sincroniza los pedidos YA importados desde Excel
                                                    # (borra y vuelve a insertar solo los origen='historico')
    python importar_ventas.py --anios 2025 2026     # hojas a leer (por defecto 2025 y 2026)
    python importar_ventas.py --hasta 8737          # no importa pedidos con número mayor a este

Siempre escribe  reporte_importacion.csv  con lo que conviene revisar (fechas corregidas, etc.).

Para deshacer TODO lo importado:  delete from pedidos where origen = 'historico';
"""
import argparse
import csv
import datetime as dt
import glob
import os
import re
import unicodedata

import pandas as pd

CARPETA = "ventas_excel"
TAMANO_LOTE = 200
USUARIO_IMPORTACION = "Importación Excel"
# Nombres equivalentes en los Excel -> nombre único. Agrega aquí los que quieras unificar.
ALIAS_VENDEDOR = {"VIRI": "VIRIATO", "MARISOL P": "MARISOL"}
# Valores de la columna VENDEDOR que no son personas: no generan comisión (se anotan en observaciones).
NO_VENDEDORES = ("CANCELADO", "ON LINE", "VIRIATHUS")
MESES = {"ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6, "JULIO": 7,
         "AGOSTO": 8, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12}
FORMAS = [("EFE", "Efectivo"), ("TAR", "Tarjeta"), ("TRA", "Transferencia"), ("TRN", "Transferencia"),
          ("TAN", "Transferencia"), ("DEP", "Deposito"), ("PAY", "Paypal"), ("MER", "MercadoPago")]
SIN_PAGO = ("PENDIENTE", "CANCELADO", "OBSEQUIO", "REGALO", "PAGADO", "NO APLICA", "")


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def norm(texto):
    t = unicodedata.normalize("NFD", str(texto if texto is not None else ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t.upper()).strip()


def vacio(v):
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def txt(v):
    return "" if vacio(v) else re.sub(r"\s+", " ", str(v)).strip()


def num(v):
    """float o None si está vacío / no es número."""
    if vacio(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try:
        return float(s)
    except ValueError:
        return None


def parse_fecha(v, anio):
    """Devuelve (fecha | None, corregida). Corrige años mal escritos (205, 2026 en la hoja 2025, etc.)."""
    if vacio(v):
        return None, False
    d = m = y = None
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        d, m, y = v.day, v.month, v.year
    else:
        r = re.match(r"^\s*(\d{1,2})\D+(\d{1,2})\D+(\d{2,4})\s*$", str(v))
        if not r:
            return None, False
        d, m, y = (int(x) for x in r.groups())
        if y < 100:
            y += 2000
        elif y < 1000:
            y = None                       # '205' -> año inválido
    corregida = (y != anio)
    try:
        return dt.date(anio, m, d), corregida
    except ValueError:
        return None, False


# ----------------------------------------------------------------------
# Normalización de catálogos
# ----------------------------------------------------------------------
def mapear_categoria(t):
    n = norm(t)
    if not n:
        return None
    if n in ("IBAN PASANDO", "IBA PASANDO"):
        return "Iba pasando"
    if n == "CLIENTE":
        return "Cliente"
    if n == "CANCELADO":
        return "Cancelado"
    return t.strip().title()


def mapear_status(t):
    n = norm(t)
    if n == "PAGADO":
        return "Pagado"
    if n == "PENDIENTE":
        return "Pendiente"
    if n == "CANCELADO":
        return "Cancelado"
    if n in ("OBSEQUIO", "REGALO"):
        return "Obsequio"
    return t.strip().title() if n else None


def mapear_estado(t, status):
    """Devuelve (estado_pedido | None, nota | None). La nota conserva el texto original si no se pudo mapear."""
    n = norm(t)
    if not n:
        return None, None
    primera = re.split(r"[\s,/.]+", n)[0]
    if "CANCEL" in n:
        return "Cancelado", None
    if re.match(r"^PEN[A-Z]{2,6}\b", n):                       # PENDIENTE y sus typos (PENTIENTE...)
        if "PAGO" in n and "ENTREGA" in n:
            return "Pendiente de pago y entrega", None
        if "ENTREGA" in n:
            return ("Pagado pendiente de entrega" if status == "Pagado" else "Pendiente de pago y entrega"), None
    if n.startswith("PAGADO") and "PEND" in n:
        return "Pagado pendiente de entrega", None
    if n.startswith("PAGADO") and "ENTREG" in n:
        return "Pagado y entregado", None
    if primera.startswith("ENV"):
        return "Enviado", None
    if re.fullmatch(r"ENT[A-Z]{3,8}", primera):            # ENTREGADO y sus typos
        if "PEND" in n:
            return "Entregado pendiente de pago", None
        extra = (n != primera)
        if status == "Pagado":
            return "Pagado y entregado", (t.strip() if extra else None)
        if status == "Pendiente":
            return "Entregado pendiente de pago", (t.strip() if extra else None)
        return None, t.strip()
    return None, t.strip()


def partir(texto):
    """Separa 'A Y B', 'A, B', 'A + B' sin romper montos como $11,000."""
    return [p.strip() for p in re.split(r"\s+Y\s+|\s*;\s*|\s*\+\s*|,\s+(?=[A-Z])", norm(texto)) if p.strip()]


def separar_vendedores(texto):
    """Devuelve (vendedores, notas). 'ROCIO / GABY', 'A, B Y C' se separan en varios."""
    notas, out = [], []
    t = norm(texto)
    if "NO SE CONSIDERA" in t:
        return [], [f"Vendedor (Excel): {texto.strip()}"]
    for p in [x.strip() for x in re.split(r"\s+Y\s+|\s*;\s*|\s*\+\s*|\s*/\s*|\s*,\s*", t) if x.strip()]:
        p = ALIAS_VENDEDOR.get(p, p)
        if p in NO_VENDEDORES:
            if p != "CANCELADO":
                notas.append(f"Vendedor (Excel): {p}")
            continue
        if p not in out:
            out.append(p)
    return out, notas


# ----------------------------------------------------------------------
# Pagos
# ----------------------------------------------------------------------
def _forma(p):
    n = re.sub(r"\.*", "", norm(p)).strip()
    for pref, nombre in FORMAS:
        if n.startswith(pref):
            return nombre
    return None


def _monto_inline(p):
    r = re.search(r"\$\s*([\d.,]+)", p)
    if not r:
        return None
    return num(r.group(1).rstrip(".-,"))


def canon_banco(b):
    """Unifica las variantes de nombre de banco/caja. Lo que no reconoce se deja tal cual."""
    b = re.sub(r"\s+", " ", b).strip()
    if re.fullmatch(r"TP[A-Z]*BB[A-Z]*", b):
        return "TPVBBVA"
    if b.startswith("IZETTLE"):
        return "IZETTLE / POS"
    if b == "EFECTIVO":
        return "CAJA"
    if b in ("TARJETA", "TRANSFERENCIA"):
        return None
    r = re.fullmatch(r"(?:BANCOMER )?(ERNESTO|ERNSTO|VIRIATO|CLAUDIA)(?: BANCOMER)?", b)
    if r and (b.startswith("BANCOMER") or b.endswith("BANCOMER")):
        return "BANCOMER " + ("ERNESTO" if r.group(1) == "ERNSTO" else r.group(1))
    if b == "HSBC":
        return "HSBC ERNESTO"
    if b == "BANORTE ENSALADA":
        return "BANORTE ENSALADA DE IDEAS"
    if b.startswith("DEPOSIT") and "ENSALADA" in b:
        return "DEPOSITO ENSALADA DE IDEAS"
    return b[:80]


def partir_bancos(texto):
    t = re.sub(r"IZETTLE\s*/\s*POS", "IZETTLE", norm(texto))
    out = []
    for p in [x.strip() for x in re.split(r"\s+Y\s+|\s*;\s*|\s*\+\s*|\s*/\s*|\s*,\s*", t) if x.strip()]:
        if p in ("PENDIENTE", "CANCELADO", "OBSEQUIO", "REGALO", "PAGADO"):
            continue
        c = canon_banco(p)
        if p in ("VIRIATO", "ERNESTO", "CLAUDIA") and out and out[-1].startswith("BANCOMER"):
            c = "BANCOMER " + p
        if c:
            out.append(c)
    return out


def parsear_pagos(forma_txt, banco_txt, total, anticipo, status, efectivo, fecha_pago):
    """Devuelve (lista de pagos, revisar: bool). 'revisar' = el reparto de montos es una estimación."""
    if status in ("Cancelado", "Obsequio") or norm(forma_txt) in SIN_PAGO:
        return [], False
    formas = [(_forma(p), _monto_inline(p)) for p in partir(forma_txt)]
    formas = [(f, m) for f, m in formas if f]
    if not formas:
        return [], False
    bancos = partir_bancos(banco_txt)

    base = total if status == "Pagado" else (anticipo or 0.0)
    revisar = False
    montos = [m for _, m in formas]
    if all(m is None for m in montos):
        if len(formas) == 1:
            montos = [base]
        else:
            montos = [0.0] * len(formas)
            idx_ef = next((i for i, (f, _) in enumerate(formas) if f == "Efectivo"), None)
            otros = [i for i in range(len(formas)) if i != idx_ef]
            resto = base
            if idx_ef is not None and efectivo and efectivo > 0:
                montos[idx_ef] = min(efectivo, base)
                resto = base - montos[idx_ef]
            if otros:
                montos[otros[0]] = round(resto, 2)       # el resto va a la primera forma que no es efectivo
            # Solo es una estimación si no se puede saber el reparto con la columna EFECTIVO
            revisar = not (idx_ef is not None and efectivo and efectivo > 0 and len(otros) == 1)
    pagos, pool = [], list(bancos)
    for (forma, _), monto in zip(formas, montos):
        banco = None
        if forma == "Efectivo":
            cajas = [b for b in pool if b.startswith("CAJA")]
            if cajas:
                banco = cajas[0]
                pool.remove(banco)
        elif forma == "Tarjeta":
            tpv = [b for b in pool if b in ("TPVBBVA", "IZETTLE / POS")]
            if tpv:
                banco = tpv[0]
                pool.remove(banco)
        if banco is None and pool:
            banco = pool.pop(0)
        if banco is None and len(bancos) == 1:
            banco = bancos[0]
        pagos.append({"forma_pago": forma, "banco_caja": banco, "monto": round(float(monto or 0), 2),
                      "fecha_pago": fecha_pago.isoformat() if fecha_pago else None})
    return pagos, revisar


# ----------------------------------------------------------------------
# Construcción de un pedido a partir de una fila
# ----------------------------------------------------------------------
def construir_pedido(f, anio, hoja):
    """f: dict {nombre de columna normalizado: valor}. Devuelve (pedido, vendedores, pagos, incidencias)
    o (None, None, None, incidencias) si la fila es un número reservado sin datos."""
    inc = []
    numero = num(f.get("NO. PEDIDO"))
    if numero is None:
        return None, None, None, inc
    numero = int(numero)

    cliente = txt(f.get("RAZON SOCIAL"))
    status_x = txt(f.get("STATUS"))
    total = num(f.get("TOTAL"))
    if not cliente and not status_x and not total:
        return None, None, None, [(numero, "SIN_DATOS", "Número reservado sin datos: se importará cuando se llene")]

    # --- fecha ---
    mes_col = MESES.get(norm(f.get("MES")))
    fecha, corregida = parse_fecha(f.get("FECHA"), anio)
    if fecha and fecha > dt.date.today():
        inc.append((numero, "FECHA_FUTURA", f"Fecha {fecha} es futura; se usa el mes de la columna MES"))
        fecha = None
    if fecha is None:
        if mes_col:
            fecha = dt.date(anio, mes_col, 1)
            inc.append((numero, "FECHA_ESTIMADA", f"Sin fecha válida ({txt(f.get('FECHA'))!r}); se usó 1/{mes_col}/{anio} (columna MES)"))
        else:
            inc.append((numero, "OMITIDO", "Sin fecha ni MES: no se puede importar"))
            return None, None, None, inc
    elif corregida:
        inc.append((numero, "FECHA_CORREGIDA", f"El año de {txt(f.get('FECHA'))!r} se corrigió a {anio}"))
    if mes_col and fecha.month != mes_col:
        inc.append((numero, "FECHA_MES_DISTINTO", f"FECHA {fecha} pero la columna MES dice {txt(f.get('MES'))}"))

    # --- catálogos ---
    status = mapear_status(status_x)
    estado, nota_estado = mapear_estado(txt(f.get("OBSERVACIONES")), status)
    if norm(cliente) == "CANCELADO" or estado == "Cancelado":
        estado = "Cancelado"
        status = "Cancelado"
    if not status:
        status = "Pendiente"
        inc.append((numero, "SIN_STATUS", "Sin status en el Excel; se dejó 'Pendiente'"))
    if not cliente:
        cliente = "SIN NOMBRE"
        inc.append((numero, "SIN_CLIENTE", "Sin cliente; se guardó como 'SIN NOMBRE'"))
    if nota_estado and estado is None:
        inc.append((numero, "ESTADO_REVISAR", f"Observaciones original sin reconocer: {nota_estado!r} (se guardó en observaciones)"))

    vendedores, notas_vend = separar_vendedores(txt(f.get("VENDEDOR")))
    if not vendedores and status not in ("Cancelado",) and (total or 0) > 0:
        inc.append((numero, "SIN_VENDEDOR", "Pedido con venta pero sin vendedor (no generará comisión)"))

    # --- importes ---
    costo = num(f.get("COSTO")) or 0.0
    iva = num(f.get("IVA")) or 0.0
    total = total or 0.0
    subtotal = num(f.get("SUBTOTAL"))
    con_iva = iva > 0
    if subtotal is None:
        subtotal = round(total / 1.16, 2) if con_iva else total
    anticipo = num(f.get("ANTICIPO")) or 0.0

    # --- fecha de pago ---
    fp_raw = f.get("FECHA PAGO")
    fecha_pago, _ = parse_fecha(fp_raw, anio)
    notas = list(notas_vend)
    if nota_estado:
        notas.append(nota_estado)
    if fecha_pago is None and txt(fp_raw) and norm(fp_raw) != "PENDIENTE":
        notas.append(f"Fecha(s) de pago: {txt(fp_raw)}")
    sub = txt(f.get("SUBCATEGORIA TIENDA ON LINE"))
    if sub:
        notas.append(f"Tienda online: {sub}")

    pagos, revisar = parsear_pagos(txt(f.get("FORMA DE PAGO")), txt(f.get("BANCO / CAJA")), total, anticipo,
                                   status, num(f.get("EFECTIVO")) or 0.0, fecha_pago)
    if revisar:
        inc.append((numero, "PAGO_REVISAR", f"Pago mixto sin montos: {txt(f.get('FORMA DE PAGO'))!r} / "
                                            f"{txt(f.get('BANCO / CAJA'))!r}; el reparto de montos es estimado"))

    pedido = {
        "numero_pedido": numero, "fecha": fecha.isoformat(), "cliente": cliente,
        "categoria_venta": mapear_categoria(txt(f.get("CATEGORIA DE VENTA"))),
        "estado_pedido": estado, "status": status,
        "observaciones": " | ".join(notas) or None,
        "email": txt(f.get("CORREO ELECTRONICO")) or None, "telefono": txt(f.get("TELEFONO")) or None,
        "no_factura": txt(f.get("NO. FACTURA")) or None,
        "con_iva": con_iva, "descuento_pct": 0, "costo_total": costo, "subtotal": subtotal, "iva": iva,
        "total": total, "anticipo": anticipo,
        "efectivo": num(f.get("EFECTIVO")) or 0.0,
        "facturado_con_iva": num(f.get("FACTURADO CON IVA")) or 0.0,
        "facturado_sin_iva": num(f.get("FACTURADO SIN IVA")) or 0.0,
        "fecha_pago": fecha_pago.isoformat() if fecha_pago else None,
        "modelos_texto": txt(f.get("OBSERVACIONES.1")) or None,
        "origen": "historico", "created_by": USUARIO_IMPORTACION,
    }
    return pedido, vendedores, pagos, inc


# ----------------------------------------------------------------------
# Lectura de los Excel
# ----------------------------------------------------------------------
def leer_hoja(archivo, hoja):
    for engine in ("calamine", "openpyxl"):
        try:
            raw = pd.read_excel(archivo, sheet_name=hoja, header=None, engine=engine)
            break
        except Exception:
            raw = None
    if raw is None:
        return None
    for h in range(min(10, len(raw))):
        if any(norm(c) == "NO. PEDIDO" for c in raw.iloc[h].tolist()):
            df = pd.read_excel(archivo, sheet_name=hoja, header=h, engine=engine)
            df.columns = [norm(c) for c in df.columns]
            return df
    return None


def hojas_ventas(carpeta, anios):
    """[(archivo, hoja, año)] para las hojas 'CONSECUTIVO VENTAS AAAA'."""
    encontradas = []
    for archivo in sorted(glob.glob(os.path.join(carpeta, "*.xlsx"))):
        if os.path.basename(archivo).startswith("~$"):
            continue
        try:
            nombres = pd.ExcelFile(archivo).sheet_names
        except Exception as ex:
            print(f"⚠️ No se pudo abrir {archivo}: {ex}")
            continue
        for nombre in nombres:
            r = re.fullmatch(r"\s*CONSECUTIVO VENTAS (\d{4})\s*", nombre, flags=re.I)
            if r and int(r.group(1)) in anios:
                encontradas.append((archivo, nombre, int(r.group(1))))
    return encontradas


def procesar_hoja(archivo, hoja, anio, hasta=None):
    df = leer_hoja(archivo, hoja)
    if df is None:
        print(f"   ⚠️ No se encontró la fila de encabezados en {hoja}")
        return [], []
    registros, incidencias, vistos = [], [], set()
    for _, fila in df.iterrows():
        pedido, vend, pagos, inc = construir_pedido(fila.to_dict(), anio, hoja)
        incidencias += [(hoja,) + i for i in inc]
        if pedido is None:
            continue
        n = pedido["numero_pedido"]
        if hasta is not None and n > hasta:
            continue
        if n in vistos:
            incidencias.append((hoja, n, "DUPLICADO_EXCEL", "Número repetido en el Excel; se usó la primera fila"))
            continue
        vistos.add(n)
        registros.append((pedido, vend, pagos))
    return registros, incidencias


# ----------------------------------------------------------------------
# Supabase
# ----------------------------------------------------------------------
def cargar_secrets():
    import toml
    ruta = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(ruta):
        s = toml.load(ruta)
        return s.get("SUPABASE_URL"), s.get("SUPABASE_KEY")
    return None, None


def numeros_existentes(sb):
    res, desde = {}, 0
    while True:
        filas = (sb.table("pedidos").select("numero_pedido,origen").order("numero_pedido")
                 .range(desde, desde + 999).execute().data)
        for f in filas:
            res[int(f["numero_pedido"])] = f.get("origen")
        if len(filas) < 1000:
            return res
        desde += 1000


def subir(sb, registros):
    """Inserta pedidos + vendedores + pagos por lotes. Devuelve (insertados, errores)."""
    ok, errores = 0, []
    for i in range(0, len(registros), TAMANO_LOTE):
        lote = registros[i:i + TAMANO_LOTE]
        exitosos = []
        try:
            sb.table("pedidos").insert([r[0] for r in lote]).execute()
            exitosos = lote
        except Exception as ex_lote:
            print(f"\n   ⚠️ Falló un lote ({ex_lote}); reintentando uno por uno...")
            for r in lote:
                try:
                    sb.table("pedidos").insert(r[0]).execute()
                    exitosos.append(r)
                except Exception as ex:
                    errores.append((r[0]["numero_pedido"], str(ex)))
        vend = [{"numero_pedido": p["numero_pedido"], "vendedor": v} for p, vs, _ in exitosos for v in vs]
        pag = [{**g, "numero_pedido": p["numero_pedido"]} for p, _, gs in exitosos for g in gs]
        try:
            if vend:
                sb.table("pedido_vendedores").insert(vend).execute()
            if pag:
                sb.table("pedido_pagos").insert(pag).execute()
        except Exception as ex:
            errores.append((f"lote {i // TAMANO_LOTE + 1}", f"vendedores/pagos: {ex}"))
        ok += len(exitosos)
    return ok, errores


def importar(prueba=False, anios=(2025, 2026), hasta=None, actualizar=False, carpeta=CARPETA):
    hojas = hojas_ventas(carpeta, set(anios))
    if not hojas:
        print(f"⚠️ No se encontraron hojas 'CONSECUTIVO VENTAS {'/'.join(map(str, anios))}' en la carpeta '{carpeta}'.")
        return
    sb = None
    existentes = {}
    if not prueba:
        from supabase import create_client
        url, key = cargar_secrets()
        if not url or not key:
            print("❌ Faltan SUPABASE_URL o SUPABASE_KEY en .streamlit/secrets.toml")
            return
        sb = create_client(url, key)
        existentes = numeros_existentes(sb)
        print(f"📚 En la base ya hay {len(existentes)} pedidos "
              f"({sum(1 for o in existentes.values() if o == 'app')} creados desde la app).")

    print(f"📦 Leyendo {len(hojas)} hoja(s)" + (" [PRUEBA: no se sube nada]" if prueba else "") + "...")
    todas, incidencias = [], []
    for archivo, hoja, anio in hojas:
        regs, inc = procesar_hoja(archivo, hoja, anio, hasta)
        print(f"   {os.path.basename(archivo)} / {hoja}: {len(regs)} pedidos con datos")
        todas += regs
        incidencias += inc

    nuevos, a_reemplazar, protegidos, ya = [], [], [], 0
    for reg in todas:
        n = reg[0]["numero_pedido"]
        if n in existentes:
            if actualizar and existentes[n] == "historico":
                a_reemplazar.append(reg)
            elif existentes[n] != "historico":
                protegidos.append(n)
            else:
                ya += 1
        else:
            nuevos.append(reg)

    pendientes = [i for i in incidencias if i[2] == "SIN_DATOS"]
    resumen = {}
    for i in incidencias:
        resumen[i[2]] = resumen.get(i[2], 0) + 1

    print("\n=== RESUMEN ===")
    print(f"Pedidos con datos en los Excel: {len(todas)}")
    if not prueba:
        print(f"  Nuevos a importar: {len(nuevos)} | ya importados (se dejan igual): {ya} | "
              f"a actualizar: {len(a_reemplazar)} | protegidos (creados en la app): {len(protegidos)}")
    print(f"Números reservados todavía sin datos: {len(pendientes)}")
    if pendientes:
        nums = sorted(i[1] for i in pendientes)
        print(f"  del {nums[0]} al {nums[-1]}")
    for tipo, c in sorted(resumen.items()):
        if tipo != "SIN_DATOS":
            print(f"  {tipo}: {c}")
    todos_vend = sorted({v for r in todas for v in r[1]})
    print(f"Vendedores encontrados: {', '.join(todos_vend)}")

    with open("reporte_importacion.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["hoja", "pedido", "tipo", "detalle"])
        w.writerows(i for i in incidencias if i[2] != "SIN_DATOS")
    print("📝 Reporte guardado en reporte_importacion.csv")

    if prueba:
        for pedido, vend, pagos in todas[:3] + todas[-2:]:
            print(f"   ej. {pedido['numero_pedido']}: {pedido['fecha']} {pedido['cliente']} | {vend} | total={pedido['total']:,.2f} "
                  f"| {pedido['estado_pedido']} / {pedido['status']} | pagos={[(p['forma_pago'], p['banco_caja'], p['monto']) for p in pagos]}")
        return

    if protegidos:
        print(f"⚠️ Estos números ya existen como pedidos de la app y NO se tocaron: {sorted(protegidos)[:15]}")
    if a_reemplazar:
        nums = [r[0]["numero_pedido"] for r in a_reemplazar]
        for i in range(0, len(nums), 200):
            sb.table("pedidos").delete().in_("numero_pedido", nums[i:i + 200]).eq("origen", "historico").execute()
        nuevos += a_reemplazar
    nuevos.sort(key=lambda r: r[0]["numero_pedido"])
    ok, errores = subir(sb, nuevos)
    print(f"\n🎉 Importados: {ok} pedidos.")
    for e in errores[:20]:
        print(f"   ❌ {e[0]}: {e[1]}")

    mayor = max(list(existentes) + [r[0]["numero_pedido"] for r in nuevos] or [0])
    print("\nCUANDO ya no vayas a llenar más pedidos en el Excel (el día del corte), ajusta el contador de la app")
    print("pegando esto en Supabase > SQL Editor:")
    print(f"   select setval(pg_get_serial_sequence('pedidos', 'numero_pedido'), {mayor});")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Importa ventas desde los Excel a Supabase")
    ap.add_argument("--prueba", action="store_true", help="No sube nada; solo revisa y genera el reporte")
    ap.add_argument("--actualizar", action="store_true", help="Re-sincroniza pedidos ya importados (origen historico)")
    ap.add_argument("--anios", nargs="+", type=int, default=[2025, 2026])
    ap.add_argument("--hasta", type=int, default=None, help="Número de pedido máximo a importar")
    ap.add_argument("--carpeta", default=CARPETA)
    a = ap.parse_args()
    importar(prueba=a.prueba, anios=tuple(a.anios), hasta=a.hasta, actualizar=a.actualizar, carpeta=a.carpeta)

