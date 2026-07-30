"""
Persistencia insert-only para episodio y evento.
No hay UPDATE ni DELETE en este archivo, a proposito: los eventos son
inmutables. Corregir = insert_evento() con corrige_a_evento_id apuntando
al evento viejo.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from modelos import Episodio, Evento

_DDL = """
CREATE TABLE IF NOT EXISTS episodio (
    id                          TEXT PRIMARY KEY,
    idcentro                    TEXT NOT NULL,
    idpaciente                  INTEGER NOT NULL,
    reingreso                   INTEGER NOT NULL,
    fecha_ingreso                TEXT NOT NULL,
    hora_ingreso                 TEXT NOT NULL,
    tipo                        TEXT NOT NULL,
    edad                        INTEGER NOT NULL,
    sexo                        TEXT NOT NULL,
    moting                      INTEGER NOT NULL,
    procedencia                 INTEGER NOT NULL,
    enfermedad_cronica_grave    INTEGER NOT NULL,
    abierto                     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS evento (
    id                    TEXT PRIMARY KEY,
    episodio_id           TEXT NOT NULL REFERENCES episodio(id),
    timestamp_clinico     TEXT NOT NULL,
    timestamp_carga       TEXT NOT NULL,
    autor                 TEXT NOT NULL,
    tipo_evento           TEXT NOT NULL,
    payload_json          TEXT NOT NULL,
    fuente                TEXT NOT NULL,
    confianza             REAL NOT NULL,
    texto_crudo           TEXT NOT NULL,
    corrige_a_evento_id   TEXT REFERENCES evento(id)
);

CREATE INDEX IF NOT EXISTS idx_evento_episodio ON evento(episodio_id);
"""


def conectar(ruta_db: str | Path = "medtranscriptor.db") -> sqlite3.Connection:
    # check_same_thread=False porque Streamlit corre cada rerun del script en
    # un hilo distinto, y la conexion queda cacheada entre reruns. Sin esto
    # salta "SQLite objects created in a thread can only be used in that same
    # thread" apenas se interactua con la app.
    #
    # Es seguro en este uso: sqlite3 serializa los accesos y aca escribe un
    # solo usuario por vez. Si alguna vez esto atiende varias sesiones que
    # escriben en paralelo, hay que poner un lock alrededor de los INSERT.
    con = sqlite3.connect(ruta_db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(_DDL)
    return con


def insert_episodio(con: sqlite3.Connection, episodio: Episodio) -> str:
    d = episodio.to_dict()
    con.execute(
        """INSERT INTO episodio
           (id, idcentro, idpaciente, reingreso, fecha_ingreso, hora_ingreso,
            tipo, edad, sexo, moting, procedencia, enfermedad_cronica_grave, abierto)
           VALUES (:id, :idcentro, :idpaciente, :reingreso, :fecha_ingreso, :hora_ingreso,
                   :tipo, :edad, :sexo, :moting, :procedencia, :enfermedad_cronica_grave, :abierto)""",
        {**d, "enfermedad_cronica_grave": int(d["enfermedad_cronica_grave"]), "abierto": int(d["abierto"])},
    )
    con.commit()
    return episodio.id


def insert_evento(con: sqlite3.Connection, evento: Evento) -> str:
    d = evento.to_dict()
    con.execute(
        """INSERT INTO evento
           (id, episodio_id, timestamp_clinico, timestamp_carga, autor, tipo_evento,
            payload_json, fuente, confianza, texto_crudo, corrige_a_evento_id)
           VALUES (:id, :episodio_id, :timestamp_clinico, :timestamp_carga, :autor, :tipo_evento,
                   :payload_json, :fuente, :confianza, :texto_crudo, :corrige_a_evento_id)""",
        {**d, "payload_json": json.dumps(d["payload_json"], ensure_ascii=False)},
    )
    con.commit()
    return evento.id


def get_episodio(con: sqlite3.Connection, episodio_id: str) -> Episodio | None:
    fila = con.execute("SELECT * FROM episodio WHERE id = ?", (episodio_id,)).fetchone()
    if fila is None:
        return None
    d = dict(fila)
    d["enfermedad_cronica_grave"] = bool(d["enfermedad_cronica_grave"])
    d["abierto"] = bool(d["abierto"])
    return Episodio.from_dict(d)


def get_eventos(con: sqlite3.Connection, episodio_id: str) -> list[Evento]:
    """Todos los eventos del episodio, incluidos los corregidos. El filtro de
    vigencia lo hace el proyector, no la capa de persistencia."""
    filas = con.execute(
        "SELECT * FROM evento WHERE episodio_id = ? ORDER BY timestamp_clinico, timestamp_carga",
        (episodio_id,),
    ).fetchall()
    eventos = []
    for fila in filas:
        d = dict(fila)
        d["payload_json"] = json.loads(d["payload_json"])
        eventos.append(Evento.from_dict(d))
    return eventos


def listar_episodios(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """La columna 'abierto' guarda el valor de alta (siempre True) y no se
    vuelve a tocar: no hay UPDATE en este archivo. Si un episodio sigue
    abierto o no se deriva de si tiene un evento 'egreso' vigente -- ver
    proyector.episodio_abierto(). Este listado es solo para elegir episodio
    en la UI."""
    filas = con.execute("SELECT * FROM episodio").fetchall()
    return [dict(f) for f in filas]
