"""
Validacion de la fila proyectada contra las reglas de schema/satiq_campos.json.

No hay ninguna regla escrita a mano aca. El validador es generico: lee el
bloque 'validacion' de cada campo y lo aplica. Si SATI-Q cambia un rango, se
cambia el JSON y este archivo no se toca.

Distingue dos severidades:
  ERROR       -> el CSV no se puede exportar. Valor fuera de rango, tipo
                 equivocado, campo obligatorio sin calcular.
  ADVERTENCIA -> el CSV se exporta igual, pero hay algo que el humano tiene
                 que mirar (score poco confiable, variables faltantes).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = json.loads(
    (Path(__file__).parent / "schema" / "satiq_campos.json").read_text(encoding="utf-8")
)
CAMPOS = _SCHEMA["campos"]
FORMATO = _SCHEMA["_meta"]["formato"]

ERROR = "ERROR"
ADVERTENCIA = "ADVERTENCIA"


@dataclass
class Hallazgo:
    campo: str
    severidad: str
    mensaje: str

    def __str__(self) -> str:
        return f"[{self.severidad}] {self.campo}: {self.mensaje}"


def _es_entero(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _es_numero(valor: Any) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _validar_tipo(campo: str, valor: Any, reglas: dict[str, Any]) -> list[Hallazgo]:
    tipo = reglas.get("tipo")
    hallazgos: list[Hallazgo] = []

    if tipo == "entero":
        if not _es_entero(valor):
            hallazgos.append(Hallazgo(campo, ERROR, f"se esperaba un entero, llego {valor!r}"))
    elif tipo == "decimal":
        if not _es_numero(valor):
            hallazgos.append(Hallazgo(campo, ERROR, f"se esperaba un numero, llego {valor!r}"))
        else:
            decimales = reglas.get("decimales", FORMATO["decimal_max_digitos"])
            if round(float(valor), decimales) != float(valor):
                hallazgos.append(
                    Hallazgo(campo, ERROR, f"mas de {decimales} decimales: {valor!r}")
                )
    elif tipo == "texto":
        if not isinstance(valor, str):
            hallazgos.append(Hallazgo(campo, ERROR, f"se esperaba texto, llego {valor!r}"))
        else:
            if "min_largo" in reglas and len(valor) < reglas["min_largo"]:
                hallazgos.append(Hallazgo(campo, ERROR, f"texto mas corto que {reglas['min_largo']}"))
            if "max_largo" in reglas and len(valor) > reglas["max_largo"]:
                hallazgos.append(Hallazgo(campo, ERROR, f"texto mas largo que {reglas['max_largo']}"))
    elif tipo == "fecha":
        if not isinstance(valor, str):
            hallazgos.append(Hallazgo(campo, ERROR, f"se esperaba una fecha, llego {valor!r}"))
        else:
            try:
                datetime.strptime(valor, "%d/%m/%Y")
            except ValueError:
                hallazgos.append(Hallazgo(campo, ERROR, f"fecha no cumple dd/mm/aaaa: {valor!r}"))
    elif tipo == "hora":
        if not isinstance(valor, str):
            hallazgos.append(Hallazgo(campo, ERROR, f"se esperaba una hora, llego {valor!r}"))
        else:
            try:
                datetime.strptime(valor, "%H:%M:%S")
            except ValueError:
                hallazgos.append(Hallazgo(campo, ERROR, f"hora no cumple HH:mm:ss: {valor!r}"))

    return hallazgos


def _validar_rango(campo: str, valor: Any, limites: dict[str, Any]) -> list[Hallazgo]:
    if not _es_numero(valor):
        return []
    hallazgos: list[Hallazgo] = []
    if "min" in limites and valor < limites["min"]:
        hallazgos.append(Hallazgo(campo, ERROR, f"{valor} es menor que el minimo {limites['min']}"))
    if "max" in limites and valor > limites["max"]:
        hallazgos.append(Hallazgo(campo, ERROR, f"{valor} es mayor que el maximo {limites['max']}"))
    return hallazgos


def _validar_condicional(
    campo: str, valor: Any, condicional: dict[str, Any], fila: dict[str, Any]
) -> list[Hallazgo]:
    """Dos formas de condicional en el schema:
    - {campo, si_vale, entonces, si_no}  -> los 15 pares flag/NUM
    - {campo, casos}                     -> EDAD segun TIPO de paciente
    """
    campo_ref = condicional["campo"]
    valor_ref = fila.get(campo_ref)

    if "casos" in condicional:
        caso = condicional["casos"].get(valor_ref)
        if caso is None:
            return [Hallazgo(campo, ADVERTENCIA, f"no hay caso definido para {campo_ref}={valor_ref!r}")]
        hallazgos = _validar_rango(campo, valor, caso)
        for h in hallazgos:
            h.mensaje += f" (para {campo_ref}={valor_ref!r}, en {caso.get('unidad', '')})"
        return hallazgos

    limites = condicional["entonces"] if valor_ref == condicional["si_vale"] else condicional["si_no"]
    hallazgos = _validar_rango(campo, valor, limites)
    for h in hallazgos:
        h.mensaje += f" (porque {campo_ref}={valor_ref!r})"
    return hallazgos


def _validar_comparacion(campo: str, valor: Any, reglas: dict[str, Any], fila: dict[str, Any]) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []

    otro = reglas.get("no_anterior_a")
    if otro and isinstance(valor, str) and isinstance(fila.get(otro), str):
        try:
            if datetime.strptime(valor, "%d/%m/%Y") < datetime.strptime(fila[otro], "%d/%m/%Y"):
                hallazgos.append(Hallazgo(campo, ERROR, f"{valor} es anterior a {otro}={fila[otro]}"))
        except ValueError:
            pass  # el error de formato ya lo reporta _validar_tipo

    otro = reglas.get("no_menor_que")
    if otro and _es_numero(valor) and _es_numero(fila.get(otro)):
        if valor < fila[otro]:
            hallazgos.append(Hallazgo(campo, ERROR, f"{valor} es menor que {otro}={fila[otro]}"))

    return hallazgos


def validar_fila(fila: dict[str, Any], advertencias_proyeccion: dict[str, list[str]] | None = None) -> list[Hallazgo]:
    """Valida los 49 campos. 'advertencias_proyeccion' es lo que devolvio el
    proyector (advertencias_fila) y se incorpora como ADVERTENCIA."""
    hallazgos: list[Hallazgo] = []

    for definicion in CAMPOS:
        nombre = definicion["nombre"]
        reglas = definicion.get("validacion", {})
        valor = fila.get(nombre)

        if valor is None:
            hallazgos.append(
                Hallazgo(nombre, ERROR, "sin valor: el campo no se pudo calcular (revisar si falta el evento de egreso o los registros TISS)")
            )
            continue

        hallazgos.extend(_validar_tipo(nombre, valor, reglas))

        if "enum" in reglas and valor not in reglas["enum"]:
            hallazgos.append(Hallazgo(nombre, ERROR, f"{valor!r} no esta en los valores permitidos {reglas['enum']}"))

        if "condicional" in reglas:
            hallazgos.extend(_validar_condicional(nombre, valor, reglas["condicional"], fila))
        else:
            hallazgos.extend(_validar_rango(nombre, valor, reglas))

        hallazgos.extend(_validar_comparacion(nombre, valor, reglas, fila))

    for campo, mensajes in (advertencias_proyeccion or {}).items():
        for mensaje in mensajes:
            hallazgos.append(Hallazgo(campo, ADVERTENCIA, mensaje))

    return hallazgos


def errores(hallazgos: list[Hallazgo]) -> list[Hallazgo]:
    return [h for h in hallazgos if h.severidad == ERROR]


def advertencias(hallazgos: list[Hallazgo]) -> list[Hallazgo]:
    return [h for h in hallazgos if h.severidad == ADVERTENCIA]


def es_exportable(hallazgos: list[Hallazgo]) -> bool:
    return not errores(hallazgos)
