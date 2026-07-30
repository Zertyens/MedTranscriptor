"""
Cliente de Gemma. Es la unica parte del proyecto que habla con un modelo.

Gemma hace exactamente 3 cosas y ninguna involucra un calculo:
  1. traducir_nota()    - convierte lo que dicto el medico en eventos fechados
  2. verificar_vihda()  - dice que criterios estan documentados y cuales faltan
  3. explicar_egreso()  - resumen en lenguaje llano para el paciente

Todo lo que sale de aca pasa por el validador de modelos.py antes de tocar la
base. Un evento que no valida NO se inserta y NO se descarta en silencio: va a
la lista de rechazados con el motivo.

LO QUE APRENDIMOS PROBANDO LA API CONTRA gemma-4-26b-a4b-it (medido, no supuesto):

1. ES UN MODELO CON RAZONAMIENTO. La respuesta viene en varias 'parts' y la
   primera trae thought=True: es el razonamiento, no la respuesta. Si se toma
   parts[0] a ciegas se lee lo que el modelo penso, no lo que contesto.

2. EL RAZONAMIENTO SE CONTROLA POR NIVEL, NO POR PRESUPUESTO.
   thinkingConfig.thinkingBudget -> HTTP 400.
   thinkingConfig.thinkingLevel: "low" -> HTTP 400.
   thinkingConfig.thinkingLevel: "minimal" -> funciona, y baja de 71s a 16s.

3. SI SOPORTA systemInstruction Y responseSchema (a diferencia de los Gemma
   anteriores). responseSchema es lo que garantiza que 'mediciones' venga
   anidado donde corresponde en vez de desparramado en el payload.

4. PERO responseSchema NO ALCANZA. Como el payload cambia segun el tipo de
   evento y el schema de Google no expresa uniones discriminadas, hay que
   declarar todos los campos posibles juntos. El modelo entonces rellena
   campos que no corresponden ("adjudicado_por" en un evento de dispositivo,
   "motivo" con texto suelto). Por eso Python normaliza el payload despues:
   descarta las claves que no pertenecen a ese tipo de evento. Determinista,
   barato, y no depende de que el modelo se porte bien.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modelos import ErrorValidacion, Evento, nuevo_evento

_DIR = Path(__file__).parent
_EVENTOS_SCHEMA = json.loads((_DIR / "schema" / "eventos.json").read_text(encoding="utf-8"))
_VIHDA = json.loads((_DIR / "schema" / "vihda_criterios.json").read_text(encoding="utf-8"))


def _cargar_env() -> None:
    """Lee .env sin dependencias externas. No pisa variables ya definidas."""
    ruta = _DIR / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip())


_cargar_env()

BACKEND = os.environ.get("GEMMA_BACKEND", "google")
MODELO = os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it")
API_KEY = os.environ.get("GOOGLE_AI_API_KEY", "")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
TIMEOUT = int(os.environ.get("GEMMA_TIMEOUT", "180"))

# Este modelo solo acepta dos niveles: "minimal" y "high". "low" y "medium"
# devuelven HTTP 400. NO es una preferencia de performance: con "minimal" el
# modelo confunde variables clinicas entre si (mapeo creatinina -> leucocitos
# observado en pruebas) y produce timestamps malformados. En un registro
# clinico eso es inaceptable, asi que el default es "high" aunque tarde mas.
NIVEL_RAZONAMIENTO = os.environ.get("GEMMA_THINKING", "high")

# responseSchema existe en este modelo pero es INESTABLE con esquemas anidados:
# combinado con thinking "high" devuelve timestamps basura del tipo
# "2thoughtful_timestamp_format". Se deja apagado por defecto y el JSON se pide
# por prompt, con _extraer_json() y el normalizador como red.
USAR_SCHEMA = os.environ.get("GEMMA_SCHEMA", "0") == "1"

TIPOS_EVENTO = _EVENTOS_SCHEMA["evento"]["campos"]["tipo_evento"]["enum"]
DISPOSITIVOS = list(_EVENTOS_SCHEMA["catalogos"]["dispositivos"])
ADVERSOS = list(_EVENTOS_SCHEMA["catalogos"]["eventos_adversos"])
MEDICIONES = list(_EVENTOS_SCHEMA["payloads"]["fisiologico_24h"]["campos"]["mediciones"]["claves"])

# Que claves de payload son legitimas para cada tipo de evento. Sale del
# schema, no esta escrito a mano: si se agrega un campo, esto se actualiza solo.
CLAVES_PAYLOAD = {
    tipo: set(definicion["campos"]) for tipo, definicion in _EVENTOS_SCHEMA["payloads"].items()
}


class ErrorGemma(Exception):
    pass


@dataclass
class ResultadoTraduccion:
    eventos: list[Evento] = field(default_factory=list)
    no_entendido: list[str] = field(default_factory=list)
    rechazados: list[dict[str, Any]] = field(default_factory=list)
    descartes: list[str] = field(default_factory=list)
    respuesta_cruda: str = ""
    segundos: float = 0.0

    @property
    def requieren_revision(self) -> list[Evento]:
        umbral = _EVENTOS_SCHEMA["evento"]["umbral_revision"]
        return [e for e in self.eventos if e.confianza < umbral]


# ---------------------------------------------------------------------------
# Transporte. Los dos backends son un POST con un JSON; cambiar de uno al otro
# es una variable de entorno, no una reescritura.
# ---------------------------------------------------------------------------

def _post(url: str, cuerpo: dict[str, Any]) -> dict[str, Any]:
    peticion = urllib.request.Request(
        url, data=json.dumps(cuerpo).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=TIMEOUT) as respuesta:
            return json.load(respuesta)
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", errors="replace")[:500]
        raise ErrorGemma(f"HTTP {e.code} del backend '{BACKEND}': {detalle}") from e
    except urllib.error.URLError as e:
        raise ErrorGemma(f"no se pudo contactar el backend '{BACKEND}': {e.reason}") from e


def _llamar_google(
    prompt: str, sistema: str | None, temperatura: float, esquema: dict[str, Any] | None
) -> str:
    if not API_KEY:
        raise ErrorGemma("falta GOOGLE_AI_API_KEY (ponela en .env)")

    config: dict[str, Any] = {
        "temperature": temperatura,
        "thinkingConfig": {"thinkingLevel": NIVEL_RAZONAMIENTO},
    }
    if esquema is not None and USAR_SCHEMA:
        config["responseMimeType"] = "application/json"
        config["responseSchema"] = esquema

    cuerpo: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": config,
    }
    if sistema:
        cuerpo["systemInstruction"] = {"parts": [{"text": sistema}]}

    datos = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent?key={API_KEY}",
        cuerpo,
    )
    try:
        partes = datos["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as e:
        raise ErrorGemma(f"respuesta inesperada de la API: {json.dumps(datos)[:400]}") from e

    # Descartar las partes de razonamiento (thought=True).
    texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
    if not texto.strip():
        raise ErrorGemma("el modelo devolvio solo razonamiento, sin respuesta")
    return texto


def _llamar_ollama(prompt: str, sistema: str | None, temperatura: float, esquema: dict[str, Any] | None) -> str:
    cuerpo: dict[str, Any] = {
        "model": MODELO, "prompt": prompt, "stream": False,
        "options": {"temperature": temperatura},
    }
    if sistema:
        cuerpo["system"] = sistema
    if esquema is not None:
        cuerpo["format"] = "json"
    return _post(f"{OLLAMA_HOST}/api/generate", cuerpo).get("response", "")


def _llamar(
    prompt: str,
    sistema: str | None = None,
    temperatura: float = 0.1,
    esquema: dict[str, Any] | None = None,
) -> str:
    if BACKEND == "google":
        return _llamar_google(prompt, sistema, temperatura, esquema)
    if BACKEND == "ollama":
        return _llamar_ollama(prompt, sistema, temperatura, esquema)
    raise ErrorGemma(f"backend desconocido: {BACKEND!r} (usar 'google' u 'ollama')")


# ---------------------------------------------------------------------------
# Parseo tolerante. Con responseSchema el JSON viene limpio, pero el backend
# de Ollama no lo garantiza, asi que el parseo sigue siendo defensivo.
# ---------------------------------------------------------------------------

def _extraer_json(texto: str) -> dict[str, Any]:
    """Busca el primer objeto JSON balanceado. Aguanta ```json, texto
    alrededor, y llaves dentro de strings."""
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = re.sub(r"^```(?:json)?\s*", "", limpio)
        limpio = re.sub(r"\s*```$", "", limpio)

    inicio = limpio.find("{")
    if inicio == -1:
        raise ErrorGemma(f"la respuesta no contiene ningun JSON: {texto[:300]!r}")

    profundidad = 0
    en_string = False
    escapando = False
    for i, caracter in enumerate(limpio[inicio:], start=inicio):
        if escapando:
            escapando = False
            continue
        if caracter == "\\":
            escapando = True
            continue
        if caracter == '"':
            en_string = not en_string
            continue
        if en_string:
            continue
        if caracter == "{":
            profundidad += 1
        elif caracter == "}":
            profundidad -= 1
            if profundidad == 0:
                try:
                    return json.loads(limpio[inicio:i + 1])
                except json.JSONDecodeError as e:
                    raise ErrorGemma(f"JSON malformado: {e}") from e
    raise ErrorGemma(f"JSON incompleto (llaves sin cerrar): {limpio[inicio:inicio + 200]!r}")


# ---------------------------------------------------------------------------
# Normalizacion. El schema de Google no expresa uniones discriminadas, asi que
# limpiamos el payload nosotros en vez de confiar en que el modelo no se pase.
# ---------------------------------------------------------------------------

def _normalizar_timestamp(valor: str) -> str:
    """El modelo a veces devuelve solo la fecha. Se completa a medianoche."""
    valor = (valor or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor):
        return f"{valor}T00:00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", valor):
        return valor.replace(" ", "T") + ":00"
    return valor.replace(" ", "T")


def _normalizar_payload(tipo_evento: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Descarta las claves que no pertenecen a este tipo de evento y limpia
    los sub-objetos. Devuelve (payload_limpio, claves_descartadas)."""
    permitidas = CLAVES_PAYLOAD.get(tipo_evento, set())
    limpio: dict[str, Any] = {}
    descartadas: list[str] = []

    for clave, valor in (payload or {}).items():
        if clave not in permitidas or valor is None:
            if clave not in permitidas:
                descartadas.append(f"{tipo_evento}.{clave}")
            continue
        limpio[clave] = valor

    # Las mediciones vienen como objeto con todas las claves posibles; el
    # modelo rellena con null o 0 las que no se dictaron.
    if tipo_evento == "fisiologico_24h":
        mediciones = {
            k: v for k, v in (limpio.get("mediciones") or {}).items()
            if k in MEDICIONES and v is not None
        }
        limpio["mediciones"] = mediciones

    if tipo_evento == "evento_adverso":
        # La declaracion de infeccion es siempre del medico, nunca del modelo.
        limpio["adjudicado_por"] = "medico"

    return limpio, descartadas


# ---------------------------------------------------------------------------
# 1. TRADUCE
# ---------------------------------------------------------------------------

SISTEMA_TRADUCCION = """Sos un asistente de registro clinico de una Unidad de Cuidados Intensivos de Argentina.
Convertis lo que dicto un medico en eventos fechados. Esa es tu unica tarea.

REGLAS ABSOLUTAS
- NO calcules. No sumes dias. No cuentes episodios. No infieras totales.
- NO decidas si hay una infeccion. Si el medico la declara, la registras.
  Si no la declara, no existe.
- Un mismo hecho genera UN SOLO evento. "Le sacamos la sonda" es unicamente un
  dispositivo_fin: no inventes el dispositivo_inicio que no se menciono.
- texto_crudo tiene que ser un fragmento LITERAL de la nota, copiado tal cual.
  No un resumen, no una parafrasis.
- Si no podes fechar algo con certeza, ponelo en no_entendido en vez de
  inventar una fecha. Declarar lo que no entendiste es lo correcto.
- La confianza tiene que reflejar tu certeza real. Si la nota no dice la hora,
  o la fecha es relativa y ambigua, baja la confianza. No pongas 1.0 por defecto.

REGLAS PARTICULARES
- Cada dispositivo lleva instancia_id con la convencion <DISPOSITIVO>-<n>.
  Dos cateteres colocados a la vez son instancias distintas: CVC-1 y CVC-2.
- Una autoextubacion genera DOS eventos: un dispositivo_fin de VI con
  motivo "accidental", y un evento_adverso con codigo AUTOEXTUBACION.
- En el payload pone SOLO los campos que corresponden a ese tipo de evento.
- timestamp_clinico siempre completo y literal: aaaa-mm-ddTHH:mm:ss
  (por ejemplo 2025-03-05T09:00:00). Nunca un texto descriptivo.

TIPOS DE EVENTO: {tipos}
DISPOSITIVOS: {dispositivos}
EVENTOS ADVERSOS: {adversos}
CLAVES DE MEDICIONES (usa exactamente estos nombres, no los confundas entre si):
{mediciones}

PAYLOAD SEGUN EL TIPO DE EVENTO
  dispositivo_inicio: dispositivo, instancia_id, sitio
  dispositivo_fin:    dispositivo, instancia_id, motivo
  evento_adverso:     codigo, adjudicado_por
  fisiologico_24h:    mediciones (objeto), falla_renal_aguda
  tiss_diario:        fecha, puntaje_manual
  egreso:             resultado (NUMERO ENTERO, nunca texto)

CODIGOS DE RESULTADO AL EGRESO (usa el numero, no la descripcion):
{resultados}

DISPOSITIVOS QUE YA ESTAN COLOCADOS EN ESTE PACIENTE
{instancias_abiertas}
Si la nota menciona que se retira uno de estos, usa EXACTAMENTE su instancia_id.
Si menciona que se coloca uno nuevo, inventa el siguiente numero libre.

FORMATO DE SALIDA. Devolve SOLO este JSON, sin texto antes ni despues:
{{
  "eventos": [
    {{"tipo_evento": "...", "timestamp_clinico": "aaaa-mm-ddTHH:mm:ss",
      "payload": {{}}, "confianza": 0.0, "texto_crudo": "fragmento literal"}}
  ],
  "no_entendido": ["fragmentos que no pudiste mapear o fechar"]
}}"""


def _sistema_traduccion(instancias_abiertas: dict[str, str] | None = None) -> str:
    """Los enums salen del schema, no estan duplicados en el prompt.

    'instancias_abiertas' es el estado del paciente: que dispositivos siguen
    colocados y con que instancia_id. Sin esto el modelo no tiene forma de
    saber que la yugular era CVC-2 y no CVC-1, porque cada nota se procesa
    de forma independiente."""
    if instancias_abiertas:
        estado = "\n".join(f"  {inst} = {desc}" for inst, desc in sorted(instancias_abiertas.items()))
    else:
        estado = "  (ninguno todavia)"

    return SISTEMA_TRADUCCION.format(
        tipos=", ".join(TIPOS_EVENTO),
        dispositivos=", ".join(
            f"{k} ({v})" for k, v in _EVENTOS_SCHEMA["catalogos"]["dispositivos"].items()
        ),
        adversos=", ".join(ADVERSOS),
        mediciones="\n".join(
            f"  {k}: {v}"
            for k, v in _EVENTOS_SCHEMA["payloads"]["fisiologico_24h"]["campos"]["mediciones"]["claves"].items()
        ),
        resultados="\n".join(
            f"  {k} = {v}" for k, v in _EVENTOS_SCHEMA["catalogos"]["resultado_egreso"].items()
        ),
        instancias_abiertas=estado,
    )


def instancias_abiertas(eventos: list[Evento]) -> dict[str, str]:
    """Que dispositivos siguen colocados, segun los eventos ya registrados.
    Se le pasa a traducir_nota() para que el modelo mantenga continuidad."""
    abiertas: dict[str, str] = {}
    for e in sorted(eventos, key=lambda x: x.timestamp_clinico):
        p = e.payload_json
        inst = p.get("instancia_id")
        if not inst:
            continue
        if e.tipo_evento == "dispositivo_inicio":
            sitio = p.get("sitio")
            abiertas[inst] = f"{p.get('dispositivo')}" + (f" ({sitio})" if sitio else "")
        elif e.tipo_evento == "dispositivo_fin":
            abiertas.pop(inst, None)
    return abiertas


def _esquema_traduccion() -> dict[str, Any]:
    """El esquema de respuesta se arma desde schema/eventos.json: los enums no
    estan duplicados aca."""
    return {
        "type": "OBJECT",
        "properties": {
            "eventos": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tipo_evento": {"type": "STRING", "enum": TIPOS_EVENTO},
                        "timestamp_clinico": {"type": "STRING"},
                        "payload": {
                            "type": "OBJECT",
                            "properties": {
                                "dispositivo": {"type": "STRING", "enum": DISPOSITIVOS},
                                "instancia_id": {"type": "STRING"},
                                "sitio": {"type": "STRING"},
                                "motivo": {"type": "STRING", "enum": ["programado", "accidental", "recambio", "egreso"]},
                                "codigo": {"type": "STRING", "enum": ADVERSOS},
                                "mediciones": {
                                    "type": "OBJECT",
                                    "properties": {k: {"type": "NUMBER"} for k in MEDICIONES},
                                },
                                "falla_renal_aguda": {"type": "BOOLEAN"},
                                "fecha": {"type": "STRING"},
                                "puntaje_manual": {"type": "INTEGER"},
                                "resultado": {"type": "INTEGER"},
                            },
                        },
                        "confianza": {"type": "NUMBER"},
                        "texto_crudo": {"type": "STRING"},
                        "corrige": {"type": "BOOLEAN"},
                    },
                    "required": ["tipo_evento", "timestamp_clinico", "payload", "confianza", "texto_crudo"],
                },
            },
            "no_entendido": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["eventos", "no_entendido"],
    }


def traducir_nota(
    episodio_id: str,
    nota: str,
    fecha_referencia: str,
    autor: str,
    fuente: str = "texto_gemma",
    abiertas: dict[str, str] | None = None,
) -> ResultadoTraduccion:
    """Convierte una nota de evolucion en eventos validados.

    'abiertas' son los dispositivos que ya estan colocados (ver
    instancias_abiertas()). Sin ese estado el modelo no puede mantener la
    continuidad de instancia_id entre notas.

    Los eventos que no pasan la validacion van a 'rechazados' con el motivo.
    Nunca se insertan a la fuerza ni se descartan sin dejar rastro."""
    import time

    inicio = time.time()
    crudo = _llamar(
        prompt=f"FECHA DE REFERENCIA: {fecha_referencia}\n\nNOTA DE EVOLUCION:\n{nota}",
        sistema=_sistema_traduccion(abiertas),
        esquema=_esquema_traduccion(),
    )
    datos = _extraer_json(crudo)

    resultado = ResultadoTraduccion(
        no_entendido=list(datos.get("no_entendido") or []),
        respuesta_cruda=crudo,
        segundos=round(time.time() - inicio, 1),
    )

    for bruto in datos.get("eventos") or []:
        tipo = bruto.get("tipo_evento")
        payload, descartes = _normalizar_payload(tipo, bruto.get("payload") or {})
        resultado.descartes.extend(descartes)
        try:
            evento = nuevo_evento(
                episodio_id=episodio_id,
                timestamp_clinico=_normalizar_timestamp(bruto.get("timestamp_clinico", "")),
                autor=autor,
                tipo_evento=tipo,
                payload_json=payload,
                fuente=fuente,
                confianza=float(bruto.get("confianza", 0.5)),
                texto_crudo=bruto.get("texto_crudo") or "",
            )
        except (ErrorValidacion, KeyError, TypeError, ValueError) as e:
            resultado.rechazados.append({"evento": bruto, "motivo": str(e)})
            continue
        resultado.eventos.append(evento)

    return resultado


# ---------------------------------------------------------------------------
# 2. VERIFICA (no adjudica)
# ---------------------------------------------------------------------------

SISTEMA_VERIFICACION = """Sos un auditor de registros clinicos de una Unidad de Cuidados Intensivos.

El medico YA DECLARO la infeccion. Su diagnostico no esta en discusion y no es
tu tarea confirmarlo, cuestionarlo ni reclasificarlo.

Tu unica tarea es revisar si el REGISTRO CLINICO documenta la evidencia que el
programa de vigilancia VIHDA exige para poder reportar el caso al registro
nacional. Es una revision de completitud del registro, no del diagnostico.

Para cada criterio decidi si el registro contiene evidencia textual que lo
documente. Si no la encontras, va a "faltantes". NO infieras: si el texto no lo
dice, falta. Es preferible marcar algo como faltante que darlo por documentado
sin evidencia.

Usa exactamente los ids de criterio que se te dan entre corchetes.

FORMATO DE SALIDA. Devolve SOLO este JSON, sin texto antes ni despues, sin
markdown, sin titulos:
{
  "cumplidos": [{"id": "id_del_criterio", "evidencia": "cita textual del registro"}],
  "faltantes": [{"id": "id_del_criterio", "que_falta": "que habria que documentar"}]
}"""

_ESQUEMA_VERIFICACION = {
    "type": "OBJECT",
    "properties": {
        "cumplidos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"id": {"type": "STRING"}, "evidencia": {"type": "STRING"}},
                "required": ["id", "evidencia"],
            },
        },
        "faltantes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"id": {"type": "STRING"}, "que_falta": {"type": "STRING"}},
                "required": ["id", "que_falta"],
            },
        },
    },
    "required": ["cumplidos", "faltantes"],
}


def _criterios_planos(codigo: str) -> list[dict[str, str]]:
    """Aplana la estructura de vihda_criterios.json a una lista de (id, texto)."""
    definicion = _VIHDA[codigo]
    planos: list[dict[str, str]] = []

    def agregar(criterios: list[dict[str, Any]]) -> None:
        for c in criterios:
            planos.append({"id": c["id"], "texto": c["texto"]})
            for sub in c.get("subcriterios", []):
                planos.append({"id": sub["id"], "texto": sub["texto"]})

    agregar(definicion.get("criterios", []))
    for tipo in definicion.get("tipos", []):
        agregar(tipo.get("criterios", []))
    return planos


def verificar_vihda(codigo: str, registro: str) -> dict[str, Any]:
    """Compara lo declarado por el medico contra los criterios VIHDA.

    Devuelve que esta documentado y que falta. NO decide si hay infeccion: eso
    ya lo decidio el medico antes de llamar a esta funcion."""
    if codigo not in _VIHDA:
        raise ErrorGemma(f"{codigo} no tiene criterios VIHDA definidos")

    criterios = _criterios_planos(codigo)
    prompt = (
        f"INFECCION DECLARADA POR EL MEDICO: {_VIHDA[codigo]['nombre']}\n\n"
        "CRITERIOS QUE EXIGE VIHDA:\n"
        + "\n".join(f"  [{c['id']}] {c['texto']}" for c in criterios)
        + f"\n\nREGISTRO CLINICO DISPONIBLE:\n{registro}"
    )
    datos = _extraer_json(_llamar(prompt, sistema=SISTEMA_VERIFICACION, esquema=_ESQUEMA_VERIFICACION))
    ids_validos = {c["id"] for c in criterios}
    return {
        # Se filtran ids que el modelo pueda haber inventado.
        "cumplidos": [c for c in (datos.get("cumplidos") or []) if c.get("id") in ids_validos],
        "faltantes": [c for c in (datos.get("faltantes") or []) if c.get("id") in ids_validos],
    }


# ---------------------------------------------------------------------------
# 3. EXPLICA
# ---------------------------------------------------------------------------

SISTEMA_EXPLICACION = """Escribis resumenes de internacion para pacientes y sus familias.

Quien lo lee no tiene formacion medica: es una persona preocupada que quiere
entender que le paso a su familiar.

REGLAS
- Nada de siglas sin explicar. "ARM" no significa nada para ellos: escribi
  "un respirador que lo ayudo a respirar".
- Nada de numeros de score ni de codigos del registro.
- No agregues informacion que no este en los datos que te dan. No supongas
  pronosticos, no des consejos medicos, no interpretes mas alla del registro.
- Tono sereno y directo. Ni frio ni dramatico.
- Entre 3 y 5 parrafos cortos. Solo el resumen, sin titulo ni encabezado."""


def explicar_egreso(resumen: str) -> str:
    """Resumen en lenguaje llano. 'resumen' lo arma Python a partir de la fila
    proyectada: Gemma redacta, no calcula."""
    return _llamar(
        f"DATOS DE LA INTERNACION:\n{resumen}",
        sistema=SISTEMA_EXPLICACION,
        temperatura=0.4,
    ).strip()
