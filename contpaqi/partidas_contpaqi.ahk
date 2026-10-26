; ======================================================================
;  Escribe las partidas en CONTPAQi Comercial desde el portapapeles
;  (AutoHotkey v2 — https://www.autohotkey.com)
;
;  Cómo se usa:
;   1. En la app, pestaña Ventas › Por facturar, elige el/los pedido(s)
;      y toca «Copiar tabla completa».
;   2. En CONTPAQi abre la factura y deja el cursor en la PRIMERA celda
;      del primer renglón vacío de las partidas.
;   3. Presiona  Ctrl + Alt + P.  Revisa el resumen y confirma.
;
;  MODO PRÁCTICA: abre el Bloc de notas y haz lo mismo (Ctrl + Alt + P).
;  Escribe las partidas ahí para que veas qué haría, sin tocar CONTPAQi.
;   4. No toques el teclado ni el mouse mientras escribe.
;      Para detenerlo en cualquier momento:  Esc.
; ======================================================================
#Requires AutoHotkey v2.0
#SingleInstance Force

; ---------------------------- CONFIGURACIÓN ----------------------------
; Orden en que se llena cada renglón en CONTPAQi. Pasos válidos:
;   codigo, descripcion, cantidad, unidad, precio, importe   → escribe ese dato
;   TAB, ENTER, F2, ABAJO, DERECHA, IZQUIERDA, INICIO        → oprime esa tecla
;   ESPERA                                                   → pausa extra
;   BORRAR                                                   → selecciona todo el campo y lo borra
; Ejemplo típico (código → nombre → cantidad → precio → siguiente renglón):
ORDEN := "codigo,TAB,ESPERA,BORRAR,descripcion,TAB,cantidad,TAB,TAB,precio,TAB,ENTER"

; Código del producto de CONTPAQi según la descripción de la partida.
; Las partidas agrupadas salen con el nombre del concepto en mayúsculas.
CODIGOS := Map(
    "MOBILIARIO DE OFICINA",    "MOB01",     ; ← cambia por tu código real
    "ACCESORIOS DE DECORACIÓN", "ACC01"      ; ← cambia por tu código real
)
; Código para las partidas desglosadas (una pieza por renglón)
CODIGO_DESGLOSE := "ANT01"                   ; ← cambia por tu código real

; Pausa (milisegundos) después de cada paso. Súbela si CONTPAQi se atrasa.
PAUSA := 250
PAUSA_EXTRA := 700        ; la que usa el paso ESPERA (p. ej. después del código)

; Solo escribe si la ventana activa contiene este texto en el título
TITULO_VENTANA := "CONTPAQ"
; Ventanas donde se puede practicar sin riesgo (ahí el paso BORRAR no hace nada)
VENTANAS_PRACTICA := ["Bloc de notas", "Notepad"]
; ------------------------------------------------------------------------

global detener := false
global escribiendo := false
global practica := false

^!p:: EscribirPartidas()

; Esc solo se intercepta mientras se está escribiendo
#HotIf escribiendo
Esc:: {
    global detener
    detener := true
}
#HotIf

EscribirPartidas() {
    global detener
    detener := false

    partidas := LeerPortapapeles()
    if partidas.Length = 0 {
        MsgBox("No encontré partidas en el portapapeles.`n`nEn la app toca «Copiar tabla completa» y vuelve a intentar.",
               "Partidas CONTPAQi", "Icon!")
        return
    }

    ; Resumen para confirmar
    total := 0.0
    lista := ""
    for i, p in partidas {
        total += p["cantidad"] * p["precio"]
        if i <= 12
            lista .= p["cantidad"] " × " SubStr(p["descripcion"], 1, 45) "   $" p["precio"] "`n"
    }
    if partidas.Length > 12
        lista .= "… y " (partidas.Length - 12) " más`n"
    r := MsgBox(partidas.Length " partida(s), subtotal aprox. $" Format("{:.2f}", total) " sin IVA:`n`n" lista
              . "`nDeja el cursor en la primera celda del renglón vacío de CONTPAQi y toca Aceptar."
              . "`nTendrás 3 segundos para volver a la ventana. Esc detiene.",
              "Partidas CONTPAQi", "OKCancel Iconi")
    if r != "OK"
        return

    Sleep(3000)
    global practica
    titulo := WinGetTitle("A")
    practica := false
    for v in VENTANAS_PRACTICA
        if InStr(titulo, v)
            practica := true
    if !practica && !InStr(titulo, TITULO_VENTANA) {
        MsgBox("La ventana activa no es CONTPAQi (ni el Bloc de notas para practicar). No escribí nada.",
               "Partidas CONTPAQi", "Icon!")
        return
    }
    if practica
        TrayTip("Modo práctica: escribiendo en el Bloc de notas.", "Partidas CONTPAQi")

    global escribiendo
    escribiendo := true
    try EscribirTodo(partidas)
    finally escribiendo := false
}

EscribirTodo(partidas) {
    global detener
    pasos := StrSplit(ORDEN, ",", " `t")
    for p in partidas {
        for paso in pasos {
            if detener {
                MsgBox("Detenido. Revisa el último renglón en CONTPAQi.", "Partidas CONTPAQi", "Icon!")
                return
            }
            HacerPaso(paso, p)
            Sleep(PAUSA)
        }
    }
    TrayTip("Listo: " partidas.Length " partida(s) escritas. Revisa los totales antes de timbrar.", "Partidas CONTPAQi")
}

HacerPaso(paso, p) {
    switch StrUpper(paso) {
        case "TAB":       Send("{Tab}")
        case "ENTER":     Send("{Enter}")
        case "F2":        Send("{F2}")
        case "ABAJO":     Send("{Down}")
        case "DERECHA":   Send("{Right}")
        case "IZQUIERDA": Send("{Left}")
        case "INICIO":    Send("{Home}")
        case "ESPERA":    Sleep(PAUSA_EXTRA)
        case "BORRAR":
            if !practica          ; en el Bloc de notas no se borra nada
                Send("^a{Delete}")
        case "CODIGO":    SendText(p["codigo"])
        case "DESCRIPCION": SendText(p["descripcion"])
        case "CANTIDAD":  SendText(p["cantidad"])
        case "UNIDAD":    SendText(p["unidad"])
        case "PRECIO":    SendText(p["precio_txt"])
        case "IMPORTE":   SendText(p["importe_txt"])
        default:          MsgBox("Paso desconocido en ORDEN: " paso, "Partidas CONTPAQi", "Icon!")
    }
}

; Lee la tabla copiada desde la app (columnas separadas por tabulador):
; Cantidad | Descripción | Unidad | Precio unitario | Importe | Impuesto
LeerPortapapeles() {
    partidas := []
    texto := A_Clipboard
    for linea in StrSplit(texto, "`n", "`r") {
        c := StrSplit(linea, "`t")
        if c.Length < 5 || !IsNumber(Trim(c[1]))       ; salta encabezado y renglones vacíos
            continue
        desc := Trim(c[2])
        p := Map()
        p["cantidad"]    := Trim(c[1]) + 0
        p["descripcion"] := desc
        p["unidad"]      := Trim(c[3])
        p["precio_txt"]  := Trim(c[4])
        p["precio"]      := Trim(c[4]) + 0
        p["importe_txt"] := Trim(c[5])
        p["codigo"]      := CODIGOS.Has(desc) ? CODIGOS[desc] : CODIGO_DESGLOSE
        partidas.Push(p)
    }
    return partidas
}
