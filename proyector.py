"""
Proyector: convierte (episodio, eventos) en la fila de 49 campos de SATI-Q.

La fila NUNCA se guarda. Se recalcula acá cada vez que se pide, a partir de
los eventos guardados en db.py. Cada campo devuelto trae la lista de ids de
evento que lo originaron -- esa lista es la trazabilidad que se muestra en
la UI cuando el jurado clickea un numero.

Gemma no entra a este archivo. Todo lo de aca es aritmetica sobre timestamps
y conteos, deliberadamente aburrido.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import apache2
from modelos import Episodio, Evento

_SCHEMA_DIR = Path(__file__).parent / "schema"
_CAMPOS_SCHEMA = json.loads((_SCHEMA_DIR / "satiq_campos.json").read_text(encoding="utf-8"))["campos"]

_FORMATO_FECHA = "%d/%m/%Y"
_FORMATO_HORA = "%H:%M:%S"


@dataclass
class ProyeccionCampo:
    valor: Any
    evento_ids: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    # Desglose opcional para campos compuestos. Hoy solo lo usa el APACHE II:
    # clickear SCORE tiene que mostrar las 12 variables con su valor, sus
    # puntos y de que evento salio cada una, no una lista plana de 20 ids.
    detalle: dict[str, Any] | None = None
    # Cuando el valor fue cargado a mano en vez de derivado de los eventos.
    # Nunca se pisa en silencio: la UI tiene que poder mostrarlo distinto y
    # decir quien lo puso y por que.
    ajuste_manual: dict[str, Any] | None = None
    valor_derivado: Any = None


class ErrorProyeccion(Exception):
    pass


# ---------------------------------------------------------------------------
# Vigencia: un evento es vigente si ningun otro evento lo corrige.
# ---------------------------------------------------------------------------

def eventos_vigentes(eventos: list[Evento]) -> list[Evento]:
    corregidos = {e.corrige_a_evento_id for e in eventos if e.corrige_a_evento_id}
    return [e for e in eventos if e.id not in corregidos]


def _por_tipo(eventos: list[Evento], tipo_evento: str) -> list[Evento]:
    return [e for e in eventos if e.tipo_evento == tipo_evento]


def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# Egreso: punto de referencia compartido por varios campos.
# ---------------------------------------------------------------------------

def _evento_egreso(vigentes: list[Evento]) -> Evento | None:
    """El egreso del episodio. Deberia haber a lo sumo uno.

    Si hay mas de uno, NO se explota: gana el ultimo cargado y se avisa por
    advertencia (ver _advertir_egresos_duplicados). Antes esto tiraba
    ErrorProyeccion, y como la proyeccion se calcula al abrir el paciente, un
    egreso duplicado dejaba la aplicacion inusable: no se podia ni entrar a
    corregirlo. Un dato inconsistente tiene que verse, no romper la pantalla."""
    egresos = _por_tipo(vigentes, "egreso")
    if not egresos:
        return None
    # Desempate por timestamp_clinico: dos egresos cargados en el mismo
    # segundo tienen el mismo timestamp_carga, y sin este segundo criterio
    # cual gana dependeria del orden en que vinieron de la base.
    return max(egresos, key=lambda e: (e.timestamp_carga, e.timestamp_clinico))


def _advertir_egresos_duplicados(vigentes: list[Evento]) -> str | None:
    egresos = _por_tipo(vigentes, "egreso")
    if len(egresos) <= 1:
        return None
    fechas = ", ".join(sorted(e.timestamp_clinico[:16] for e in egresos))
    return (
        f"Hay {len(egresos)} egresos cargados ({fechas}). Se usa el ultimo, pero solo "
        "puede haber uno: corregi o anula los que sobran en la historia clinica."
    )


def f_constante_centro(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    return ProyeccionCampo(valor=episodio.idcentro)


def f_campo_episodio(episodio: Episodio, vigentes: list[Evento], arg: str) -> ProyeccionCampo:
    valor = getattr(episodio, arg)
    if isinstance(valor, bool):
        valor = int(valor)
    elif arg == "fecha_ingreso":
        valor = datetime.strptime(valor, "%Y-%m-%d").strftime(_FORMATO_FECHA)
    return ProyeccionCampo(valor=valor)


def f_egreso_fecha(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    egreso = _evento_egreso(vigentes)
    if egreso is None:
        return ProyeccionCampo(valor=None, advertencias=["episodio sin evento egreso"])
    fecha = _parse_dt(egreso.timestamp_clinico).strftime(_FORMATO_FECHA)
    return ProyeccionCampo(valor=fecha, evento_ids=[egreso.id])


def f_egreso_hora(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    egreso = _evento_egreso(vigentes)
    if egreso is None:
        return ProyeccionCampo(valor=None, advertencias=["episodio sin evento egreso"])
    hora = _parse_dt(egreso.timestamp_clinico).strftime(_FORMATO_HORA)
    return ProyeccionCampo(valor=hora, evento_ids=[egreso.id])


def f_egreso_resultado(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    egreso = _evento_egreso(vigentes)
    if egreso is None:
        return ProyeccionCampo(valor=None, advertencias=["episodio sin evento egreso"])
    return ProyeccionCampo(valor=egreso.payload_json["resultado"], evento_ids=[egreso.id])


def f_estadia(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    """Cuenta el dia de ingreso pero no el de egreso. Mismo dia = 1 dia.
    Verificado contra el ejemplo del Anexo A4: 01/03/2025 -> 01/04/2025 = 31."""
    egreso = _evento_egreso(vigentes)
    if egreso is None:
        return ProyeccionCampo(valor=None, advertencias=["episodio sin evento egreso"])
    fecha_ingreso = datetime.strptime(episodio.fecha_ingreso, "%Y-%m-%d").date()
    fecha_egreso = _parse_dt(egreso.timestamp_clinico).date()
    dias = (fecha_egreso - fecha_ingreso).days
    return ProyeccionCampo(valor=max(1, dias), evento_ids=[egreso.id])


# ---------------------------------------------------------------------------
# Dispositivos: flag + dias, con instancia_id para simultaneos.
# ---------------------------------------------------------------------------

@dataclass
class _Instancia:
    instancia_id: str
    inicio: Evento
    fin: Evento | None  # None si se cierra por egreso (fallback)
    dias: int
    evento_ids: list[str]


def _instancias_dispositivo(
    dispositivo: str, vigentes: list[Evento], egreso: Evento | None
) -> tuple[list[_Instancia], list[str]]:
    """Empareja cada dispositivo_inicio con su dispositivo_fin por instancia_id.
    Si una instancia nunca se cierra, se cierra con el timestamp del egreso.
    Devuelve las instancias resueltas y advertencias (instancias sin cierre y sin egreso)."""
    inicios = [e for e in _por_tipo(vigentes, "dispositivo_inicio") if e.payload_json["dispositivo"] == dispositivo]
    fines = [e for e in _por_tipo(vigentes, "dispositivo_fin") if e.payload_json["dispositivo"] == dispositivo]
    fines_por_instancia: dict[str, Evento] = {}
    for f in fines:
        inst = f.payload_json["instancia_id"]
        # Si hay mas de un fin para la misma instancia (no deberia pasar sin
        # una correccion de por medio), nos quedamos con el mas temprano.
        if inst not in fines_por_instancia or _parse_dt(f.timestamp_clinico) < _parse_dt(fines_por_instancia[inst].timestamp_clinico):
            fines_por_instancia[inst] = f

    instancias: list[_Instancia] = []
    advertencias: list[str] = []
    vistas: set[str] = set()
    for inicio in sorted(inicios, key=lambda e: e.timestamp_clinico):
        inst_id = inicio.payload_json["instancia_id"]
        if inst_id in vistas:
            continue  # instancia duplicada: nos quedamos con el primer inicio
        vistas.add(inst_id)

        fin = fines_por_instancia.get(inst_id)
        evento_ids = [inicio.id]
        if fin is not None:
            fin_dt = _parse_dt(fin.timestamp_clinico)
            evento_ids.append(fin.id)
        elif egreso is not None:
            fin_dt = _parse_dt(egreso.timestamp_clinico)
            evento_ids.append(egreso.id)
        else:
            advertencias.append(
                f"{dispositivo} instancia {inst_id}: sin dispositivo_fin y episodio sin egreso, no se puede cerrar"
            )
            continue

        inicio_dt = _parse_dt(inicio.timestamp_clinico)
        if fin_dt < inicio_dt:
            advertencias.append(f"{dispositivo} instancia {inst_id}: el fin es anterior al inicio")
            continue
        # Metodologia VIHDA: se cuentan fechas calendario distintas, no horas
        # transcurridas. "Un dia calendario no debe interpretarse como 24
        # horas": el dia de colocacion es el dia 1 y el de remocion tambien
        # cuenta. Por eso 1/3 23:00 -> 2/3 01:00 (2hs reales) da 2 dias.
        dias = (fin_dt.date() - inicio_dt.date()).days + 1
        instancias.append(_Instancia(inst_id, inicio, fin, dias, evento_ids))

    return instancias, advertencias


def f_dispositivo_flag(episodio: Episodio, vigentes: list[Evento], arg: str) -> ProyeccionCampo:
    egreso = _evento_egreso(vigentes)
    instancias, advertencias = _instancias_dispositivo(arg, vigentes, egreso)
    if not instancias:
        return ProyeccionCampo(valor=0, advertencias=advertencias)
    ids = sorted({i.inicio.id for i in instancias})
    return ProyeccionCampo(valor=1, evento_ids=ids, advertencias=advertencias)


def f_dispositivo_dias(episodio: Episodio, vigentes: list[Evento], arg: str) -> ProyeccionCampo:
    egreso = _evento_egreso(vigentes)
    instancias, advertencias = _instancias_dispositivo(arg, vigentes, egreso)
    if not instancias:
        return ProyeccionCampo(valor=0, advertencias=advertencias)
    total_dias = sum(i.dias for i in instancias)
    ids = sorted({eid for i in instancias for eid in i.evento_ids})
    return ProyeccionCampo(valor=total_dias, evento_ids=ids, advertencias=advertencias)


# ---------------------------------------------------------------------------
# Eventos adversos: flag + num.
# ---------------------------------------------------------------------------

def f_adverso_flag(episodio: Episodio, vigentes: list[Evento], arg: str) -> ProyeccionCampo:
    ocurrencias = [e for e in _por_tipo(vigentes, "evento_adverso") if e.payload_json["codigo"] == arg]
    if not ocurrencias:
        return ProyeccionCampo(valor=0)
    return ProyeccionCampo(valor=1, evento_ids=[e.id for e in ocurrencias])


def f_adverso_num(episodio: Episodio, vigentes: list[Evento], arg: str) -> ProyeccionCampo:
    ocurrencias = [e for e in _por_tipo(vigentes, "evento_adverso") if e.payload_json["codigo"] == arg]
    if not ocurrencias:
        return ProyeccionCampo(valor=0)
    return ProyeccionCampo(valor=len(ocurrencias), evento_ids=[e.id for e in ocurrencias])


# ---------------------------------------------------------------------------
# TISS-28: min/max/promedio de puntajes diarios.
# Los pesos de los items (schema/tiss28_items.json) todavia no existen --
# quedan pendientes del equipo clinico. Mientras tanto se apoya en
# 'puntaje_manual' si vino en el payload.
# ---------------------------------------------------------------------------

_TISS_PESOS_PATH = _SCHEMA_DIR / "tiss28_items.json"


def _cargar_pesos_tiss() -> dict[str, int] | None:
    if not _TISS_PESOS_PATH.exists():
        return None
    data = json.loads(_TISS_PESOS_PATH.read_text(encoding="utf-8"))
    return data.get("pesos", data)


def _puntaje_tiss(evento: Evento, pesos: dict[str, int] | None) -> tuple[int | None, list[str]]:
    payload = evento.payload_json
    advertencias: list[str] = []
    if payload.get("items"):
        if pesos is None:
            if "puntaje_manual" in payload:
                advertencias.append(
                    f"tiss_diario {evento.id}: no hay tabla de pesos, se uso puntaje_manual en su lugar"
                )
                return payload["puntaje_manual"], advertencias
            advertencias.append(f"tiss_diario {evento.id}: trae 'items' pero no existe schema/tiss28_items.json")
            return None, advertencias
        total = 0
        for item in payload["items"]:
            if item not in pesos:
                advertencias.append(f"tiss_diario {evento.id}: item desconocido '{item}', se ignora")
                continue
            total += pesos[item]
        return total, advertencias
    if "puntaje_manual" in payload:
        return payload["puntaje_manual"], advertencias
    advertencias.append(f"tiss_diario {evento.id}: sin 'items' ni 'puntaje_manual', no se puede puntuar")
    return None, advertencias


def _puntajes_tiss(vigentes: list[Evento]) -> tuple[list[tuple[Evento, int]], list[str]]:
    pesos = _cargar_pesos_tiss()
    resultado: list[tuple[Evento, int]] = []
    advertencias: list[str] = []
    for evento in _por_tipo(vigentes, "tiss_diario"):
        puntaje, adv = _puntaje_tiss(evento, pesos)
        advertencias.extend(adv)
        if puntaje is not None:
            resultado.append((evento, puntaje))
    return resultado, advertencias


def f_tiss_min(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    puntajes, advertencias = _puntajes_tiss(vigentes)
    if not puntajes:
        return ProyeccionCampo(valor=None, advertencias=advertencias or ["sin registros tiss_diario"])
    minimo = min(p for _, p in puntajes)
    ids = [e.id for e, p in puntajes if p == minimo]
    return ProyeccionCampo(valor=minimo, evento_ids=ids, advertencias=advertencias)


def f_tiss_max(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    puntajes, advertencias = _puntajes_tiss(vigentes)
    if not puntajes:
        return ProyeccionCampo(valor=None, advertencias=advertencias or ["sin registros tiss_diario"])
    maximo = max(p for _, p in puntajes)
    ids = [e.id for e, p in puntajes if p == maximo]
    return ProyeccionCampo(valor=maximo, evento_ids=ids, advertencias=advertencias)


def f_tiss_promedio(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    puntajes, advertencias = _puntajes_tiss(vigentes)
    if not puntajes:
        return ProyeccionCampo(valor=None, advertencias=advertencias or ["sin registros tiss_diario"])
    promedio = round(sum(p for _, p in puntajes) / len(puntajes), 2)
    ids = [e.id for e, _ in puntajes]
    return ProyeccionCampo(valor=promedio, evento_ids=ids, advertencias=advertencias)


# ---------------------------------------------------------------------------
# APACHE II. El calculo vive en apache2.py; aca solo se arma la ventana de
# 24hs y se traduce el resultado a ProyeccionCampo.
# ---------------------------------------------------------------------------

def _eventos_primeras_24h(episodio: Episodio, vigentes: list[Evento]) -> list[Evento]:
    ingreso = datetime.strptime(f"{episodio.fecha_ingreso}T{episodio.hora_ingreso}", "%Y-%m-%dT%H:%M:%S")
    limite = ingreso + timedelta(hours=24)
    return [
        e for e in _por_tipo(vigentes, "fisiologico_24h")
        if ingreso <= _parse_dt(e.timestamp_clinico) <= limite
    ]


def f_apache_score(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    eventos = _eventos_primeras_24h(episodio, vigentes)
    resultado = apache2.calcular(episodio, eventos)
    # Solo se citan los eventos que efectivamente aportaron puntos, no todos
    # los de la ventana: si no, clickear SCORE muestra ruido.
    ids = sorted({c.evento_id for c in resultado.componentes if c.evento_id})
    return ProyeccionCampo(
        valor=resultado.score,
        evento_ids=ids,
        advertencias=resultado.advertencias,
        detalle=resultado.to_dict(),
    )


def f_apache_probabilidad(episodio: Episodio, vigentes: list[Evento], arg: str | None) -> ProyeccionCampo:
    eventos = _eventos_primeras_24h(episodio, vigentes)
    resultado = apache2.calcular(episodio, eventos)
    probabilidad, advertencias = apache2.probabilidad_muerte(resultado, episodio)
    ids = sorted({c.evento_id for c in resultado.componentes if c.evento_id})
    return ProyeccionCampo(
        valor=probabilidad,
        evento_ids=ids,
        advertencias=advertencias,
        detalle={"score_apache": resultado.score, "confiable": resultado.confiable},
    )


# ---------------------------------------------------------------------------
# Dispatch y proyeccion completa.
# ---------------------------------------------------------------------------

FUNCIONES: dict[str, Callable[[Episodio, list[Evento], str | None], ProyeccionCampo]] = {
    "constante_centro": f_constante_centro,
    "campo_episodio": f_campo_episodio,
    "egreso_fecha": f_egreso_fecha,
    "egreso_hora": f_egreso_hora,
    "egreso_resultado": f_egreso_resultado,
    "estadia": f_estadia,
    "dispositivo_flag": f_dispositivo_flag,
    "dispositivo_dias": f_dispositivo_dias,
    "adverso_flag": f_adverso_flag,
    "adverso_num": f_adverso_num,
    "tiss_min": f_tiss_min,
    "tiss_max": f_tiss_max,
    "tiss_promedio": f_tiss_promedio,
    "apache_score": f_apache_score,
    "apache_probabilidad": f_apache_probabilidad,
}


def _convertir(texto: str, tipo: str | None):
    """Los ajustes se guardan como texto; se devuelven con el tipo del campo."""
    if texto is None:
        return None
    if tipo == "entero":
        return int(float(texto))
    if tipo == "decimal":
        return round(float(texto), 2)
    return texto


def proyectar_fila(
    episodio: Episodio,
    eventos: list[Evento],
    ajustes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, ProyeccionCampo]:
    """Proyecta los 49 campos de SATI-Q, en el orden del Anexo A4.

    'ajustes' son valores cargados a mano (ver db.ajustes_vigentes). Pisan lo
    derivado, pero el valor derivado se conserva en valor_derivado y el ajuste
    queda descripto en ajuste_manual: el reemplazo nunca es silencioso."""
    vigentes = eventos_vigentes(eventos)
    ajustes = ajustes or {}
    fila: dict[str, ProyeccionCampo] = {}
    aviso_egresos = _advertir_egresos_duplicados(vigentes)

    for campo in _CAMPOS_SCHEMA:
        nombre = campo["nombre"]
        funcion = FUNCIONES.get(campo["funcion"])
        if funcion is None:
            raise ErrorProyeccion(f"campo {nombre}: no hay funcion registrada para '{campo['funcion']}'")
        proyectado = funcion(episodio, vigentes, campo.get("arg"))

        if aviso_egresos and "egreso" in campo["depende_de"]:
            proyectado.advertencias.append(aviso_egresos)

        ajuste = ajustes.get(nombre)
        if ajuste is not None:
            proyectado.valor_derivado = proyectado.valor
            proyectado.ajuste_manual = ajuste
            try:
                proyectado.valor = _convertir(ajuste["valor"], campo.get("validacion", {}).get("tipo"))
            except (TypeError, ValueError):
                proyectado.advertencias.append(
                    f"el valor cargado a mano ({ajuste['valor']!r}) no tiene el tipo esperado"
                )

        fila[nombre] = proyectado

    return fila


def episodio_abierto(eventos: list[Evento]) -> bool:
    """Un episodio esta abierto si no tiene (todavia) un evento egreso vigente."""
    return _evento_egreso(eventos_vigentes(eventos)) is None


def fila_a_valores(fila: dict[str, ProyeccionCampo]) -> dict[str, Any]:
    """Solo los valores, sin trazabilidad. Para exportar CSV (paso 4)."""
    return {nombre: campo.valor for nombre, campo in fila.items()}


def advertencias_fila(fila: dict[str, ProyeccionCampo]) -> dict[str, list[str]]:
    """Solo las advertencias no vacias, por campo. Para mostrar en la UI."""
    return {nombre: campo.advertencias for nombre, campo in fila.items() if campo.advertencias}
