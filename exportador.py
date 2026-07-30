"""
Exportacion de la fila proyectada al CSV de SATI-Q.

El orden de columnas sale de schema/satiq_campos.json, que es el header
literal del Anexo A4. No hay ninguna lista de nombres escrita a mano aca.

Por defecto se niega a exportar si la validacion encontro errores: un CSV
mal formado que el registro nacional rechaza es peor que no tener CSV.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from validador import CAMPOS, FORMATO, Hallazgo, errores, validar_fila

NOMBRES_COLUMNAS = [c["nombre"] for c in CAMPOS]


class ErrorExportacion(Exception):
    pass


def _formatear(valor: Any, definicion: dict[str, Any]) -> str:
    """Los decimales van con punto y con el minimo de digitos necesario.
    El Anexo A4 escribe 17.7, no 17.70, y 49 en vez de 49.0."""
    if valor is None:
        return ""

    tipo = definicion.get("validacion", {}).get("tipo")
    if tipo == "decimal":
        decimales = definicion["validacion"].get("decimales", FORMATO["decimal_max_digitos"])
        texto = f"{float(valor):.{decimales}f}"
        if "." in texto:
            texto = texto.rstrip("0").rstrip(".")
        return texto or "0"
    return str(valor)


def fila_a_csv(fila: dict[str, Any]) -> list[str]:
    """La fila como lista de strings, en el orden del Anexo A4."""
    return [_formatear(fila.get(d["nombre"]), d) for d in CAMPOS]


def exportar_csv(
    filas: list[dict[str, Any]],
    destino: str | Path | None = None,
    incluir_header: bool = True,
    forzar: bool = False,
) -> str:
    """Exporta una o mas filas proyectadas. Devuelve el CSV como texto y, si
    se pasa 'destino', tambien lo escribe a disco.

    forzar=True exporta aunque haya errores de validacion. Existe solo para
    poder mirar un CSV roto mientras se debuggea; no usarlo para entregar."""
    if not forzar:
        for i, fila in enumerate(filas, start=1):
            problemas = errores(validar_fila(fila))
            if problemas:
                detalle = "\n  ".join(str(p) for p in problemas)
                raise ErrorExportacion(
                    f"La fila {i} tiene {len(problemas)} error(es) de validacion y no se puede exportar:\n  {detalle}"
                )

    buffer = io.StringIO()
    escritor = csv.writer(buffer, delimiter=FORMATO["csv_delimitador"], lineterminator="\r\n")
    if incluir_header:
        escritor.writerow(NOMBRES_COLUMNAS)
    for fila in filas:
        escritor.writerow(fila_a_csv(fila))

    contenido = buffer.getvalue()
    if destino is not None:
        Path(destino).write_text(contenido, encoding=FORMATO["csv_encoding"], newline="")
    return contenido
