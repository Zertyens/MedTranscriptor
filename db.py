"""
Persistencia de episodio y evento.

LOS EVENTOS SON INMUTABLES. No hay UPDATE ni DELETE sobre 'evento' en este
archivo, a proposito: corregir = insert_evento() con corrige_a_evento_id
apuntando al evento viejo.

EL EPISODIO SI SE PUEDE CORREGIR, y la diferencia no es un descuido. El
episodio no es un movimiento clinico: es la cabecera administrativa de la
cuenta (edad, sexo, motivo de ingreso). Si se tipeo mal la edad al admitir,
eso es un error de carga, no un hecho clinico que haya que contraasentar.

Pero corregirlo NO es gratis: la edad alimenta los puntos de edad del
APACHE II, asi que un cambio silencioso alteraria un puntaje de gravedad sin
dejar rastro. Por eso actualizar_episodio() escribe ademas una fila por cada
campo modificado en 'episodio_cambio', con el valor anterior, el nuevo, quien
lo cambio y cuando. Nada desaparece.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
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

-- Auditoria de correcciones a la cabecera del episodio. Insert-only:
-- una fila por campo corregido, para que ninguna edicion sea silenciosa.
CREATE TABLE IF NOT EXISTS episodio_cambio (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    episodio_id     TEXT NOT NULL REFERENCES episodio(id),
    campo           TEXT NOT NULL,
    valor_anterior  TEXT,
    valor_nuevo     TEXT,
    autor           TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cambio_episodio ON episodio_cambio(episodio_id);

-- Valores del reporte cargados a mano, pisando lo que da la proyeccion.
-- Existe porque a veces el medico sabe algo que los registros no sostienen y
-- SATI-Q exige un valor igual. NO se mezcla con los eventos: un ajuste no es
-- un hecho clinico, es una correccion al reporte, y tiene que verse distinto.
-- Insert-only: para volver atras se inserta una fila con anulado=1.
CREATE TABLE IF NOT EXISTS ajuste_manual (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    episodio_id  TEXT NOT NULL REFERENCES episodio(id),
    campo        TEXT NOT NULL,
    valor        TEXT,
    motivo       TEXT,
    autor        TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    anulado      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ajuste_episodio ON ajuste_manual(episodio_id);
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


# Lo unico que se puede corregir del episodio. Ni el id ni el idcentro:
# corregir esos seria otro episodio, no una correccion.
CAMPOS_CORREGIBLES = frozenset({
    "idpaciente", "reingreso", "fecha_ingreso", "hora_ingreso",
    "edad", "sexo", "moting", "procedencia", "enfermedad_cronica_grave",
})


def actualizar_episodio(
    con: sqlite3.Connection, episodio: Episodio, cambios: dict[str, Any], autor: str
) -> list[str]:
    """Corrige la cabecera del episodio dejando auditoria de cada campo.

    Devuelve la descripcion de los cambios aplicados. Los campos que no
    cambiaron de valor no se tocan ni se registran."""
    invalidos = set(cambios) - CAMPOS_CORREGIBLES
    if invalidos:
        raise ValueError(f"campos no corregibles: {sorted(invalidos)}")

    actuales = episodio.to_dict()
    efectivos = {c: v for c, v in cambios.items() if actuales.get(c) != v}
    if not efectivos:
        return []

    # Se revalida el episodio completo antes de guardar: una edad de 8 anios
    # en un adulto tiene que rebotar aca, no al exportar el CSV.
    Episodio.from_dict({**actuales, **efectivos})

    ahora = datetime.now().isoformat(timespec="seconds")
    resumen: list[str] = []
    for campo, nuevo in efectivos.items():
        con.execute(
            """INSERT INTO episodio_cambio
               (episodio_id, campo, valor_anterior, valor_nuevo, autor, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (episodio.id, campo, str(actuales.get(campo)), str(nuevo), autor, ahora),
        )
        valor = int(nuevo) if isinstance(nuevo, bool) else nuevo
        con.execute(f"UPDATE episodio SET {campo} = ? WHERE id = ?", (valor, episodio.id))
        resumen.append(f"{campo}: {actuales.get(campo)} → {nuevo}")

    con.commit()
    return resumen


def historial_episodio(con: sqlite3.Connection, episodio_id: str) -> list[dict[str, Any]]:
    filas = con.execute(
        "SELECT * FROM episodio_cambio WHERE episodio_id = ? ORDER BY id DESC",
        (episodio_id,),
    ).fetchall()
    return [dict(f) for f in filas]


def insert_ajuste(
    con: sqlite3.Connection, episodio_id: str, campo: str, valor: Any,
    motivo: str, autor: str, anulado: bool = False,
) -> None:
    """Registra un valor cargado a mano para un campo del reporte."""
    con.execute(
        """INSERT INTO ajuste_manual
           (episodio_id, campo, valor, motivo, autor, timestamp, anulado)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (episodio_id, campo, None if valor is None else str(valor), motivo, autor,
         datetime.now().isoformat(timespec="seconds"), int(anulado)),
    )
    con.commit()


def ajustes_vigentes(con: sqlite3.Connection, episodio_id: str) -> dict[str, dict[str, Any]]:
    """El ajuste vigente por campo: gana el ultimo insertado, salvo que ese
    ultimo sea una anulacion. Las filas viejas no se borran nunca."""
    filas = con.execute(
        "SELECT * FROM ajuste_manual WHERE episodio_id = ? ORDER BY id",
        (episodio_id,),
    ).fetchall()
    vigentes: dict[str, dict[str, Any]] = {}
    for f in filas:
        d = dict(f)
        if d["anulado"]:
            vigentes.pop(d["campo"], None)
        else:
            vigentes[d["campo"]] = d
    return vigentes


def historial_ajustes(con: sqlite3.Connection, episodio_id: str) -> list[dict[str, Any]]:
    filas = con.execute(
        "SELECT * FROM ajuste_manual WHERE episodio_id = ? ORDER BY id DESC",
        (episodio_id,),
    ).fetchall()
    return [dict(f) for f in filas]


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
