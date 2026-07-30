"""
Modelos del libro de movimientos: Episodio y Evento.
Shape y reglas de validacion viven en schema/eventos.json, no aca.
Este modulo solo sabe construir y validar instancias contra ese contrato.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).parent / "schema" / "eventos.json"
_EVENTOS_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

TIPOS_EVENTO = _EVENTOS_SCHEMA["evento"]["campos"]["tipo_evento"]["enum"]
UMBRAL_REVISION = _EVENTOS_SCHEMA["evento"]["umbral_revision"]
_PAYLOADS_SCHEMA = _EVENTOS_SCHEMA["payloads"]
_FUENTES_VALIDAS = _EVENTOS_SCHEMA["evento"]["campos"]["fuente"]["enum"]


class ErrorValidacion(Exception):
    pass


@dataclass
class Episodio:
    idcentro: str
    idpaciente: int
    reingreso: int
    fecha_ingreso: str  # ISO aaaa-mm-dd
    hora_ingreso: str  # HH:mm:ss
    tipo: str
    edad: int
    sexo: str
    moting: int
    procedencia: int
    enfermedad_cronica_grave: bool
    abierto: bool = True
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.reingreso not in (0, 1):
            raise ErrorValidacion(f"reingreso invalido: {self.reingreso}")
        if self.tipo not in ("A", "P", "N"):
            raise ErrorValidacion(f"tipo invalido: {self.tipo}")
        if self.tipo != "A":
            raise ErrorValidacion(
                "Alcance del hackathon: solo pacientes TIPO=A (adulto)."
            )
        if self.sexo not in ("M", "F", "O"):
            raise ErrorValidacion(f"sexo invalido: {self.sexo}")
        if self.moting not in (1, 2, 3, 4, 99):
            raise ErrorValidacion(f"moting invalido: {self.moting}")
        if not (1 <= self.procedencia <= 14):
            raise ErrorValidacion(f"procedencia invalida: {self.procedencia}")
        _validar_fecha_iso(self.fecha_ingreso, "fecha_ingreso")
        _validar_hora(self.hora_ingreso, "hora_ingreso")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "idcentro": self.idcentro,
            "idpaciente": self.idpaciente,
            "reingreso": self.reingreso,
            "fecha_ingreso": self.fecha_ingreso,
            "hora_ingreso": self.hora_ingreso,
            "tipo": self.tipo,
            "edad": self.edad,
            "sexo": self.sexo,
            "moting": self.moting,
            "procedencia": self.procedencia,
            "enfermedad_cronica_grave": self.enfermedad_cronica_grave,
            "abierto": self.abierto,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Episodio":
        return Episodio(**d)


@dataclass
class Evento:
    episodio_id: str
    timestamp_clinico: str  # ISO datetime aaaa-mm-ddTHH:mm:ss
    autor: str
    tipo_evento: str
    payload_json: dict[str, Any]
    fuente: str
    confianza: float
    texto_crudo: str
    corrige_a_evento_id: str | None = None
    timestamp_carga: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.tipo_evento not in TIPOS_EVENTO:
            raise ErrorValidacion(f"tipo_evento invalido: {self.tipo_evento}")
        if self.fuente not in _FUENTES_VALIDAS:
            raise ErrorValidacion(f"fuente invalida: {self.fuente}")
        if not (0.0 <= self.confianza <= 1.0):
            raise ErrorValidacion(f"confianza fuera de rango: {self.confianza}")
        if not self.texto_crudo:
            raise ErrorValidacion("texto_crudo es obligatorio")
        _validar_datetime_iso(self.timestamp_clinico, "timestamp_clinico")
        validar_payload(self.tipo_evento, self.payload_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "episodio_id": self.episodio_id,
            "timestamp_clinico": self.timestamp_clinico,
            "timestamp_carga": self.timestamp_carga,
            "autor": self.autor,
            "tipo_evento": self.tipo_evento,
            "payload_json": self.payload_json,
            "fuente": self.fuente,
            "confianza": self.confianza,
            "texto_crudo": self.texto_crudo,
            "corrige_a_evento_id": self.corrige_a_evento_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Evento":
        return Evento(**d)


def _validar_fecha_iso(valor: str, campo: str) -> None:
    try:
        datetime.strptime(valor, "%Y-%m-%d")
    except ValueError:
        raise ErrorValidacion(f"{campo} no es una fecha ISO valida (aaaa-mm-dd): {valor}")


def _validar_hora(valor: str, campo: str) -> None:
    try:
        datetime.strptime(valor, "%H:%M:%S")
    except ValueError:
        raise ErrorValidacion(f"{campo} no es una hora valida (HH:mm:ss): {valor}")


def _validar_datetime_iso(valor: str, campo: str) -> None:
    try:
        datetime.fromisoformat(valor)
    except ValueError:
        raise ErrorValidacion(f"{campo} no es un datetime ISO valido: {valor}")


_DISPOSITIVOS_VALIDOS = set(_EVENTOS_SCHEMA["catalogos"]["dispositivos"].keys())
_ADVERSOS_VALIDOS = set(_EVENTOS_SCHEMA["catalogos"]["eventos_adversos"].keys())
_RESULTADOS_VALIDOS = {int(k) for k in _EVENTOS_SCHEMA["catalogos"]["resultado_egreso"].keys()}


def validar_payload(tipo_evento: str, payload: dict[str, Any]) -> None:
    """Valida payload contra el shape declarado en schema/eventos.json para ese tipo_evento."""
    if tipo_evento in ("dispositivo_inicio", "dispositivo_fin"):
        _requerir(payload, ["dispositivo", "instancia_id"], tipo_evento)
        if payload["dispositivo"] not in _DISPOSITIVOS_VALIDOS:
            raise ErrorValidacion(f"dispositivo invalido: {payload['dispositivo']}")
        if not payload["instancia_id"]:
            raise ErrorValidacion("instancia_id no puede estar vacio")

    elif tipo_evento == "evento_adverso":
        _requerir(payload, ["codigo", "adjudicado_por"], tipo_evento)
        if payload["codigo"] not in _ADVERSOS_VALIDOS:
            raise ErrorValidacion(f"codigo de evento adverso invalido: {payload['codigo']}")
        if payload["adjudicado_por"] != "medico":
            raise ErrorValidacion("adjudicado_por debe ser 'medico': la infeccion la declara el medico, no el modelo")

    elif tipo_evento == "fisiologico_24h":
        if "mediciones" not in payload:
            raise ErrorValidacion("fisiologico_24h requiere 'mediciones'")

    elif tipo_evento == "tiss_diario":
        _requerir(payload, ["fecha"], tipo_evento)
        _validar_fecha_iso(payload["fecha"], "tiss_diario.fecha")
        if "items" not in payload and "puntaje_manual" not in payload:
            raise ErrorValidacion("tiss_diario requiere 'items' o 'puntaje_manual'")

    elif tipo_evento == "egreso":
        _requerir(payload, ["resultado"], tipo_evento)
        if payload["resultado"] not in _RESULTADOS_VALIDOS:
            raise ErrorValidacion(f"resultado invalido: {payload['resultado']}")

    else:
        raise ErrorValidacion(f"tipo_evento sin validador: {tipo_evento}")


def _requerir(payload: dict[str, Any], claves: list[str], tipo_evento: str) -> None:
    faltantes = [c for c in claves if c not in payload]
    if faltantes:
        raise ErrorValidacion(f"{tipo_evento}: faltan campos obligatorios en payload: {faltantes}")


def nuevo_evento(
    episodio_id: str,
    timestamp_clinico: str,
    autor: str,
    tipo_evento: str,
    payload_json: dict[str, Any],
    fuente: str,
    confianza: float,
    texto_crudo: str,
    corrige_a_evento_id: str | None = None,
) -> Evento:
    """Punto de entrada unico para crear un evento nuevo (incluye correcciones)."""
    return Evento(
        episodio_id=episodio_id,
        timestamp_clinico=timestamp_clinico,
        autor=autor,
        tipo_evento=tipo_evento,
        payload_json=payload_json,
        fuente=fuente,
        confianza=confianza,
        texto_crudo=texto_crudo,
        corrige_a_evento_id=corrige_a_evento_id,
    )


def requiere_revision(evento: Evento) -> bool:
    return evento.confianza < UMBRAL_REVISION
