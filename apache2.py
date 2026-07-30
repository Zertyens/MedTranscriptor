"""
Calculo del APACHE II y de la probabilidad de muerte asociada.

Funciones puras: no toca la base, no toca Gemma. Entra un episodio y la lista
de eventos fisiologico_24h de las primeras 24hs, sale un score con el detalle
de como se compuso.

Las tablas de puntos viven en schema/apache2.json. Este archivo solo hace
lookup contra ellas.

Dos cosas que se hacen distinto a la implementacion ingenua, a proposito:

1. PEOR VALOR = MAXIMO DE PUNTOS, no valor mas alto ni mas bajo. Para cada
   medicion registrada se calculan sus puntos y gana la que mas puntua. Es la
   unica forma correcta para variables bidireccionales: en temperatura, tanto
   33 C como 41 C son peores que 37 C.

2. LA OXIGENACION SE EVALUA POR EVENTO, no por variable. La rama (gradiente
   alveolo-arterial vs PaO2) depende del FiO2 DEL MISMO GAS. Si se tomara el
   peor FiO2 y la peor PaO2 por separado se estarian mezclando mediciones de
   momentos distintos y se elegiria la rama equivocada.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modelos import Episodio, Evento

_TABLAS = json.loads((Path(__file__).parent / "schema" / "apache2.json").read_text(encoding="utf-8"))

_VARIABLES = _TABLAS["variables_fisiologicas"]
_OXIGENACION = _TABLAS["oxigenacion"]
_GLASGOW = _TABLAS["glasgow"]
_EDAD = _TABLAS["edad"]
_SALUD_CRONICA = _TABLAS["salud_cronica"]
_MORTALIDAD = _TABLAS["mortalidad"]

# Las 12 variables fisiologicas del score. 'oxigenacion' y 'glasgow' son
# especiales y se calculan aparte; el pH tiene al bicarbonato como sustituto.
_VARIABLES_SIMPLES = [
    "temperatura_c",
    "pam_mmhg",
    "fc_lpm",
    "fr_rpm",
    "sodio_meq_l",
    "potasio_meq_l",
    "creatinina_mg_dl",
    "hematocrito_pct",
    "leucocitos_mil_mm3",
]

# Si faltan mas de estas, el score deja de considerarse confiable.
MAX_VARIABLES_FALTANTES = 4


@dataclass
class ComponenteApache:
    """Una linea del desglose del score. Es lo que ve el jurado al clickear SCORE."""
    variable: str
    etiqueta: str
    valor: float | None
    puntos: int
    evento_id: str | None = None
    faltante: bool = False
    nota: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "etiqueta": self.etiqueta,
            "valor": self.valor,
            "puntos": self.puntos,
            "evento_id": self.evento_id,
            "faltante": self.faltante,
            "nota": self.nota,
        }


@dataclass
class ResultadoApache:
    score: int
    componentes: list[ComponenteApache] = field(default_factory=list)
    variables_faltantes: list[str] = field(default_factory=list)
    confiable: bool = True
    advertencias: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "componentes": [c.to_dict() for c in self.componentes],
            "variables_faltantes": self.variables_faltantes,
            "confiable": self.confiable,
            "advertencias": self.advertencias,
        }


def _puntos_por_rangos(valor: float, rangos: list[dict[str, Any]]) -> int:
    """Recorre los rangos de mayor a menor y devuelve el primero cuyo 'desde'
    es <= valor. El ultimo rango tiene 'desde': null y matchea siempre."""
    for rango in rangos:
        desde = rango["desde"]
        if desde is None or valor >= desde:
            return rango["puntos"]
    # Inalcanzable si el JSON tiene su catch-all, pero mejor fallar fuerte
    # que devolver 0 en silencio en un score clinico.
    raise ValueError(f"ningun rango matcheo el valor {valor}: falta el catch-all en apache2.json")


def _mediciones(evento: Evento) -> dict[str, Any]:
    """Las mediciones del evento, con la presion arterial media derivada.

    El medico dicta '120 sobre 80', no la media. APACHE II usa la media, asi
    que la calcula Python: PAM = (sistolica + 2 * diastolica) / 3. Si el
    medico ya dicto la media, esa gana y no se toca."""
    med = dict(evento.payload_json.get("mediciones", {}) or {})
    if med.get("pam_mmhg") is None:
        tas, tad = med.get("tas_mmhg"), med.get("tad_mmhg")
        if tas is not None and tad is not None:
            med["pam_mmhg"] = round((tas + 2 * tad) / 3, 1)
    return med


def _peor_simple(eventos: list[Evento], clave: str) -> ComponenteApache:
    """Para una variable comun: calcula los puntos de cada medicion registrada
    y se queda con la que mas puntua."""
    tabla = _VARIABLES[clave]
    mejor: tuple[int, float, str] | None = None  # (puntos, valor, evento_id)

    for evento in eventos:
        valor = _mediciones(evento).get(clave)
        if valor is None:
            continue
        puntos = _puntos_por_rangos(valor, tabla["rangos"])
        if mejor is None or puntos > mejor[0]:
            mejor = (puntos, valor, evento.id)

    if mejor is None:
        return ComponenteApache(
            variable=clave, etiqueta=tabla["etiqueta"], valor=None, puntos=0,
            faltante=True, nota="sin mediciones en las primeras 24h, se asume normal (0 puntos)",
        )
    puntos, valor, evento_id = mejor
    return ComponenteApache(
        variable=clave, etiqueta=tabla["etiqueta"], valor=valor,
        puntos=puntos, evento_id=evento_id,
    )


def _componente_creatinina(eventos: list[Evento]) -> ComponenteApache:
    """Igual que una variable comun, pero los puntos se duplican si hubo falla
    renal aguda en cualquier evento de la ventana."""
    componente = _peor_simple(eventos, "creatinina_mg_dl")
    hay_falla_renal = any(e.payload_json.get("falla_renal_aguda") for e in eventos)
    if hay_falla_renal and not componente.faltante:
        componente.puntos *= 2
        componente.nota = "puntos duplicados por falla renal aguda"
    return componente


def _componente_ph(eventos: list[Evento]) -> ComponenteApache:
    """pH arterial, con el bicarbonato como sustituto si no hay ningun pH."""
    componente = _peor_simple(eventos, "ph_arterial")
    if not componente.faltante:
        return componente

    sustituto = _peor_simple(eventos, "hco3_meq_l")
    if sustituto.faltante:
        return componente  # no hay ni pH ni bicarbonato
    sustituto.nota = "sin pH arterial en la ventana, se uso bicarbonato como sustituto (APACHE II lo admite)"
    return sustituto


def _puntos_oxigenacion_de_evento(
    evento: Evento, presion_atmosferica: float
) -> tuple[int, float, str] | None:
    """Evalua la oxigenacion DENTRO de un solo evento, porque la rama depende
    del FiO2 de ese mismo gas. Devuelve (puntos, valor_usado, descripcion) o
    None si el gas esta incompleto."""
    med = _mediciones(evento)
    fio2 = med.get("fio2")
    if fio2 is None:
        return None

    if fio2 >= _OXIGENACION["umbral_fio2"]:
        aado2 = med.get("aado2_mmhg")
        if aado2 is None:
            pao2 = med.get("pao2_mmhg")
            paco2 = med.get("paco2_mmhg")
            if pao2 is None or paco2 is None:
                return None  # gas incompleto: no se puede calcular el gradiente
            aado2 = (
                fio2 * (presion_atmosferica - _OXIGENACION["presion_vapor_agua_mmhg"])
                - paco2 / _OXIGENACION["cociente_respiratorio"]
                - pao2
            )
        puntos = _puntos_por_rangos(aado2, _OXIGENACION["rangos_aado2"])
        return puntos, round(aado2, 1), f"gradiente A-a (FiO2 {fio2})"

    pao2 = med.get("pao2_mmhg")
    if pao2 is None:
        return None
    puntos = _puntos_por_rangos(pao2, _OXIGENACION["rangos_pao2"])
    return puntos, pao2, f"PaO2 (FiO2 {fio2})"


def _componente_oxigenacion(eventos: list[Evento], presion_atmosferica: float) -> ComponenteApache:
    mejor: tuple[int, float, str, str] | None = None  # (puntos, valor, nota, evento_id)
    hubo_gas_incompleto = False

    for evento in eventos:
        med = _mediciones(evento)
        resultado = _puntos_oxigenacion_de_evento(evento, presion_atmosferica)
        if resultado is None:
            # Solo cuenta como "incompleto" si el evento traia algo de gas.
            if any(k in med for k in ("fio2", "pao2_mmhg", "paco2_mmhg", "aado2_mmhg")):
                hubo_gas_incompleto = True
            continue
        puntos, valor, nota = resultado
        if mejor is None or puntos > mejor[0]:
            mejor = (puntos, valor, nota, evento.id)

    if mejor is None:
        nota = "sin gases arteriales completos en las primeras 24h, se asume normal (0 puntos)"
        if hubo_gas_incompleto:
            nota = "hay gases registrados pero incompletos (falta FiO2, PaO2 o PaCO2), se asume normal (0 puntos)"
        return ComponenteApache(
            variable="oxigenacion", etiqueta=_OXIGENACION["etiqueta"], valor=None,
            puntos=0, faltante=True, nota=nota,
        )

    puntos, valor, nota, evento_id = mejor
    return ComponenteApache(
        variable="oxigenacion", etiqueta=_OXIGENACION["etiqueta"], valor=valor,
        puntos=puntos, evento_id=evento_id, nota=nota,
    )


def _componente_glasgow(eventos: list[Evento]) -> ComponenteApache:
    """Puntos = 15 - GCS. El peor valor es el GCS mas bajo."""
    mejor: tuple[int, float, str] | None = None
    for evento in eventos:
        gcs = _mediciones(evento).get("glasgow")
        if gcs is None:
            continue
        puntos = _GLASGOW["max_gcs"] - gcs
        if mejor is None or puntos > mejor[0]:
            mejor = (puntos, gcs, evento.id)

    if mejor is None:
        return ComponenteApache(
            variable="glasgow", etiqueta=_GLASGOW["etiqueta"], valor=None, puntos=0,
            faltante=True, nota="sin Glasgow en las primeras 24h, se asume 15 (0 puntos)",
        )
    puntos, gcs, evento_id = mejor
    return ComponenteApache(
        variable="glasgow", etiqueta=_GLASGOW["etiqueta"], valor=gcs,
        puntos=puntos, evento_id=evento_id, nota=f"15 - {gcs:g}",
    )


def _componente_edad(episodio: Episodio) -> ComponenteApache:
    puntos = _puntos_por_rangos(episodio.edad, _EDAD["rangos"])
    return ComponenteApache(
        variable="edad", etiqueta=_EDAD["etiqueta"], valor=episodio.edad, puntos=puntos,
    )


def _componente_salud_cronica(episodio: Episodio) -> tuple[ComponenteApache, list[str]]:
    advertencias: list[str] = []
    if not episodio.enfermedad_cronica_grave:
        return (
            ComponenteApache(
                variable="salud_cronica", etiqueta=_SALUD_CRONICA["etiqueta"], valor=None,
                puntos=0, nota="sin enfermedad cronica grave preexistente",
            ),
            advertencias,
        )

    entrada = _SALUD_CRONICA["por_moting"].get(str(episodio.moting))
    if entrada is None:
        advertencias.append(f"MOTING {episodio.moting} sin mapeo de salud cronica, se asumen 0 puntos")
        return (
            ComponenteApache(
                variable="salud_cronica", etiqueta=_SALUD_CRONICA["etiqueta"], valor=None,
                puntos=0, faltante=True, nota=f"MOTING {episodio.moting} sin mapeo",
            ),
            advertencias,
        )

    if episodio.moting == 99:
        advertencias.append(
            "MOTING desconocido (99): se asumieron 5 puntos de salud cronica, la opcion conservadora"
        )
    return (
        ComponenteApache(
            variable="salud_cronica", etiqueta=_SALUD_CRONICA["etiqueta"],
            valor=None, puntos=entrada["puntos"], nota=entrada["categoria_knaus"],
        ),
        advertencias,
    )


def calcular(
    episodio: Episodio,
    eventos_24h: list[Evento],
    presion_atmosferica: float | None = None,
) -> ResultadoApache:
    """APACHE II del episodio. 'eventos_24h' ya viene filtrado a los eventos
    fisiologico_24h de las primeras 24hs (lo hace el proyector)."""
    if presion_atmosferica is None:
        presion_atmosferica = _OXIGENACION["presion_atmosferica_default_mmhg"]

    advertencias: list[str] = []
    componentes: list[ComponenteApache] = []

    for clave in _VARIABLES_SIMPLES:
        if clave == "creatinina_mg_dl":
            componentes.append(_componente_creatinina(eventos_24h))
        else:
            componentes.append(_peor_simple(eventos_24h, clave))

    componentes.append(_componente_ph(eventos_24h))
    componentes.append(_componente_oxigenacion(eventos_24h, presion_atmosferica))
    componentes.append(_componente_glasgow(eventos_24h))

    # Las 12 fisiologicas son las de arriba; edad y salud cronica no cuentan
    # para el criterio de "variables faltantes".
    faltantes = [c.variable for c in componentes if c.faltante]

    componentes.append(_componente_edad(episodio))
    componente_cronica, adv_cronica = _componente_salud_cronica(episodio)
    componentes.append(componente_cronica)
    advertencias.extend(adv_cronica)

    score = sum(c.puntos for c in componentes)
    score = max(0, min(score, _TABLAS["_meta"]["score_maximo"]))

    confiable = len(faltantes) <= MAX_VARIABLES_FALTANTES
    if not eventos_24h:
        advertencias.append(
            "no hay ningun evento fisiologico_24h en las primeras 24h: el score es solo edad y salud cronica"
        )
    elif faltantes:
        advertencias.append(
            f"{len(faltantes)} de 12 variables fisiologicas sin medicion en las primeras 24h "
            f"({', '.join(faltantes)}). APACHE II asume valor normal (0 puntos), "
            "lo que subestima la gravedad real."
        )
    if not confiable:
        advertencias.append(
            f"SCORE POCO CONFIABLE: faltan mas de {MAX_VARIABLES_FALTANTES} variables fisiologicas."
        )

    return ResultadoApache(
        score=score,
        componentes=componentes,
        variables_faltantes=faltantes,
        confiable=confiable,
        advertencias=advertencias,
    )


def probabilidad_muerte(resultado: ResultadoApache, episodio: Episodio) -> tuple[float, list[str]]:
    """Regresion logistica de Knaus 1985. Devuelve (porcentaje, advertencias).

    El peso de la categoria diagnostica va en 0 por decision explicita: MOTING
    solo tiene 4 categorias amplias y no mapean a las ~50 de Knaus. Ver la nota
    en schema/apache2.json."""
    advertencias: list[str] = []

    logit = _MORTALIDAD["intercepto"] + resultado.score * _MORTALIDAD["coef_apache"]
    if episodio.moting == _MORTALIDAD["moting_postop_urgencia"]:
        logit += _MORTALIDAD["coef_postop_urgencia"]
    logit += _MORTALIDAD["peso_categoria_diagnostica"]

    probabilidad = 100 / (1 + math.exp(-logit))
    probabilidad = round(
        max(_MORTALIDAD["min"], min(probabilidad, _MORTALIDAD["max"])),
        _MORTALIDAD["decimales"],
    )

    advertencias.append(
        "PROBABMORT calculada sin el peso de categoria diagnostica de Knaus "
        "(MOTING de SATI-Q no mapea a las categorias del paper). Sesgo sistematico conocido."
    )
    if not resultado.confiable:
        advertencias.append("PROBABMORT deriva de un APACHE II marcado como poco confiable.")

    return probabilidad, advertencias
