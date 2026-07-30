"""
Catalogos legibles, leidos del schema.

Existe para que los nombres humanos ("Patologia medica", "Guardia") esten en
un solo lugar. Estaban escritos a mano en app.py y los necesitaba tambien
gemma.py: la segunda copia es la que se desincroniza.

Todo sale de schema/satiq_campos.json y schema/eventos.json, que ya tenian las
etiquetas. Nada se escribe dos veces.
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).parent
_CAMPOS = json.loads((_DIR / "schema" / "satiq_campos.json").read_text(encoding="utf-8"))["campos"]
_EVENTOS = json.loads((_DIR / "schema" / "eventos.json").read_text(encoding="utf-8"))


def _etiquetas(nombre_campo: str) -> dict[int, str]:
    """Las etiquetas de un campo del A4, con la clave como entero."""
    for c in _CAMPOS:
        if c["nombre"] == nombre_campo:
            return {int(k): v for k, v in c.get("validacion", {}).get("etiquetas", {}).items()}
    return {}


MOTIVOS_INGRESO = _etiquetas("MOTING")
PROCEDENCIAS = _etiquetas("PROCEDENCIA")
RESULTADOS = _etiquetas("RESULTADO")

DISPOSITIVOS = _EVENTOS["catalogos"]["dispositivos"]
ADVERSOS = {k: v["descripcion"] for k, v in _EVENTOS["catalogos"]["eventos_adversos"].items()}
MEDICIONES = _EVENTOS["payloads"]["fisiologico_24h"]["campos"]["mediciones"]["claves"]

SEXOS = {"M": "Masculino", "F": "Femenino", "O": "Otro"}
