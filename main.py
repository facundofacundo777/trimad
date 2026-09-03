"""Trimetal Admin Bot — carga de compras y ventas por foto.

Flujo: foto/PDF -> IA lee -> QR verifica -> vista previa -> Confirmar -> fila en Compras/Ventas
+ foto en Drive + registro en Log. Con Deshacer por 10 minutos."""
import io
import logging
import uuid
from datetime import datetime, timedelta

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config as C
from extractor import extraer
from gsheets import Google, cuit_con_guiones, solo_digitos

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

G = Google(C.GOOGLE_CREDENTIALS_JSON, C.SHEET_ID, C.DRIVE_FOLDER_ID)
IA = anthropic.Anthropic(api_key=C.ANTHROPIC_API_KEY)

OPS: dict[str, dict] = {}      # operaciones en curso / recientes, por id
ESPERANDO: dict[tuple, dict] = {}   # (chat_id, user_id) -> {"op": id, "campo": nombre}

CAMPOS_EDITABLES = [
    ("fecha", "Fecha"), ("punto_venta", "Pto Vta"), ("numero", "Numero"),
    ("neto", "Neto"), ("iva", "IVA"), ("alicuota", "Alicuota"),
    ("percepcion_iva", "Perc. IVA"), ("percepcion_iibb", "Perc. IIBB"),
    ("otros_tributos", "Otros trib."), ("total", "Total"), ("razon_social", "Nombre"),
]


# =====================================================================
# utilidades
# =====================================================================
def autorizado(update: Update) -> bool:
    u = update.effective_user.id
    ch = update.effective_chat
    if u not in C.ALLOWED_USER_IDS:
        return False
    if ch.type == "private":
        return True
    return ch.id in C.ALLOWED_CHAT_IDS


def es_admin(update: Update) -> bool:
    return update.effective_user.id in C.ADMIN_USER_IDS


def pesos(x: float) -> str:
    s = f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-$" if x < 0 else "$") + s


def fecha_ar(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return iso or "?"


def teclado(botones):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in fila] for fila in botones])


# =====================================================================
# armado de la operacion
# =====================================================================
def preparar_operacion(d: dict, user, contenido: bytes, mime: str, ext: str) -> dict:
    es_venta = d["emisor"]["cuit"] == C.MI_CUIT
    contraparte = d["receptor"] if es_venta else d["emisor"]
    hoja = C.HOJA_VENTAS if es_venta else C.HOJA_COMPRAS

    avisos = []
    if not d["qr"]:
        avisos.append("⚠ Sin QR: revisá los números antes de confirmar")
    if d.get("confianza") == "baja":
        avisos.append("⚠ La lectura fue difícil (imagen borrosa o cortada)")
    if not d["cuadra"]:
        avisos.append(f"⚠ No cierra: la suma da {pesos(d['suma_calculada'])} y el total {pesos(d['total'])}")
    if not es_venta and d["receptor"]["cuit"] and d["receptor"]["cuit"] != C.MI_CUIT:
        avisos.append("⚠ El receptor no es tu CUIT: ¿es una foto de otra empresa?")
    if len(d["iva"]) > 1:
        avisos.append(f"ℹ {len(d['iva'])} alícuotas: se van a cargar {len(d['iva'])} filas")
    if d["es_nc"]:
        avisos.append("ℹ Nota de crédito: se carga en negativo")
    if d["otros_tributos"] > 0 and d["otros_tributos"] > 0.05 * sum(i["neto"] for i in d["iva"]):
        avisos.append("ℹ Otros tributos altos: probablemente impuesto interno, no percepción")

    # duplicado
    dup = None
    if d["punto_venta"] and d["numero"]:
        comp = f"{d['punto_venta']:04d}-{d['numero']:08d}"
        filas = G.get(f"{hoja}!A{C.FILA_INICIO}:I{C.FILA_FIN}")
        for f in filas:
            f += [""] * (9 - len(f))
            if f[8] == comp and (solo_digitos(f[4]) == contraparte["cuit"] or f[3] == (contraparte.get("razon_social") or "")):
                dup = f[0]
                break

    # entidad
    ent_ok = False
    ent_nombre = contraparte.get("razon_social") or ""
    if contraparte["cuit"]:
        ents = G.get(f"{C.HOJA_ENTIDADES}!B{C.FILA_INICIO}:C{C.ENT_FILA_FIN}")
        for e in ents:
            e += [""] * (2 - len(e))
            if solo_digitos(e[1]) == contraparte["cuit"]:
                ent_ok, ent_nombre = True, e[0]
                break

    op_id = f"OP-{uuid.uuid4().hex[:6].upper()}"
    op = {
        "id": op_id, "datos": d, "es_venta": es_venta, "hoja": hoja,
        "contraparte_cuit": contraparte["cuit"], "contraparte_nombre": ent_nombre,
        "entidad_existe": ent_ok, "crear_entidad": False, "duplicado": dup, "avisos": avisos,
        "rubro": "", "user_id": user.id, "user_nombre": user.full_name,
        "archivo": contenido, "mime": mime, "ext": ext,
        "creado": datetime.now(), "estado": "previa", "escrito": [],
    }
    OPS[op_id] = op
    return op


def texto_previa(op: dict) -> str:
    d = op["datos"]
    tipo = "VENTA" if op["es_venta"] else "COMPRA"
    comp = f"{d['punto_venta'] or '?':>04}-{d['numero'] or '?':>08}" if isinstance(d['punto_venta'], int) else "?"
    chk = " ✓QR" if d["qr"] else ""
    lineas = [
        f"*{tipo}* · {d['tipo_texto']} {comp} · {fecha_ar(d['fecha'])}{chk}",
        f"{op['contraparte_nombre'] or '(sin nombre)'} · CUIT {cuit_con_guiones(op['contraparte_cuit']) or '?'}",
        "",
    ]
    for i in d["iva"]:
        al = f"{i['alicuota']:g}%"
        lineas.append(f"Neto {pesos(i['neto'])} · IVA {al} {pesos(i['iva'])}")
    extras = []
    if d["percepcion_iva"]:
        extras.append(f"Perc. IVA {pesos(d['percepcion_iva'])}")
    if d["percepcion_iibb"]:
        extras.append(f"Perc. IIBB {pesos(d['percepcion_iibb'])}")
    if d["otros_tributos"]:
        extras.append(f"Otros trib. {pesos(d['otros_tributos'])}")
    if d["no_gravado"] or d["exento"]:
        extras.append(f"No grav./exento {pesos(d['no_gravado'] + d['exento'])}")
    if extras:
        lineas.append(" · ".join(extras))
    lineas.append(f"*Total {pesos(d['total'])}*" + (" ✓ cierra" if d["cuadra"] else ""))
    if op["rubro"]:
        lineas.append(f"Rubro: {op['rubro']}")
    if op["duplicado"]:
        lineas += ["", f"🚫 *Ya está cargada como {op['duplicado']}*"]
    if not op["entidad_existe"]:
        lineas += ["", f"🆕 {'Cliente' if op['es_venta'] else 'Proveedor'} nuevo — se va a dar de alta en Entidades"]
    if op["avisos"]:
        lineas += [""] + op["avisos"]
    lineas += ["", f"`{op['id']}`"]
    return "\n".join(lineas)


def teclado_previa(op: dict):
    if op["duplicado"]:
        return teclado([[("Cargar igual", f"cf:{op['id']}"), ("❌ Cancelar", f"cx:{op['id']}")]])
    return teclado([[("✅ Confirmar", f"cf:{op['id']}"), ("✏️ Corregir", f"ed:{op['id']}"), ("❌ Cancelar", f"cx:{op['id']}")]])


# =====================================================================
# escritura en la planilla
# =====================================================================
def escribir(op: dict) -> str:
    d = op["datos"]
    hoja = op["hoja"]
    sg = -1 if d["es_nc"] else 1
    escrito = []

    # 1) entidad nueva
    if not op["entidad_existe"] and op["contraparte_cuit"]:
        fila = G.primera_fila_vacia(C.HOJA_ENTIDADES, "B", C.FILA_INICIO, C.ENT_FILA_FIN)
        if fila:
            tipo = "Cliente" if op["es_venta"] else "Proveedor"
            rng = f"{C.HOJA_ENTIDADES}!A{fila}:D{fila}"
            G.batch_update([{"range": rng, "values": [[tipo, op["contraparte_nombre"],
                                                        cuit_con_guiones(op["contraparte_cuit"]),
                                                        "Responsable Inscripto"]]}])
            escrito.append(rng)

    # 2) foto a Drive
    fecha = d["fecha"] or datetime.now().strftime("%Y-%m-%d")
    comp = f"{d['punto_venta'] or 0:04d}-{d['numero'] or 0:08d}"
    nombre_arch = f"{fecha}_{'V' if op['es_venta'] else 'C'}_{(op['contraparte_nombre'] or 'sin-nombre')[:30].replace('/', '-')}_{comp}.{op['ext']}"
    link = ""
    try:
        r = G.subir_archivo(nombre_arch, op["archivo"], op["mime"])
        link = r.get("webViewLink", "")
    except Exception as e:
        log.warning("No se pudo subir a Drive: %s", e)

    # 3) filas en Compras/Ventas (una por alicuota)
    ids = []
    obs_base = f"Cargado por bot · {op['id']}"
    for n, it in enumerate(d["iva"]):
        fila = G.primera_fila_vacia(hoja, "B", C.FILA_INICIO, C.FILA_FIN)
        if not fila:
            raise RuntimeError(f"La hoja {hoja} no tiene filas libres con fórmulas")
        obs = obs_base + (f" · fila {n+1}/{len(d['iva'])}" if len(d["iva"]) > 1 else "")
        if d.get("notas"):
            obs += f" · {d['notas']}"
        if not d["qr"]:
            obs += " · sin QR"
        perc_iva = sg * d["percepcion_iva"] if n == 0 else ""
        perc_iibb = sg * d["percepcion_iibb"] if n == 0 else ""
        otros = sg * (d["otros_tributos"] + d["no_gravado"] + d["exento"]) if n == 0 else ""
        data = [
            {"range": f"{hoja}!B{fila}", "values": [[fecha_ar(fecha)]]},
            {"range": f"{hoja}!D{fila}", "values": [[op["contraparte_nombre"]]]},
            {"range": f"{hoja}!F{fila}:H{fila}", "values": [[d["tipo_texto"], d["punto_venta"] or "", d["numero"] or ""]]},
            {"range": f"{hoja}!J{fila}:L{fila}", "values": [[op["rubro"], round(sg * it["neto"], 2), it["alicuota"] / 100.0]]},
            {"range": f"{hoja}!N{fila}:P{fila}", "values": [[perc_iva, perc_iibb, otros]]},
            {"range": f"{hoja}!X{fila}:Y{fila}", "values": [[link, obs]]},
        ]
        G.batch_update(data)
        escrito += [x["range"] for x in data]
        idv = G.get(f"{hoja}!A{fila}")
        ids.append(idv[0][0] if idv and idv[0] else f"fila {fila}")

    # 4) log
    G.asegurar_hoja(C.HOJA_LOG, ["Fecha", "Usuario", "Operacion", "Hoja", "IDs", "Contraparte", "Total", "Foto", "Estado"])
    G.append(C.HOJA_LOG, [datetime.now().strftime("%d/%m/%Y %H:%M"), op["user_nombre"], op["id"], hoja,
                          ", ".join(ids), op["contraparte_nombre"], sg * d["total"], link, "CARGADA"])

    op["escrito"] = escrito
    op["ids"] = ids
    op["estado"] = "cargada"
    op["archivo"] = None   # liberar memoria
    return ", ".join(ids)


def deshacer(op: dict) -> bool:
    if op.get("estado") != "cargada" or not op.get("escrito"):
        return False
    G.batch_clear(op["escrito"])
    G.append(C.HOJA_LOG, [datetime.now().strftime("%d/%m/%Y %H:%M"), "", op["id"], op["hoja"],
                          ", ".join(op.get("ids", [])), op["contraparte_nombre"], "", "", "DESHECHA"])
    op["estado"] = "deshecha"
    return True


# =====================================================================
# handlers
# =====================================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await update.message.reply_text("Mandá la foto o el PDF del comprobante y lo cargo. /id para ver tu ID.")


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat: {update.effective_chat.id}\nUsuario: {update.effective_user.id}")


async def cmd_ultimas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    filas = G.get(f"{C.HOJA_LOG}!A2:I")
    if not filas:
        await update.message.reply_text("Todavía no hay cargas.")
        return
    lineas = []
    for f in filas[-10:]:
        f += [""] * (9 - len(f))
        lineas.append(f"{f[0]} · {f[1]} · {f[2]} · {f[3]} {f[4]} · {f[5][:25]} · {f[8]}")
    await update.message.reply_text("\n".join(lineas))


async def cmd_deshacer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update) or not es_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /deshacer OP-XXXXXX")
        return
    op = OPS.get(ctx.args[0].upper())
    if not op:
        await update.message.reply_text("No tengo esa operación en memoria (solo guardo las de esta sesión).")
        return
    ok = deshacer(op)
    await update.message.reply_text(f"{op['id']} deshecha." if ok else "Esa operación no se puede deshacer.")


async def recibir_archivo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    msg = update.message
    if msg.photo:
        tf = await msg.photo[-1].get_file()
        mime, ext = "image/jpeg", "jpg"
    elif msg.document and msg.document.mime_type in ("application/pdf", "image/jpeg", "image/png"):
        tf = await msg.document.get_file()
        mime = msg.document.mime_type
        ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[mime]
    else:
        return
    aviso = await msg.reply_text("Leyendo el comprobante…")
    try:
        contenido = bytes(await tf.download_as_bytearray())
        d = extraer(IA, C.MODEL, contenido, mime)
        op = preparar_operacion(d, update.effective_user, contenido, mime, ext)
        await aviso.edit_text(texto_previa(op), parse_mode="Markdown", reply_markup=teclado_previa(op))
    except Exception as e:
        log.exception("Error leyendo comprobante")
        await aviso.edit_text(f"No pude leer el comprobante: {e}\nProbá con una foto más nítida o el PDF.")


async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_user.id not in C.ALLOWED_USER_IDS:
        await q.answer("No estás habilitado.", show_alert=True)
        return
    accion, _, resto = q.data.partition(":")
    op_id, _, extra = resto.partition(":")
    op = OPS.get(op_id)
    if not op:
        await q.answer("Esa operación ya no está en memoria.", show_alert=True)
        return

    if accion == "cx":
        op["estado"] = "cancelada"
        await q.edit_message_text(f"Cancelada {op['id']}.")
        await q.answer()

    elif accion == "cf":
        if op["estado"] != "previa":
            await q.answer("Ya se procesó.")
            return
        rubros = []
        if not op["es_venta"] and not op["rubro"]:
            try:
                rubros = [r[0] for r in G.get(f"{C.HOJA_CONFIG}!E4:E40") if r and r[0]]
            except Exception:
                rubros = []
        if rubros:
            filas = [[(r, f"ru:{op_id}:{i}")] for i, r in enumerate(rubros[:12])]
            filas.append([("Sin rubro", f"ru:{op_id}:-1")])
            op["_rubros"] = rubros
            await q.edit_message_text("¿Rubro?", reply_markup=teclado(filas))
            await q.answer()
        else:
            await confirmar(q, op)

    elif accion == "ru":
        i = int(extra)
        op["rubro"] = op.get("_rubros", [])[i] if i >= 0 else ""
        await confirmar(q, op)

    elif accion == "ed":
        filas = [[(t, f"fx:{op_id}:{k}")] for k, t in CAMPOS_EDITABLES]
        filas.append([("↩ Volver", f"bk:{op_id}")])
        await q.edit_message_text("¿Qué campo corregís?", reply_markup=teclado(filas))
        await q.answer()

    elif accion == "fx":
        ESPERANDO[(update.effective_chat.id, update.effective_user.id)] = {"op": op_id, "campo": extra}
        nombre = dict(CAMPOS_EDITABLES).get(extra, extra)
        await q.edit_message_text(f"Mandá el nuevo valor de *{nombre}* (para fecha usá DD/MM/AAAA, para alícuota 21 o 10.5).", parse_mode="Markdown")
        await q.answer()

    elif accion == "bk":
        await q.edit_message_text(texto_previa(op), parse_mode="Markdown", reply_markup=teclado_previa(op))
        await q.answer()

    elif accion == "un":
        if not es_admin(update) and update.effective_user.id != op["user_id"]:
            await q.answer("Solo quien la cargó o un admin puede deshacer.", show_alert=True)
            return
        if datetime.now() - op["creado"] > timedelta(minutes=C.UNDO_MINUTOS):
            await q.answer(f"Pasaron más de {C.UNDO_MINUTOS} minutos. Usá /deshacer {op_id}.", show_alert=True)
            return
        ok = deshacer(op)
        await q.edit_message_text(f"{op['id']} deshecha." if ok else "No se pudo deshacer.")
        await q.answer()


async def confirmar(q, op: dict):
    await q.edit_message_text("Cargando…")
    try:
        ids = escribir(op)
        msg = f"✅ Cargada como *{ids}* en {op['hoja']}"
        if not op["entidad_existe"] and op["contraparte_cuit"]:
            msg += f"\n🆕 {op['contraparte_nombre']} dado de alta en Entidades"
        msg += f"\n`{op['id']}` · {op['user_nombre']} · {datetime.now():%H:%M}"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=teclado([[("↶ Deshacer", f"un:{op['id']}")]]))
    except Exception as e:
        log.exception("Error escribiendo")
        await q.edit_message_text(f"❌ No se pudo cargar: {e}")
    await q.answer()


async def recibir_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = (update.effective_chat.id, update.effective_user.id)
    esp = ESPERANDO.pop(key, None)
    if not esp:
        return
    op = OPS.get(esp["op"])
    if not op:
        return
    campo, valor = esp["campo"], update.message.text.strip()
    d = op["datos"]
    try:
        if campo == "fecha":
            d["fecha"] = datetime.strptime(valor, "%d/%m/%Y").strftime("%Y-%m-%d")
        elif campo in ("punto_venta", "numero"):
            d[campo] = int(solo_digitos(valor))
        elif campo == "razon_social":
            op["contraparte_nombre"] = valor
        elif campo == "neto":
            d["iva"][0]["neto"] = float(valor.replace(".", "").replace(",", "."))
        elif campo == "iva":
            d["iva"][0]["iva"] = float(valor.replace(".", "").replace(",", "."))
        elif campo == "alicuota":
            d["iva"][0]["alicuota"] = float(valor.replace(",", "."))
        else:
            d[campo] = float(valor.replace(".", "").replace(",", "."))
        suma = sum(i["neto"] + i["iva"] for i in d["iva"]) + d["no_gravado"] + d["exento"] \
            + d["percepcion_iva"] + d["percepcion_iibb"] + d["otros_tributos"]
        d["suma_calculada"] = round(suma, 2)
        d["cuadra"] = abs(suma - d["total"]) <= C.TOLERANCIA
        op["avisos"] = [a for a in op["avisos"] if not a.startswith("⚠ No cierra")]
        if not d["cuadra"]:
            op["avisos"].append(f"⚠ No cierra: la suma da {pesos(d['suma_calculada'])} y el total {pesos(d['total'])}")
    except Exception:
        await update.message.reply_text("No entendí el valor. Probá de nuevo desde Corregir.")
        return
    await update.message.reply_text(texto_previa(op), parse_mode="Markdown", reply_markup=teclado_previa(op))


def main():
    app = ApplicationBuilder().token(C.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ultimas", cmd_ultimas))
    app.add_handler(CommandHandler("deshacer", cmd_deshacer))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, recibir_archivo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto))
    log.info("Bot arrancando con service account %s", G.sa_email)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
