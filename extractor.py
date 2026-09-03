"""Lee un comprobante (foto o PDF) y devuelve sus datos estructurados.
1) Intenta leer el QR de ARCA localmente (no sale nada del servidor).
2) Manda la imagen a Claude para el desglose completo.
3) Cruza ambos: el QR manda sobre CUIT, tipo, punto de venta, numero, fecha y total."""
import base64
import json
import re
import anthropic

TIPOS = {
    1: "Factura A", 2: "Nota de Debito A", 3: "Nota de Credito A", 4: "Recibo A",
    6: "Factura B", 7: "Nota de Debito B", 8: "Nota de Credito B", 9: "Recibo B",
    11: "Factura C", 12: "Nota de Debito C", 13: "Nota de Credito C", 15: "Recibo C",
    51: "Factura M", 52: "Nota de Debito M", 53: "Nota de Credito M",
    63: "Liquidacion A", 64: "Liquidacion B", 81: "Tique Factura A", 82: "Tique Factura B",
    83: "Tique", 109: "Tique C", 110: "Tique Nota de Credito", 111: "Tique Factura C",
}
NOTAS_CREDITO = {3, 8, 13, 53, 110}

PROMPT = """Sos un asistente contable argentino. Te paso la imagen de un comprobante fiscal
(factura, nota de credito, ticket, liquidacion). Extrae los datos y devolve SOLO un JSON,
sin texto antes ni despues, con esta forma exacta:

{
  "codigo_tipo": <int, codigo AFIP del tipo: 1 Factura A, 6 Factura B, 11 Factura C, 3 NC A,
                  8 NC B, 13 NC C, 2 ND A, 7 ND B, 63 Liquidacion A, 81 Tique Factura A,
                  15 Recibo C, 83 Tique, 109 Tique C; null si no se distingue>,
  "punto_venta": <int o null>,
  "numero": <int o null>,
  "fecha": "<YYYY-MM-DD o null>",
  "emisor": {"cuit": "<11 digitos sin guiones o null>", "razon_social": "<texto o null>"},
  "receptor": {"cuit": "<11 digitos o null>", "razon_social": "<texto o null>"},
  "iva": [ {"alicuota": <21|10.5|27|5|2.5|0>, "neto": <numero>, "iva": <numero>} ],
  "no_gravado": <numero>,
  "exento": <numero>,
  "percepcion_iva": <numero>,
  "percepcion_iibb": <numero>,
  "otros_tributos": <numero>,
  "total": <numero>,
  "cae": "<texto o null>",
  "confianza": "<alta|media|baja>",
  "notas": "<algo que te haya llamado la atencion, o cadena vacia>"
}

Reglas:
- Numeros con punto decimal, sin separador de miles, siempre positivos (aunque sea nota de credito).
- En "iva" una entrada por cada alicuota que aparezca con importe distinto de cero.
- Si la factura es B o C y no discrimina IVA, dejalo con una sola entrada alicuota 0, neto = total menos otros tributos, iva 0.
- percepcion_iva / percepcion_iibb solo si el comprobante las nombra explicitamente asi.
  Impuestos internos, impuesto a los combustibles, tasas municipales, sellos: van en otros_tributos.
- Si no podes leer un campo, null. No inventes.
- confianza baja si la imagen esta borrosa, cortada o hay numeros que no se leen bien."""


def _num(x):
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def leer_qr(imagen: bytes) -> dict | None:
    """Decodifica el QR de ARCA si esta presente. Devuelve None si no hay QR o falla."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(imagen, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        if not data or "afip.gob.ar/fe/qr" not in data:
            return None
        p = re.search(r"[?&]p=([A-Za-z0-9+/=_-]+)", data)
        if not p:
            return None
        raw = p.group(1)
        raw += "=" * (-len(raw) % 4)
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return None


def extraer(cliente: anthropic.Anthropic, modelo: str, contenido: bytes, mime: str) -> dict:
    """Devuelve un dict normalizado con los datos del comprobante."""
    b64 = base64.b64encode(contenido).decode()
    if mime == "application/pdf":
        bloque = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    else:
        bloque = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}

    resp = cliente.messages.create(
        model=modelo,
        max_tokens=1500,
        messages=[{"role": "user", "content": [bloque, {"type": "text", "text": PROMPT}]}],
    )
    texto = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError("La IA no devolvio JSON")
    d = json.loads(m.group(0))

    qr = leer_qr(contenido) if mime != "application/pdf" else None
    verificados = []
    if qr:
        if qr.get("cuit"):
            d.setdefault("emisor", {})["cuit"] = str(qr["cuit"]); verificados.append("cuit")
        if qr.get("tipoCmp"):
            d["codigo_tipo"] = int(qr["tipoCmp"]); verificados.append("tipo")
        if qr.get("ptoVta"):
            d["punto_venta"] = int(qr["ptoVta"]); verificados.append("pv")
        if qr.get("nroCmp"):
            d["numero"] = int(qr["nroCmp"]); verificados.append("numero")
        if qr.get("fecha"):
            d["fecha"] = str(qr["fecha"]); verificados.append("fecha")
        if qr.get("importe"):
            d["total"] = _num(qr["importe"]); verificados.append("total")
    d["qr"] = bool(qr)
    d["verificados"] = verificados

    # normalizacion
    d["codigo_tipo"] = int(d["codigo_tipo"]) if d.get("codigo_tipo") else None
    d["tipo_texto"] = TIPOS.get(d["codigo_tipo"], "Otro comprobante")
    d["es_nc"] = d["codigo_tipo"] in NOTAS_CREDITO
    d["punto_venta"] = int(d["punto_venta"]) if d.get("punto_venta") else None
    d["numero"] = int(d["numero"]) if d.get("numero") else None
    for k in ("no_gravado", "exento", "percepcion_iva", "percepcion_iibb", "otros_tributos", "total"):
        d[k] = _num(d.get(k))
    ivas = []
    for it in d.get("iva") or []:
        al = _num(it.get("alicuota"))
        ne = _num(it.get("neto"))
        iv = _num(it.get("iva"))
        if ne or iv:
            ivas.append({"alicuota": al, "neto": ne, "iva": iv})
    if not ivas:
        ivas = [{"alicuota": 0.0, "neto": d["total"] - d["otros_tributos"], "iva": 0.0}]
    d["iva"] = ivas
    d.setdefault("emisor", {}); d.setdefault("receptor", {})
    d["emisor"]["cuit"] = re.sub(r"\D", "", str(d["emisor"].get("cuit") or ""))
    d["receptor"]["cuit"] = re.sub(r"\D", "", str(d["receptor"].get("cuit") or ""))

    suma = sum(i["neto"] + i["iva"] for i in ivas) + d["no_gravado"] + d["exento"] \
        + d["percepcion_iva"] + d["percepcion_iibb"] + d["otros_tributos"]
    d["suma_calculada"] = round(suma, 2)
    d["cuadra"] = abs(suma - d["total"]) <= 1.0
    return d
