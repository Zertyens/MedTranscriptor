"""
MedTranscriptor — Registro de internación en Terapia Intensiva
==============================================================

Interfaz clínica. El médico dicta la evolución, el sistema la convierte en
eventos fechados, y al egreso proyecta la fila del registro nacional SATI-Q
con cada número trazable hasta la frase que lo originó.

DECISIONES DE DISEÑO
--------------------
1. Es una APLICACIÓN CLÍNICA, no un formulario. Densidad alta, jerarquía
   tipográfica clara, y color únicamente con significado:
       ámbar = requiere revisión humana
       rojo  = bloquea el reporte
       azul  = valor derivado por el sistema (nadie lo cargó a mano)
   Todo lo demás es neutro y hereda el tema de Streamlit, así funciona igual
   en claro y en oscuro.

2. NINGÚN CÁLCULO OCURRE ACÁ. Todos los números salen de proyectar_fila().
   Si un dato clínico necesita aritmética, ya está calculado en el motor.

3. El paciente es el contexto permanente: se elige en el panel lateral y su
   estado está siempre visible arriba. Sin paciente seleccionado no hay nada
   que hacer, así que la app lo pide antes que cualquier otra cosa.

4. Las herramientas de demostración viven en un desplegable cerrado al fondo
   del panel lateral. Un médico no tiene por qué ver botones que cargan
   pacientes de prueba.

5. El vocabulario es clínico, no técnico. El médico lee "Se colocó catéter
   venoso central en subclavia derecha", no el JSON que hay debajo. El dato
   técnico queda accesible para quien audite el sistema.

Los archivos del motor (modelos, db, proyector, apache2, validador,
exportador, gemma, voz) no se tocan desde acá.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import streamlit as st

import db
import gemma
import modelos
import semilla
import validador
from exportador import NOMBRES_COLUMNAS as exportador_columnas
from exportador import exportar_csv
from proyector import advertencias_fila, fila_a_valores, proyectar_fila

st.set_page_config(
    page_title="MedTranscriptor",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sistema visual
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
  :root {
    --acento: #0ea5e9;
    --revisar: #f59e0b;
    --error: #ef4444;
    --ok: #10b981;
    --superficie: rgba(128,128,128,0.06);
    --borde: rgba(128,128,128,0.22);
  }

  /* Menos aire vertical: es una app de trabajo, no una landing */
  .block-container { padding-top: 2.2rem; max-width: 1500px; }
  h1, h2, h3 { letter-spacing: -0.015em; }

  /* --- Ficha del paciente, siempre visible --- */
  .ficha {
    display: flex; align-items: center; gap: 2rem; flex-wrap: wrap;
    padding: 0.9rem 1.2rem; margin-bottom: 1.2rem;
    background: var(--superficie);
    border: 1px solid var(--borde);
    border-left: 4px solid var(--acento);
    border-radius: 10px;
  }
  .ficha-id { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.02em; }
  .ficha-dato { display: flex; flex-direction: column; gap: 0.1rem; }
  .ficha-rotulo {
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em;
    opacity: 0.5; font-weight: 600;
  }
  .ficha-valor { font-size: 0.98rem; font-weight: 600; }
  .ficha-estado {
    margin-left: auto; padding: 0.3rem 0.8rem; border-radius: 9999px;
    font-size: 0.8rem; font-weight: 700;
  }
  .estado-curso { background: rgba(14,165,233,0.15); color: var(--acento); }
  .estado-cerrado { background: rgba(16,185,129,0.15); color: var(--ok); }

  /* --- Eventos --- */
  .evento {
    padding: 0.7rem 0.9rem; margin-bottom: 0.5rem;
    background: var(--superficie);
    border-left: 3px solid var(--borde);
    border-radius: 0 8px 8px 0;
  }
  .evento-revisar { border-left-color: var(--revisar); background: rgba(245,158,11,0.07); }
  .evento-titulo { font-size: 1rem; font-weight: 600; line-height: 1.45; }
  .evento-linea {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 1rem; flex-wrap: wrap;
  }
  .evento-hora {
    font-variant-numeric: tabular-nums; font-weight: 700;
    opacity: 0.5; font-size: 0.85rem; white-space: nowrap;
  }
  .cita {
    margin-top: 0.35rem; padding-left: 0.7rem;
    border-left: 2px solid var(--borde);
    font-size: 0.88rem; font-style: italic; opacity: 0.72;
  }
  .marca {
    display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px;
    font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .marca-revisar { background: rgba(245,158,11,0.18); color: var(--revisar); }
  .marca-corrige { background: rgba(128,128,128,0.15); opacity: 0.8; }

  /* --- Campos del registro SATI-Q --- */
  .campo {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 1rem; padding: 0.55rem 0; border-bottom: 1px solid var(--borde);
  }
  .campo-nombre { font-size: 0.9rem; line-height: 1.35; }
  .campo-codigo {
    font-family: ui-monospace, monospace; font-size: 0.7rem;
    opacity: 0.4; margin-left: 0.4rem;
  }
  .campo-valor {
    font-size: 1.15rem; font-weight: 700; white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .valor-derivado { color: var(--acento); }
  /* Los cargados a mano se ven distinto a propósito: que un número del
     reporte no venga de los registros es información, no un detalle. */
  .valor-manual { color: var(--revisar); }
  .marca-manual {
    background: rgba(245,158,11,0.18); color: var(--revisar);
    font-size: 0.62rem; vertical-align: middle; margin-left: 0.4rem;
    padding: 0.1rem 0.4rem; border-radius: 4px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.03em;
  }
  .valor-vacio { opacity: 0.3; font-weight: 400; font-size: 0.95rem; }

  .dia-sep {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
    font-weight: 700; opacity: 0.45; margin: 1.2rem 0 0.5rem 0;
    padding-bottom: 0.25rem; border-bottom: 1px solid var(--borde);
  }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Motor: conexión y modelos
# ---------------------------------------------------------------------------
@st.cache_resource
def conexion():
    return db.conectar("medtranscriptor.db")


@st.cache_resource(show_spinner=False)
def modelo_voz(nombre: str):
    import voz

    return voz.cargar_modelo(nombre)


def transcribir(ruta: Path, modelo: str) -> tuple[str | None, str | None]:
    """(texto, error). El error se muestra: tragarse la excepción deja al
    usuario sin saber por qué no anda."""
    try:
        modelo_voz(modelo)
        import voz

        texto, _ = voz.transcribir(ruta, modelo)
        return (texto or None), None
    except ImportError:
        return None, "El transcriptor no está instalado (pip install faster-whisper)."
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


con = conexion()

# ---------------------------------------------------------------------------
# Catálogos: los nombres legibles salen del schema, no se escriben dos veces
# ---------------------------------------------------------------------------
from catalogos import (  # noqa: E402  (después de configurar la página)
    ADVERSOS, DISPOSITIVOS, MEDICIONES, MOTIVOS_INGRESO, PROCEDENCIAS, SEXOS,
)

_SCHEMA = json.loads(Path("schema/eventos.json").read_text(encoding="utf-8"))
RESULTADOS = _SCHEMA["catalogos"]["resultado_egreso"]
CAMPOS_SATIQ = {c["nombre"]: c for c in validador.CAMPOS}

ICONOS = {
    "dispositivo_inicio": "🔵", "dispositivo_fin": "⚪", "evento_adverso": "🔶",
    "fisiologico_24h": "📈", "tiss_diario": "📋", "egreso": "🏁",
}

_RELLENOS = {
    "no mencionado", "no especificado", "no especifica", "desconocido", "n/a", "na",
    "none", "null", "no aplica", "sin especificar", "no indicado",
}


def _util(valor) -> str:
    """Descarta rellenos tipo 'no mencionado'. gemma.py ya los filtra al
    normalizar, pero los eventos guardados antes de ese arreglo los tienen."""
    if not isinstance(valor, str) or valor.strip().lower() in _RELLENOS:
        return ""
    return valor.strip()


def describir(ev) -> str:
    """El evento, dicho en castellano clínico."""
    p, tipo = ev.payload_json, ev.tipo_evento

    if tipo == "dispositivo_inicio":
        sitio = _util(p.get("sitio"))
        return f"Se colocó {DISPOSITIVOS.get(p.get('dispositivo'), '').lower()}" + (f" en {sitio}" if sitio else "")

    if tipo == "dispositivo_fin":
        motivo = {"accidental": " (retiro accidental)", "recambio": " (por recambio)"}.get(p.get("motivo"), "")
        return f"Se retiró {DISPOSITIVOS.get(p.get('dispositivo'), '').lower()}{motivo}"

    if tipo == "evento_adverso":
        return ADVERSOS.get(p.get("codigo"), p.get("codigo", ""))

    if tipo == "fisiologico_24h":
        med = p.get("mediciones") or {}
        if not med:
            return "Registro de valores fisiológicos"
        partes = [f"{MEDICIONES.get(k, k).split(',')[0]} {v}" for k, v in med.items()]
        texto = " · ".join(partes)
        return texto + (" · falla renal aguda" if p.get("falla_renal_aguda") else "")

    if tipo == "tiss_diario":
        pts = p.get("puntaje_manual")
        return f"Carga de enfermería del día: {pts} puntos" if pts is not None else "Carga de enfermería del día"

    if tipo == "egreso":
        return f"Egreso de la unidad → {RESULTADOS.get(str(p.get('resultado')), '')}"

    return tipo


def instancia(ev) -> str:
    """CVC-2 se lee '2º catéter venoso central'."""
    inst = (ev.payload_json or {}).get("instancia_id") or ""
    codigo, _, num = inst.partition("-")
    if not num.isdigit() or num == "1":
        return ""
    return f"{num}º {DISPOSITIVOS.get(codigo, codigo).lower()}"


def html(bloque: str) -> str:
    """Aplasta el HTML a una sola línea antes de mandarlo a st.markdown.

    Streamlit pasa el contenido por su parser de markdown incluso con
    unsafe_allow_html: cualquier línea indentada 4 espacios o más la toma como
    bloque de código y escupe los tags como texto plano. Sin esto se ven
    '</div>' sueltos en la pantalla."""
    return "".join(linea.strip() for linea in bloque.splitlines())


FIRMA_VACIA = "Dr./Dra."
_TITULOS = {"dr", "dra", "dr.", "dra.", "dr./dra.", "doctor", "doctora", "medico", "médico"}


def firma_valida(autor: str | None) -> bool:
    """Una firma sirve si identifica a una persona.

    Un registro clínico sin autor identificable no vale nada: la trazabilidad
    entera se apoya en saber quién dijo qué. El placeholder 'Dr./Dra.' no
    alcanza, y un título solo tampoco."""
    if not autor:
        return False
    palabras = [p for p in autor.replace("/", " ").split() if p.strip()]
    utiles = [p for p in palabras if p.lower().strip(".") not in _TITULOS]
    return bool(utiles) and len("".join(utiles)) >= 3


def firma_actual() -> str:
    return (st.session_state.get("autor") or "").strip()


def aviso_firma() -> None:
    st.caption("⚠️ Poné tu nombre en **Firma** para poder guardar. Todo registro queda firmado.")


def fecha_larga(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso[:10]


# ---------------------------------------------------------------------------
# Pacientes
# ---------------------------------------------------------------------------
def pacientes() -> list[modelos.Episodio]:
    return [modelos.Episodio.from_dict(d) for d in db.listar_episodios(con)]


def esta_cerrado(eventos) -> bool:
    return any(e.tipo_evento == "egreso" for e in eventos if not e.corrige_a_evento_id)


@st.dialog("Admitir paciente en la unidad", width="large")
def dialogo_nuevo_paciente():
    previos = pacientes()

    st.markdown("##### Dictá la admisión")
    st.caption("Contá quién ingresa como se lo dirías a un colega. Después revisás y completás lo que falte.")

    audio_adm = st.audio_input("Admisión", label_visibility="collapsed")
    if audio_adm is not None:
        crudo = audio_adm.getvalue()
        firma = hashlib.md5(crudo).hexdigest()
        if st.session_state.get("firma_admision") != firma:
            st.session_state["firma_admision"] = firma
            carpeta = Path("demo/audio")
            carpeta.mkdir(parents=True, exist_ok=True)
            ruta = carpeta / f"admision_{datetime.now():%Y%m%d_%H%M%S}.wav"
            ruta.write_bytes(crudo)
            with st.spinner("Transcribiendo…"):
                texto_adm, err = transcribir(ruta, st.session_state.get("modelo_whisper", "small"))
            if texto_adm:
                with st.spinner("Interpretando la admisión…"):
                    try:
                        st.session_state["ingreso_dictado"] = gemma.interpretar_ingreso(
                            texto_adm, date.today().isoformat()
                        )
                        st.session_state["texto_admision"] = texto_adm
                    except gemma.ErrorGemma as e:
                        st.error(str(e))
            else:
                st.error(err or "No se detectó voz.")
            # OJO: NO va st.rerun() acá. Dentro de un diálogo, st.rerun() lo
            # CIERRA y se pierde todo lo cargado. Los datos ya quedaron en
            # session_state y los widgets de abajo se dibujan en esta misma
            # pasada, así que no hace falta rerun.

    d = st.session_state.get("ingreso_dictado") or {}
    if st.session_state.get("texto_admision"):
        st.caption(f"Se escuchó: *“{st.session_state['texto_admision']}”*")
    if d.get("no_entendido"):
        st.caption("No se entendió: " + " · ".join(d["no_entendido"]))

    st.divider()
    st.markdown("##### Datos del ingreso")
    if d:
        st.caption("Precargado con lo que dictaste. Revisalo y completá lo que quedó vacío.")

    # --- Reingreso: se elige el paciente, no se tipea el número ---
    reing = st.checkbox(
        "Es un reingreso (vuelve a la unidad antes de 48 h)",
        value=bool(d.get("reingreso")),
        help="Un reingreso es un episodio nuevo del MISMO paciente, así que comparte su número.",
    )

    if reing and previos:
        # El numero de paciente identifica a la PERSONA, no al episodio: un
        # reingreso reusa el mismo. Tipearlo a mano es la forma segura de
        # equivocarse, asi que se elige de la lista.
        opciones = sorted({e.idpaciente for e in previos})
        idpac = st.selectbox(
            "¿Quién vuelve?", opciones,
            format_func=lambda n: (
                f"Paciente {n} · " + ", ".join(
                    f"{e.edad}a {e.sexo}, ingresó {fecha_larga(e.fecha_ingreso)}"
                    for e in previos if e.idpaciente == n
                )
            ),
        )
        anterior = next(e for e in previos if e.idpaciente == idpac)
        st.info(f"Se registra como un episodio nuevo del paciente {idpac}, que ya tuvo {sum(1 for e in previos if e.idpaciente == idpac)} internación(es).")
        defaults = {"edad": anterior.edad, "sexo": anterior.sexo,
                    "cronica": bool(anterior.enfermedad_cronica_grave)}
    else:
        if reing and not previos:
            st.warning("No hay pacientes previos para vincular. Se admite como ingreso nuevo.")
            reing = False
        idpac = st.number_input(
            "N.º de paciente", 1, 999_999_999,
            value=(max((e.idpaciente for e in previos), default=0) + 1),
        )
        defaults = {"edad": 65, "sexo": "M", "cronica": False}

    c1, c2 = st.columns(2)
    with c1:
        edad = st.number_input("Edad (años)", 16, 150, value=int(d.get("edad") or defaults["edad"]))
        sexo_v = d.get("sexo") or defaults["sexo"]
        sexo = st.selectbox("Sexo", list(SEXOS), index=list(SEXOS).index(sexo_v),
                            format_func=lambda s: SEXOS[s])
        f_ing = st.date_input(
            "Fecha de ingreso",
            datetime.strptime(d["fecha_ingreso"], "%Y-%m-%d").date() if d.get("fecha_ingreso") else date.today(),
        )
    with c2:
        h_ing = st.time_input(
            "Hora de ingreso",
            datetime.strptime(d["hora_ingreso"], "%H:%M:%S").time() if d.get("hora_ingreso")
            else datetime.now().time().replace(second=0, microsecond=0),
        )
        mot_v = int(d.get("moting") or 1)
        moting = st.selectbox("Motivo de ingreso", list(MOTIVOS_INGRESO),
                              index=list(MOTIVOS_INGRESO).index(mot_v) if mot_v in MOTIVOS_INGRESO else 0,
                              format_func=lambda k: MOTIVOS_INGRESO[k])
        pro_v = int(d.get("procedencia") or 1)
        proced = st.selectbox("Procedencia", list(PROCEDENCIAS),
                              index=list(PROCEDENCIAS).index(pro_v) if pro_v in PROCEDENCIAS else 0,
                              format_func=lambda k: PROCEDENCIAS[k])

    cronica = st.checkbox(
        "Insuficiencia orgánica severa preexistente o inmunocompromiso",
        value=bool(d.get("enfermedad_cronica_grave", defaults["cronica"])),
        help="Suma puntos de salud crónica al APACHE II. Solo si ya estaba antes del ingreso.",
    )

    if st.button("Admitir paciente", type="primary", use_container_width=True):
        try:
            ep = modelos.Episodio(
                idcentro=semilla.IDCENTRO, idpaciente=int(idpac), reingreso=int(reing),
                fecha_ingreso=f_ing.isoformat(), hora_ingreso=h_ing.strftime("%H:%M:%S"),
                tipo="A", edad=int(edad), sexo=sexo, moting=int(moting),
                procedencia=int(proced), enfermedad_cronica_grave=cronica,
            )
            db.insert_episodio(con, ep)
            st.session_state["paciente_id"] = ep.id
            for k in ("ingreso_dictado", "texto_admision", "firma_admision"):
                st.session_state.pop(k, None)
            st.rerun()
        except modelos.ErrorValidacion as e:
            st.error(str(e))


@st.dialog("Corregir datos de ingreso")
def dialogo_editar_paciente(episodio):
    """Los datos administrativos se corrigen; los clínicos no.

    Un error de tipeo al admitir (la edad, el sexo) es eso: un error de carga.
    Pero la edad suma puntos al APACHE II, así que cada corrección queda
    asentada con el valor anterior, el nuevo y quién la hizo."""
    st.caption("Sólo los datos del ingreso. Lo que pasó durante la internación se corrige en la historia clínica.")

    c1, c2 = st.columns(2)
    with c1:
        idpac = st.number_input("N.º de paciente", 1, 999_999_999, value=episodio.idpaciente)
        edad = st.number_input("Edad (años)", 16, 150, value=episodio.edad)
        sexo = st.selectbox(
            "Sexo", list(SEXOS), index=list(SEXOS).index(episodio.sexo),
            format_func=lambda s: SEXOS[s],
        )
        f_ing = st.date_input("Fecha de ingreso", datetime.strptime(episodio.fecha_ingreso, "%Y-%m-%d").date())
    with c2:
        h_ing = st.time_input("Hora de ingreso", datetime.strptime(episodio.hora_ingreso, "%H:%M:%S").time())
        moting = st.selectbox("Motivo de ingreso", list(MOTIVOS_INGRESO),
                              index=list(MOTIVOS_INGRESO).index(episodio.moting),
                              format_func=lambda k: MOTIVOS_INGRESO[k])
        proced = st.selectbox("Procedencia", list(PROCEDENCIAS),
                              index=list(PROCEDENCIAS).index(episodio.procedencia),
                              format_func=lambda k: PROCEDENCIAS[k])
        reing = st.checkbox("Es un reingreso (< 48 h)", value=bool(episodio.reingreso))

    cronica = st.checkbox(
        "Insuficiencia orgánica severa preexistente o inmunocompromiso",
        value=bool(episodio.enfermedad_cronica_grave),
        help="Cambiar esto modifica el APACHE II del paciente.",
    )

    if edad != episodio.edad or cronica != bool(episodio.enfermedad_cronica_grave):
        st.warning("Estos cambios modifican el puntaje de gravedad APACHE II. Queda registrado quién los hizo.")

    if not firma_valida(firma_actual()):
        aviso_firma()
    if st.button("Guardar correcciones", type="primary", use_container_width=True,
                 disabled=not firma_valida(firma_actual())):
        try:
            cambios = db.actualizar_episodio(
                con, episodio,
                {
                    "idpaciente": int(idpac), "edad": int(edad), "sexo": sexo,
                    "fecha_ingreso": f_ing.isoformat(), "hora_ingreso": h_ing.strftime("%H:%M:%S"),
                    "moting": int(moting), "procedencia": int(proced),
                    "reingreso": int(reing), "enfermedad_cronica_grave": cronica,
                },
                autor=st.session_state.get("autor", "Dr./Dra."),
            )
            st.session_state["cambios_recientes"] = cambios
            st.rerun()
        except (modelos.ErrorValidacion, ValueError) as e:
            st.error(str(e))

    historial = db.historial_episodio(con, episodio.id)
    if historial:
        with st.expander(f"Correcciones anteriores ({len(historial)})"):
            for h in historial:
                st.caption(
                    f"**{h['campo']}**: {h['valor_anterior']} → {h['valor_nuevo']} · "
                    f"{h['autor']} · {h['timestamp'][:16].replace('T', ' ')}"
                )


@st.dialog("Cargar un dato a mano")
def dialogo_evento_manual(episodio, eventos_previos):
    """Escape hatch imprescindible: si el sistema no entendió algo, o el médico
    prefiere tipearlo, tiene que poder cargarlo igual. Un registro clínico que
    sólo acepta lo que el modelo entendió es inservible."""
    st.caption("Lo que cargues acá queda registrado como certeza total y firmado por vos.")

    tipo = st.selectbox(
        "Qué pasó",
        ["dispositivo_inicio", "dispositivo_fin", "evento_adverso", "fisiologico_24h", "tiss_diario", "egreso"],
        format_func=lambda t: {
            "dispositivo_inicio": "Se colocó un dispositivo",
            "dispositivo_fin": "Se retiró un dispositivo",
            "evento_adverso": "Ocurrió un evento adverso o una infección",
            "fisiologico_24h": "Valores de signos vitales o laboratorio",
            "tiss_diario": "Carga de enfermería del día (TISS-28)",
            "egreso": "El paciente egresa de la unidad",
        }[t],
    )

    c1, c2 = st.columns(2)
    with c1:
        f = st.date_input("Fecha", date.today())
    with c2:
        h = st.time_input("Hora", datetime.now().time().replace(second=0, microsecond=0))

    payload: dict = {}
    abiertas = gemma.instancias_abiertas(eventos_previos)

    if tipo == "dispositivo_inicio":
        disp = st.selectbox("Dispositivo", list(DISPOSITIVOS), format_func=lambda d: DISPOSITIVOS[d])
        usadas = [i for i in abiertas if i.startswith(f"{disp}-")]
        payload = {"dispositivo": disp, "instancia_id": f"{disp}-{len(usadas) + 1}"}
        sitio = st.text_input("Sitio de colocación (opcional)", placeholder="subclavia derecha")
        if sitio.strip():
            payload["sitio"] = sitio.strip()
        if usadas:
            st.caption(f"Ya hay {len(usadas)} colocado(s). Este se registra como {payload['instancia_id']}.")

    elif tipo == "dispositivo_fin":
        if not abiertas:
            st.warning("No hay ningún dispositivo colocado para retirar.")
            return
        inst = st.selectbox("Cuál se retira", list(abiertas), format_func=lambda i: abiertas[i])
        payload = {"dispositivo": inst.split("-")[0], "instancia_id": inst}
        motivo = st.selectbox("Motivo", ["programado", "accidental", "recambio", "egreso"])
        payload["motivo"] = motivo

    elif tipo == "evento_adverso":
        cod = st.selectbox("Cuál", list(ADVERSOS), format_func=lambda c: ADVERSOS[c])
        payload = {"codigo": cod, "adjudicado_por": "medico"}
        st.caption("La declaración es tuya. El sistema después revisa si la historia la documenta.")

    elif tipo == "fisiologico_24h":
        st.caption("Completá sólo lo que tengas. Lo que quede vacío no se registra.")
        med: dict = {}
        campos = [
            ("tas_mmhg", "Presión sistólica"), ("tad_mmhg", "Presión diastólica"),
            ("temperatura_c", "Temperatura °C"), ("fc_lpm", "Frecuencia cardíaca"),
            ("fr_rpm", "Frecuencia respiratoria"), ("glasgow", "Glasgow"),
            ("pao2_mmhg", "PaO₂"), ("paco2_mmhg", "PaCO₂"), ("fio2", "FiO₂"),
            ("ph_arterial", "pH"), ("sodio_meq_l", "Sodio"), ("potasio_meq_l", "Potasio"),
            ("creatinina_mg_dl", "Creatinina"), ("hematocrito_pct", "Hematocrito"),
            ("leucocitos_mil_mm3", "Leucocitos (miles)"),
        ]
        cols = st.columns(3)
        for i, (clave, rotulo) in enumerate(campos):
            v = cols[i % 3].text_input(rotulo, key=f"man_{clave}")
            if v.strip():
                try:
                    med[clave] = float(v.replace(",", "."))
                except ValueError:
                    st.error(f"{rotulo}: '{v}' no es un número")
        payload = {"mediciones": med}
        if st.checkbox("Falla renal aguda"):
            payload["falla_renal_aguda"] = True
        st.caption("La presión arterial media la calcula el sistema a partir de la sistólica y la diastólica.")

    elif tipo == "tiss_diario":
        pts = st.number_input("Puntaje TISS-28 del día", min_value=0, max_value=77, value=20)
        payload = {"fecha": f.isoformat(), "puntaje_manual": int(pts)}

    elif tipo == "egreso":
        # Un episodio tiene un solo egreso. Permitir un segundo dejaba la
        # proyeccion inconsistente, asi que se corta acá y se manda a corregir
        # el que ya existe.
        anulados_prev = {e.corrige_a_evento_id for e in eventos_previos if e.corrige_a_evento_id}
        ya_egresado = [
            e for e in eventos_previos
            if e.tipo_evento == "egreso" and e.id not in anulados_prev
        ]
        if ya_egresado:
            previo = ya_egresado[0]
            st.error(
                f"Este paciente ya tiene un egreso cargado "
                f"({fecha_larga(previo.timestamp_clinico)} {previo.timestamp_clinico[11:16]})."
            )
            st.caption(
                "No se puede agregar un segundo egreso. Si el que está cargado es incorrecto, "
                "corregilo desde la historia clínica con el botón «Corregir»."
            )
            return
        res = st.selectbox("Destino al egreso", [int(k) for k in RESULTADOS],
                           format_func=lambda k: RESULTADOS[str(k)])
        payload = {"resultado": int(res)}

    nota = st.text_input("Nota (queda como respaldo del registro)",
                         placeholder="por qué se carga esto a mano")

    if not firma_valida(firma_actual()):
        aviso_firma()
    if st.button("Guardar registro", type="primary", use_container_width=True,
                 disabled=not firma_valida(firma_actual())):
        try:
            ev = modelos.nuevo_evento(
                episodio_id=episodio.id,
                timestamp_clinico=f"{f.isoformat()}T{h.strftime('%H:%M:%S')}",
                autor=st.session_state.get("autor", "Dr./Dra."),
                tipo_evento=tipo,
                payload_json=payload,
                fuente="manual",
                confianza=1.0,
                texto_crudo=nota.strip() or "Cargado a mano por el profesional",
            )
            db.insert_evento(con, ev)
            st.rerun()
        except modelos.ErrorValidacion as e:
            st.error(str(e))


@st.dialog("Corregir registro")
def dialogo_editar_evento(ev, eventos_previos):
    """Corregir no edita nada: inserta un registro nuevo que anula al viejo.
    Los dos quedan a la vista, que es toda la gracia del libro de movimientos."""
    st.caption(f"Registrado por {ev.autor} · certeza {int(ev.confianza * 100)}%")
    st.markdown(f"**Dice ahora:** {describir(ev)}")
    st.markdown(f"> *“{ev.texto_crudo}”*")
    st.divider()

    f_actual = datetime.strptime(ev.timestamp_clinico[:10], "%Y-%m-%d").date()
    h_actual = datetime.strptime(ev.timestamp_clinico[11:16], "%H:%M").time()
    c1, c2 = st.columns(2)
    with c1:
        f = st.date_input("Fecha correcta", f_actual)
    with c2:
        h = st.time_input("Hora correcta", h_actual)

    payload = dict(ev.payload_json)

    if ev.tipo_evento in ("dispositivo_inicio", "dispositivo_fin"):
        disp = st.selectbox(
            "Dispositivo", list(DISPOSITIVOS),
            index=list(DISPOSITIVOS).index(payload.get("dispositivo", "VI")),
            format_func=lambda d: DISPOSITIVOS[d],
        )
        if disp != payload.get("dispositivo"):
            payload["instancia_id"] = f"{disp}-1"
        payload["dispositivo"] = disp
        if ev.tipo_evento == "dispositivo_inicio":
            sitio = st.text_input("Sitio", value=payload.get("sitio", ""))
            payload["sitio"] = sitio.strip()
            if not payload["sitio"]:
                payload.pop("sitio")

    elif ev.tipo_evento == "evento_adverso":
        cod = st.selectbox(
            "Cuál fue", list(ADVERSOS),
            index=list(ADVERSOS).index(payload.get("codigo", "ESCARAS")),
            format_func=lambda c: ADVERSOS[c],
        )
        payload = {"codigo": cod, "adjudicado_por": "medico"}

    elif ev.tipo_evento == "fisiologico_24h":
        st.caption("Vaciá un campo para quitarlo del registro.")
        med = dict(payload.get("mediciones") or {})
        nuevas: dict = {}
        cols = st.columns(3)
        for i, (clave, valor) in enumerate(med.items()):
            rotulo = MEDICIONES.get(clave, clave).split(",")[0]
            v = cols[i % 3].text_input(rotulo, value=str(valor), key=f"ed_{ev.id}_{clave}")
            if v.strip():
                try:
                    nuevas[clave] = float(v.replace(",", "."))
                except ValueError:
                    st.error(f"{rotulo}: '{v}' no es un número")
        payload["mediciones"] = nuevas

    elif ev.tipo_evento == "tiss_diario":
        pts = st.number_input("Puntaje TISS-28", 0, 77, value=int(payload.get("puntaje_manual") or 20))
        payload = {"fecha": f.isoformat(), "puntaje_manual": int(pts)}

    elif ev.tipo_evento == "egreso":
        actual = int(payload.get("resultado", 1))
        res = st.selectbox(
            "Destino al egreso", [int(k) for k in RESULTADOS],
            index=[int(k) for k in RESULTADOS].index(actual),
            format_func=lambda k: RESULTADOS[str(k)],
        )
        payload = {"resultado": int(res)}

    motivo = st.text_input("Por qué se corrige", placeholder="el sistema entendió mal la hora")

    if not firma_valida(firma_actual()):
        aviso_firma()
    if st.button("Guardar corrección", type="primary", use_container_width=True,
                 disabled=not firma_valida(firma_actual())):
        try:
            db.insert_evento(con, modelos.nuevo_evento(
                episodio_id=ev.episodio_id,
                timestamp_clinico=f"{f.isoformat()}T{h.strftime('%H:%M:%S')}",
                autor=st.session_state.get("autor", "Dr./Dra."),
                tipo_evento=ev.tipo_evento,
                payload_json=payload,
                fuente="manual",
                confianza=1.0,
                texto_crudo=motivo.strip() or ev.texto_crudo,
                corrige_a_evento_id=ev.id,
            ))
            st.rerun()
        except modelos.ErrorValidacion as e:
            st.error(str(e))


@st.dialog("Cargar el valor a mano")
def dialogo_ajustar_campo(episodio, nombre_campo, campo_proy, definicion):
    """Pisar un campo del reporte con un valor cargado a mano.

    El valor derivado no se pierde: queda guardado al lado, y el campo se
    muestra marcado como cargado a mano. Que un número del reporte no venga
    de los registros es información, no un detalle a esconder."""
    st.markdown(f"**{definicion.get('descripcion', nombre_campo)}**")
    st.caption(f"Campo {nombre_campo} del registro SATI-Q")

    derivado = campo_proy.valor_derivado if campo_proy.ajuste_manual else campo_proy.valor
    st.info(f"Calculado a partir de los registros: **{derivado if derivado is not None else 'sin dato'}**")

    val = definicion.get("validacion", {})
    etiquetas = val.get("etiquetas")

    if etiquetas:
        opciones = list(etiquetas)
        nuevo = st.selectbox("Valor correcto", opciones, format_func=lambda k: f"{etiquetas[k]} ({k})")
    elif val.get("enum"):
        nuevo = st.selectbox("Valor correcto", val["enum"])
    elif val.get("tipo") in ("entero", "decimal"):
        nuevo = st.text_input("Valor correcto", value=str(campo_proy.valor or ""))
    else:
        nuevo = st.text_input("Valor correcto", value=str(campo_proy.valor or ""))

    motivo = st.text_input(
        "Por qué lo cargás a mano",
        placeholder="el dato figura en la historia en papel",
    )
    st.caption("Queda registrado que este valor no salió de los registros del sistema, con tu firma.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Guardar valor", type="primary", use_container_width=True,
                     disabled=not firma_valida(firma_actual())):
            db.insert_ajuste(
                con, episodio.id, nombre_campo, nuevo, motivo.strip() or "sin motivo declarado",
                st.session_state.get("autor", "Dr./Dra."),
            )
            st.rerun()
    with c2:
        if campo_proy.ajuste_manual and st.button("Volver a lo calculado", use_container_width=True):
            db.insert_ajuste(
                con, episodio.id, nombre_campo, None, "se vuelve al valor derivado",
                st.session_state.get("autor", "Dr./Dra."), anulado=True,
            )
            st.rerun()


def confirmar_evento(ev, autor: str) -> None:
    """Confirmar no edita el registro dudoso: inserta uno nuevo, idéntico pero
    con certeza total, que lo anula. Así queda asentado que un humano lo miró
    y quién fue, sin romper la inmutabilidad."""
    db.insert_evento(
        con,
        modelos.nuevo_evento(
            episodio_id=ev.episodio_id,
            timestamp_clinico=ev.timestamp_clinico,
            autor=autor,
            tipo_evento=ev.tipo_evento,
            payload_json=ev.payload_json,
            fuente="manual",
            confianza=1.0,
            texto_crudo=ev.texto_crudo,
            corrige_a_evento_id=ev.id,
        ),
    )


# ---------------------------------------------------------------------------
# Panel lateral: selección de paciente
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🩺 MedTranscriptor")
    st.caption("Registro de internación · Terapia Intensiva")

    # La firma vive acá, no en una vista: todo lo que se guarda queda firmado,
    # así que tiene que poder ponerse desde cualquier pantalla.
    firma_sidebar = st.text_input(
        "Firma del profesional", key="autor", placeholder="Apellido",
        help="Todo registro queda firmado. Sin nombre no se puede guardar nada.",
    )
    if not firma_valida(firma_sidebar):
        st.caption("⚠️ Poné tu nombre para poder registrar.")

    st.divider()

    lista = pacientes()

    if not lista:
        st.info("No hay pacientes internados.")
    else:
        etiquetas = {}
        for ep in lista:
            cerrado = esta_cerrado(db.get_eventos(con, ep.id))
            etiquetas[ep.id] = f"{'○' if cerrado else '●'}  Paciente {ep.idpaciente} · {ep.edad}a {ep.sexo}"

        actual = st.session_state.get("paciente_id")
        if actual not in etiquetas:
            actual = lista[0].id

        elegido = st.radio(
            "Pacientes",
            options=list(etiquetas),
            format_func=lambda i: etiquetas[i],
            index=list(etiquetas).index(actual),
            label_visibility="collapsed",
        )
        st.session_state["paciente_id"] = elegido
        st.caption("● internado    ○ egresado")

    if st.button("＋ Admitir paciente", use_container_width=True):
        dialogo_nuevo_paciente()

    st.divider()
    modelo_whisper = st.selectbox(
        "Transcripción de voz",
        ["small", "base", "large-v3-turbo"],
        key="modelo_whisper",
        help="Corre en esta máquina, sin enviar el audio a ningún servidor.",
    )
    if modelo_whisper == "large-v3-turbo":
        st.caption(
            "⏳ La primera vez descarga 1,6 GB y parece trabado. Además, sin placa de video "
            "tarda bastante más que `small` en transcribir."
        )
    elif modelo_whisper == "base":
        st.caption("El más rápido. Puede errarle a términos médicos poco frecuentes.")

    with st.expander("Herramientas de demostración"):
        st.caption("Solo para la presentación. No forma parte del uso clínico.")
        if st.button("Cargar caso de ejemplo", use_container_width=True):
            cache = Path("demo/eventos_cache.json")
            if not cache.exists():
                st.error("Falta demo/eventos_cache.json. Correr `python demo.py`.")
            else:
                ep = semilla.crear_episodio()
                existentes = {e.idpaciente for e in pacientes()}
                while ep.idpaciente in existentes:
                    ep.idpaciente += 1
                db.insert_episodio(con, ep)

                # Los eventos del caché traen sus ids grabados. Al cargar el
                # caso una segunda vez son eventos NUEVOS de un episodio NUEVO,
                # así que necesitan ids propios o chocan con los ya insertados.
                # Se remapean en dos pasadas para que corrige_a_evento_id siga
                # apuntando al evento correcto dentro del episodio nuevo.
                brutos = json.loads(cache.read_text(encoding="utf-8"))
                nuevos_ids = {d["id"]: str(uuid.uuid4()) for d in brutos}
                for d in brutos:
                    d["episodio_id"] = ep.id
                    d["id"] = nuevos_ids[d["id"]]
                    if d.get("corrige_a_evento_id"):
                        d["corrige_a_evento_id"] = nuevos_ids.get(d["corrige_a_evento_id"])
                    db.insert_evento(con, modelos.Evento.from_dict(d))

                st.session_state["paciente_id"] = ep.id
                st.rerun()

# ---------------------------------------------------------------------------
# Sin paciente no hay nada que hacer
# ---------------------------------------------------------------------------
lista = pacientes()
if not lista:
    st.markdown("## Bienvenido a MedTranscriptor")
    st.markdown(
        "Todavía no hay ningún paciente internado. Admitir uno desde el panel de la "
        "izquierda, o cargar el caso de ejemplo si estás conociendo el sistema."
    )
    st.stop()

paciente = next((e for e in lista if e.id == st.session_state.get("paciente_id")), lista[0])
st.session_state["paciente_id"] = paciente.id

eventos = db.get_eventos(con, paciente.id)
ajustes = db.ajustes_vigentes(con, paciente.id)
proyeccion = proyectar_fila(paciente, eventos, ajustes)
valores = fila_a_valores(proyeccion)
cerrado = esta_cerrado(eventos)
por_revisar = [e for e in eventos if e.confianza < modelos.UMBRAL_REVISION]

# ---------------------------------------------------------------------------
# Ficha del paciente
# ---------------------------------------------------------------------------
estadia = valores.get("ESTADIA")
# ESTADIA sale de la proyección y sólo existe con el egreso cargado. No se
# calcula acá una estadía provisoria: sería el único número de la pantalla
# que no vino del motor.
texto_estadia = f"{estadia} días" if estadia else "En curso"
sexo_largo = SEXOS[paciente.sexo]

st.markdown(
    html(f"""
<div class="ficha">
  <div class="ficha-id">Paciente {paciente.idpaciente}</div>
  <div class="ficha-dato"><span class="ficha-rotulo">Edad · Sexo</span>
    <span class="ficha-valor">{paciente.edad} años · {sexo_largo}</span></div>
  <div class="ficha-dato"><span class="ficha-rotulo">Ingreso</span>
    <span class="ficha-valor">{fecha_larga(paciente.fecha_ingreso)} · {paciente.hora_ingreso[:5]}</span></div>
  <div class="ficha-dato"><span class="ficha-rotulo">Motivo</span>
    <span class="ficha-valor">{MOTIVOS_INGRESO.get(paciente.moting, '')}</span></div>
  <div class="ficha-dato"><span class="ficha-rotulo">Estadía</span>
    <span class="ficha-valor">{texto_estadia}</span></div>
  <div class="ficha-dato"><span class="ficha-rotulo">Registros</span>
    <span class="ficha-valor">{len(eventos)}</span></div>
  <div class="ficha-estado {'estado-cerrado' if cerrado else 'estado-curso'}">
    {'Egresado' if cerrado else 'Internado'}</div>
</div>
"""),
    unsafe_allow_html=True,
)

izq_f, der_f = st.columns([5, 1])
with der_f:
    if st.button("Corregir ingreso", use_container_width=True, help="Editar edad, sexo, motivo o procedencia"):
        dialogo_editar_paciente(paciente)

if st.session_state.pop("cambios_recientes", None):
    st.toast("Datos de ingreso corregidos. Los puntajes se recalcularon.", icon="✅")


def pendientes(paciente, eventos, proyeccion, valores) -> list[tuple[str, str]]:
    """Qué le falta al registro, HOY, no al egreso.

    Avisar al egreso que faltan las variables del APACHE de las primeras 24 h
    es inútil: esa ventana ya pasó y el dato no se puede recuperar. El valor
    del sistema está en decirlo el primer día, cuando todavía se puede medir."""
    faltas: list[tuple[str, str]] = []

    detalle = proyeccion["SCORE"].detalle or {}
    faltantes = detalle.get("variables_faltantes") or []
    if faltantes:
        legibles = [MEDICIONES.get(v, v).split(",")[0] for v in faltantes]
        faltas.append((
            "APACHE II incompleto",
            f"Sin registro en las primeras 24 h de: {', '.join(legibles)}. "
            f"APACHE II las asume normales, así que el puntaje de gravedad "
            f"({valores.get('SCORE')}) queda por debajo del real.",
        ))

    abiertas = gemma.instancias_abiertas(eventos)
    if abiertas and cerrado:
        faltas.append((
            "Dispositivos sin retirar",
            "Quedaron colocados al egreso: " + ", ".join(abiertas.values())
            + ". Se cuentan hasta la fecha de egreso; si se retiraron antes, hay que registrarlo.",
        ))

    if not any(e.tipo_evento == "tiss_diario" for e in eventos):
        faltas.append((
            "Sin carga de enfermería",
            "No hay ningún TISS-28 registrado. SATI-Q pide el mínimo, el máximo y el promedio de la internación.",
        ))

    return faltas


faltas = pendientes(paciente, eventos, proyeccion, valores)

if por_revisar or faltas:
    with st.container(border=True):
        if por_revisar:
            st.markdown(
                f"**{len(por_revisar)} registros esperan tu confirmación.** El sistema no estaba seguro "
                "de haberlos entendido y no los da por buenos solo. Están marcados en la historia clínica."
            )
        for titulo_f, detalle_f in faltas:
            st.markdown(f"**{titulo_f}.** {detalle_f}")

vista = st.segmented_control(
    "Vista",
    ["Evolución diaria", "Historia clínica", "Registro SATI-Q"],
    default="Evolución diaria",
    label_visibility="collapsed",
)

# ===========================================================================
# EVOLUCIÓN DIARIA — dictar, revisar y confirmar, todo en un solo recorrido
# ===========================================================================
if vista == "Evolución diaria":
    izq, der = st.columns([3, 2], gap="large")

    with izq:
        st.markdown("#### Dictá la evolución")
        st.caption(
            "Contá lo que pasó con fecha y hora, como se lo pasarías a un colega. "
            "Los días de dispositivo y los puntajes los calcula el sistema."
        )

        audio = st.audio_input("Grabación", label_visibility="collapsed")

        if audio is not None:
            crudo = audio.getvalue()
            # Un audio se transcribe una sola vez: Streamlit re-ejecuta el
            # script en cada interacción y sin este guard volvería a pasar
            # Whisper sobre la misma grabación en cada click.
            firma = hashlib.md5(crudo).hexdigest()
            if st.session_state.get("firma_audio") != firma:
                carpeta = Path("demo/audio")
                carpeta.mkdir(parents=True, exist_ok=True)
                ruta = carpeta / f"p{paciente.idpaciente}_{datetime.now():%Y%m%d_%H%M%S}.wav"
                ruta.write_bytes(crudo)
                st.session_state["firma_audio"] = firma
                st.session_state["ruta_audio"] = str(ruta)

                with st.spinner("Transcribiendo…"):
                    t0 = time.time()
                    texto, err = transcribir(ruta, modelo_whisper)
                    st.session_state["seg_transcripcion"] = round(time.time() - t0, 1)

                if texto:
                    st.session_state["nota"] = texto
                    st.session_state["transcripcion_cruda"] = texto
                    st.session_state["error_voz"] = None
                else:
                    st.session_state["error_voz"] = err or "No se detectó voz en la grabación."
                st.rerun()

        if st.session_state.get("error_voz"):
            st.error(st.session_state["error_voz"])
            st.caption("Podés escribir la evolución a mano acá abajo.")
        elif st.session_state.get("transcripcion_cruda"):
            st.caption(f"Transcripto en {st.session_state.get('seg_transcripcion', '?')} s · revisalo antes de registrar")

        nota = st.text_area(
            "Evolución",
            height=200,
            key="nota",
            label_visibility="collapsed",
            placeholder=(
                "Dictá arriba o escribí acá.\n\n"
                "Ejemplo: Hoy a las diez de la mañana le coloqué una vía central subclavia "
                "derecha. Temperatura 38.5, frecuencia cardíaca 110, Glasgow 14. Ayer le "
                "sacamos la sonda vesical."
            ),
        )

        c1, c2 = st.columns([2, 1])
        with c1:
            fecha_ref = st.date_input("Fecha de la evolución", key="fecha_evolucion")
        with c2:
            st.time_input("Hora", key="hora_evolucion")
        autor = firma_actual()

        sin_firma = not firma_valida(autor)
        if sin_firma:
            aviso_firma()
        if st.button("Registrar evolución", type="primary", use_container_width=True,
                     disabled=not nota.strip() or sin_firma):
            with st.spinner("Leyendo la evolución y extrayendo los datos…"):
                st.session_state["extraido"] = gemma.traducir_nota(
                    episodio_id=paciente.id,
                    nota=nota,
                    fecha_referencia=fecha_ref.isoformat(),
                    autor=autor,
                    fuente="audio_gemma" if st.session_state.get("ruta_audio") else "texto_gemma",
                    eventos_previos=eventos,
                )
            st.rerun()

    with der:
        extraido = st.session_state.get("extraido")

        if extraido is None:
            st.markdown("#### Qué se va a registrar")
            st.caption(
                "Acá vas a ver lo que el sistema entendió, con la frase exacta que lo justifica, "
                "antes de que se guarde nada."
            )
            st.stop()  # nada más que mostrar en esta vista hasta que se procese

        # Datos de cabecera que la nota mencionó: quién firma, cuándo ingresó.
        # No se aplican solos — cambian el episodio, y eso se pregunta.
        meta = extraido.metadatos or {}
        if meta.get("autor") and meta["autor"] not in st.session_state.get("autor", ""):
            firma = f"Dr./Dra. {meta['autor']}"
            if st.button(f"Firmar como {firma}", use_container_width=True):
                st.session_state["autor"] = firma
                st.rerun()

        if meta.get("fecha_ingreso"):
            f_dicha = meta["fecha_ingreso"]
            h_dicha = meta.get("hora_ingreso") or "00:00:00"
            if f_dicha != paciente.fecha_ingreso or h_dicha != paciente.hora_ingreso:
                with st.container(border=True):
                    st.markdown(
                        f"**Dijiste que ingresó el {fecha_larga(f_dicha)} a las {h_dicha[:5]}.**  \n"
                        f"En el sistema figura {fecha_larga(paciente.fecha_ingreso)} a las {paciente.hora_ingreso[:5]}."
                    )
                    if st.button("Corregir el ingreso con lo que dicté", use_container_width=True):
                        try:
                            db.actualizar_episodio(
                                con, paciente,
                                {"fecha_ingreso": f_dicha, "hora_ingreso": h_dicha},
                                autor=st.session_state.get("autor", "Dr./Dra."),
                            )
                            st.rerun()
                        except (modelos.ErrorValidacion, ValueError) as e:
                            st.error(str(e))

        st.markdown(f"#### Qué se entendió · {len(extraido.eventos)} registros")

        for ev in extraido.eventos:
            dudoso = ev.confianza < modelos.UMBRAL_REVISION
            inst = instancia(ev)
            st.markdown(
                html(f"""<div class="evento {'evento-revisar' if dudoso else ''}">
                  <div class="evento-linea">
                    <span class="evento-titulo">{ICONOS.get(ev.tipo_evento, '•')} {describir(ev)}
                      {f'<span style="opacity:.55"> · {inst}</span>' if inst else ''}</span>
                    <span class="evento-hora">{fecha_larga(ev.timestamp_clinico)} {ev.timestamp_clinico[11:16]}</span>
                  </div>
                  <div class="cita">“{ev.texto_crudo}”</div>
                  {'<div style="margin-top:.4rem"><span class="marca marca-revisar">Confirmá esto</span></div>' if dudoso else ''}
                </div>"""),
                unsafe_allow_html=True,
            )

        if extraido.correcciones:
            for c in extraido.correcciones:
                st.info(f"↩️ {c}")

        if extraido.no_entendido:
            with st.container(border=True):
                st.markdown("**Esto no lo entendí**")
                st.caption("Prefiero decírtelo antes que inventar un dato.")
                for frag in extraido.no_entendido:
                    st.markdown(f"· *“{frag}”*")

        if extraido.rechazados:
            with st.container(border=True):
                st.markdown("**Descartado por inconsistente**")
                for r in extraido.rechazados:
                    st.caption(r["motivo"])

        a, b = st.columns(2)
        with a:
            if st.button("Confirmar y guardar", type="primary", use_container_width=True,
                         disabled=not firma_valida(firma_actual())):
                for ev in extraido.eventos:
                    db.insert_evento(con, ev)
                for clave in ("extraido", "nota", "transcripcion_cruda", "firma_audio", "ruta_audio"):
                    st.session_state.pop(clave, None)
                st.rerun()
        with b:
            if st.button("Descartar", use_container_width=True):
                st.session_state.pop("extraido", None)
                st.rerun()

# ===========================================================================
# HISTORIA CLÍNICA — la internación como línea de tiempo
# ===========================================================================
elif vista == "Historia clínica":
    if not eventos:
        st.info("Todavía no hay registros para este paciente. Empezá dictando una evolución.")
        st.stop()

    cab, acc = st.columns([3, 1])
    with cab:
        st.markdown("#### Historia de la internación")
        st.caption(
            "Cada línea es un hecho con su hora y su firma. Los registros no se editan ni se borran: "
            "una corrección entra como un asiento nuevo que anula al anterior, y ambos quedan a la vista."
        )
    with acc:
        if st.button("＋ Cargar dato a mano", use_container_width=True):
            dialogo_evento_manual(paciente, eventos)
        if por_revisar and st.button(
            f"Confirmar los {len(por_revisar)} pendientes", use_container_width=True,
            disabled=not firma_valida(firma_actual()),
        ):
            for ev in por_revisar:
                confirmar_evento(ev, st.session_state.get("autor", "Dr./Dra."))
            st.rerun()

    anulados = {e.corrige_a_evento_id for e in eventos if e.corrige_a_evento_id}
    dia = None
    for ev in sorted(eventos, key=lambda x: x.timestamp_clinico):
        f = ev.timestamp_clinico[:10]
        if f != dia:
            dia = f
            st.markdown(f'<div class="dia-sep">{fecha_larga(f)}</div>', unsafe_allow_html=True)

        anulado = ev.id in anulados
        dudoso = ev.confianza < modelos.UMBRAL_REVISION and not anulado
        inst = instancia(ev)
        marcas = ""
        if anulado:
            marcas = '<span class="marca marca-corrige">Anulado por una corrección</span>'
        elif ev.corrige_a_evento_id:
            marcas = '<span class="marca marca-corrige">↩️ Corrige un registro anterior</span>'
        elif dudoso:
            marcas = '<span class="marca marca-revisar">Sin confirmar</span>'

        st.markdown(
            html(f"""<div class="evento {'evento-revisar' if dudoso else ''}"
                     style="{'opacity:.45;text-decoration:line-through' if anulado else ''}">
              <div class="evento-linea">
                <span class="evento-titulo">{ICONOS.get(ev.tipo_evento, '•')} {describir(ev)}
                  {f'<span style="opacity:.55"> · {inst}</span>' if inst else ''}</span>
                <span class="evento-hora">{ev.timestamp_clinico[11:16]} · {ev.autor}</span>
              </div>
              <div class="cita">“{ev.texto_crudo}”</div>
              {f'<div style="margin-top:.4rem">{marcas}</div>' if marcas else ''}
            </div>"""),
            unsafe_allow_html=True,
        )
        if not anulado:
            b1, b2, _ = st.columns([1, 1, 4])
            if dudoso and b1.button("Está bien", key=f"conf_{ev.id}",
                                    disabled=not firma_valida(firma_actual()),
                                    help="Confirmar que se entendió correctamente"):
                confirmar_evento(ev, st.session_state.get("autor", "Dr./Dra."))
                st.rerun()
            if b2.button("Corregir", key=f"edit_{ev.id}"):
                dialogo_editar_evento(ev, eventos)

# ===========================================================================
# REGISTRO SATI-Q — la fila oficial, explicada y trazable
# ===========================================================================
else:
    hallazgos = validador.validar_fila(valores, advertencias_fila(proyeccion))
    errores = validador.errores(hallazgos)
    advertencias = validador.advertencias(hallazgos)
    por_id = {e.id: e for e in eventos}

    st.markdown("#### Registro para SATI-Q")
    st.caption(
        "Los 49 campos del registro nacional, calculados a partir de la historia. "
        "Ninguno se cargó a mano: tocá cualquier valor en azul para ver de qué frase salió."
    )

    if errores:
        st.error(
            f"**Faltan datos para poder reportar este paciente** ({len(errores)}). "
            "Lo más común es que la internación siga abierta: el registro se cierra al egreso."
        )
        with st.expander(f"Ver los {len(errores)} campos incompletos"):
            for h in errores:
                st.markdown(f"· **{h.campo}** — {h.mensaje}")
    else:
        st.success("El registro está completo y cumple las 49 reglas de validación de SATI-Q.")

    if advertencias:
        with st.expander(f"{len(advertencias)} observaciones a tener en cuenta"):
            for h in advertencias:
                st.markdown(f"· **{h.campo}** — {h.mensaje}")

    SECCIONES = {
        "Identificación": ["IDCENTRO", "IDPACIENTE", "REINGRESO", "TIPO", "EDAD", "SEXO", "MOTING", "PROCEDENCIA"],
        "Internación": ["FECHING", "HORAING", "FECEGR", "HORAEGR", "ESTADIA", "RESULTADO"],
        "Gravedad al ingreso": ["SCORE", "PROBABMORT"],
        "Soporte vital y dispositivos": ["VI", "DIASVI", "VNI", "DIASVNI", "CAFO", "DIASCAFO", "CVC", "DIASCVC",
                                          "SE", "DIASSE", "SV", "DIASSV", "SNG", "DIASSNG"],
        "Infecciones y eventos adversos": ["NEUMONIA", "NEUMONIANUM", "INFCATETER", "INFCATETERNUM",
                                            "INFURINARIA", "INFURINARIANUM", "INFHERIDAS", "INFHERIDASNUM",
                                            "AUTOEXTUBACION", "AUTOEXTUBACIONNUM", "ESCARAS", "ESCARASNUM",
                                            "DESLIZSNG", "DESLIZSNGNUM", "DESLIZCAMA", "DESLIZCAMANUM"],
        "Carga de enfermería (TISS-28)": ["TISSMIN", "TISSMAX", "TISSPROMEDIO"],
    }

    # -----------------------------------------------------------------------
    # Vista de tabla: una fila por paciente, una columna por campo. Es la
    # forma real del CSV que se le manda a SATI-Q, asi que verlo asi hace
    # obvio que esto es un reporte y no un formulario.
    # -----------------------------------------------------------------------
    import pandas as pd

    st.markdown("##### Todos los pacientes")
    st.caption(
        "Cada fila es un paciente, cada columna un campo del reporte. Tocá una celda para "
        "cargar el valor a mano. Debajo está el detalle del paciente seleccionado."
    )

    filas_tabla, indice = [], []
    for ep in lista:
        evs = db.get_eventos(con, ep.id)
        pr = proyectar_fila(ep, evs, db.ajustes_vigentes(con, ep.id))
        vals = fila_a_valores(pr)
        filas_tabla.append({n: vals.get(n) for n in exportador_columnas})
        indice.append(f"Paciente {ep.idpaciente}" + (" (reingreso)" if ep.reingreso else ""))

    tabla = pd.DataFrame(filas_tabla, index=indice)

    # Los campos derivados se pueden pisar; los del episodio se corrigen en
    # "Corregir ingreso", que es donde tienen sentido y deja auditoría propia.
    bloqueadas = [n for n in exportador_columnas
                  if CAMPOS_SATIQ.get(n, {}).get("origen") != "derivado"]

    editada = st.data_editor(
        tabla,
        use_container_width=True,
        height=min(120 + 36 * len(lista), 380),
        disabled=bloqueadas,
        key=f"tabla_satiq_{len(lista)}",
    )

    # Cada celda cambiada se guarda como ajuste manual, con su firma.
    cambios_tabla = []
    for i, ep in enumerate(lista):
        for nombre_col in exportador_columnas:
            if nombre_col in bloqueadas:
                continue
            antes, ahora = tabla.iloc[i][nombre_col], editada.iloc[i][nombre_col]
            if pd.isna(antes) and pd.isna(ahora):
                continue
            if antes != ahora:
                cambios_tabla.append((ep, nombre_col, ahora))

    if cambios_tabla:
        st.warning(f"{len(cambios_tabla)} valor(es) modificados en la tabla, sin guardar.")
        for ep, nombre_col, valor in cambios_tabla:
            st.caption(f"· Paciente {ep.idpaciente} · {nombre_col} → {valor}")
        motivo_tabla = st.text_input("Por qué se cargan a mano", key="motivo_tabla",
                                     placeholder="figura en la historia en papel")
        if st.button("Guardar valores cargados a mano", type="primary",
                     disabled=not firma_valida(firma_actual())):
            for ep, nombre_col, valor in cambios_tabla:
                db.insert_ajuste(
                    con, ep.id, nombre_col, valor,
                    motivo_tabla.strip() or "cargado desde la tabla",
                    st.session_state.get("autor", "Dr./Dra."),
                )
            st.rerun()

    st.divider()
    st.markdown(f"##### Detalle del paciente {paciente.idpaciente}")
    st.caption("De dónde sale cada número, campo por campo.")

    for titulo, nombres in SECCIONES.items():
        st.markdown(f"###### {titulo}")
        columnas = st.columns(2)
        for i, nombre in enumerate(nombres):
            definicion = CAMPOS_SATIQ.get(nombre, {})
            campo = proyeccion[nombre]
            derivado = definicion.get("origen") == "derivado"

            etiquetas = definicion.get("validacion", {}).get("etiquetas")
            if campo.valor is None:
                mostrado = '<span class="valor-vacio">sin dato</span>'
            elif etiquetas:
                mostrado = etiquetas.get(str(campo.valor), str(campo.valor))
            else:
                mostrado = str(campo.valor)

            a_mano = campo.ajuste_manual is not None
            clase = "valor-manual" if a_mano else ("valor-derivado" if derivado and campo.valor is not None else "")

            with columnas[i % 2]:
                st.markdown(
                    html(f"""<div class="campo">
                      <span class="campo-nombre">{definicion.get('descripcion', nombre)}
                        <span class="campo-codigo">{nombre}</span></span>
                      <span class="campo-valor {clase}">{mostrado}
                        {'<span class="marca marca-manual">a mano</span>' if a_mano else ''}</span>
                    </div>"""),
                    unsafe_allow_html=True,
                )

                if a_mano:
                    aj = campo.ajuste_manual
                    st.caption(
                        f"Cargado por {aj['autor']} · {aj['motivo']} · el sistema calculaba "
                        f"{campo.valor_derivado if campo.valor_derivado is not None else 'sin dato'}"
                    )

                # Un solo control por campo. Adentro va todo: de dónde sale el
                # número y la opción de cargarlo a mano. Un botón por campo
                # llenaba la pantalla de 49 botones idénticos.
                rotulo = f"Detalle · {len(campo.evento_ids)} registros" if campo.evento_ids else "Detalle"
                with st.popover(rotulo, use_container_width=True):
                    st.markdown(f"**{definicion.get('descripcion', nombre)}**")

                    if a_mano:
                        st.warning(
                            f"Este valor lo cargó **{campo.ajuste_manual['autor']}** a mano. "
                            f"No sale de los registros del sistema, que calculan "
                            f"**{campo.valor_derivado if campo.valor_derivado is not None else 'sin dato'}**."
                        )
                        st.caption(f"Motivo: {campo.ajuste_manual['motivo']}")

                    if campo.detalle and "componentes" in campo.detalle:
                        st.caption("Cómo se compone el puntaje:")
                        for c in campo.detalle["componentes"]:
                            if c["puntos"] == 0 and c.get("faltante"):
                                continue
                            nota_c = f" · {c['nota']}" if c.get("nota") else ""
                            st.markdown(f"· {c['etiqueta']} **{c['valor'] if c['valor'] is not None else ''}** → **{c['puntos']} pts**{nota_c}")
                        faltan = campo.detalle.get("variables_faltantes") or []
                        if faltan:
                            st.caption(f"Sin medición en las primeras 24 h: {', '.join(faltan)}. Se asumen normales.")
                        st.divider()

                    # Cada registro que originó el número es accionable desde
                    # acá: si el número está mal, casi siempre el problema está
                    # en uno de estos registros, no en el campo.
                    for eid in campo.evento_ids:
                        ev = por_id.get(eid)
                        if not ev:
                            continue
                        with st.container(border=True):
                            st.markdown(
                                f"**{fecha_larga(ev.timestamp_clinico)} {ev.timestamp_clinico[11:16]}** · {ev.autor}  \n"
                                f"{ICONOS.get(ev.tipo_evento, '•')} {describir(ev)}  \n"
                                f"> *“{ev.texto_crudo}”*"
                            )
                            if ev.confianza < modelos.UMBRAL_REVISION:
                                st.caption(f"⚠️ Sin confirmar · certeza {int(ev.confianza * 100)}%")
                            if st.button(
                                "Corregir este registro", key=f"pop_{nombre}_{ev.id}",
                                use_container_width=True,
                                disabled=not firma_valida(firma_actual()),
                            ):
                                dialogo_editar_evento(ev, eventos)

                    if not campo.evento_ids and not campo.detalle:
                        st.caption("Dato administrativo del ingreso, no derivado de la historia.")

                    st.divider()
                    if st.button("Cargar este valor a mano", key=f"aj_{nombre}", use_container_width=True):
                        dialogo_ajustar_campo(paciente, nombre, campo, definicion)
        st.write("")

    st.divider()
    st.markdown("#### Cierre del episodio")
    a, b, c = st.columns(3)

    with a:
        st.markdown("**Reporte oficial**")
        if validador.es_exportable(hallazgos):
            st.download_button(
                "Descargar CSV para SATI-Q",
                data=exportar_csv([valores]),
                file_name=f"satiq_paciente_{paciente.idpaciente}.csv",
                mime="text/csv",
                use_container_width=True,
                type="primary",
            )
        else:
            st.button("Descargar CSV para SATI-Q", disabled=True, use_container_width=True)
            st.caption("Disponible cuando el registro esté completo.")

    with b:
        st.markdown("**Resumen para la familia**")
        st.caption("En lenguaje llano, sin siglas ni puntajes.")
        if st.button("Redactar resumen", use_container_width=True, disabled=not cerrado):
            with st.spinner("Redactando…"):
                st.session_state["resumen"] = gemma.explicar_egreso(
                    f"Paciente de {paciente.edad} años. Estuvo {valores.get('ESTADIA')} días en terapia intensiva.\n"
                    f"Días con respirador: {valores.get('DIASVI')}. Días con vía central: {valores.get('DIASCVC')}.\n"
                    f"Tuvo neumonía asociada al respirador: {'sí' if valores.get('NEUMONIA') else 'no'}.\n"
                    f"Tuvo lesiones por presión: {'sí' if valores.get('ESCARAS') else 'no'}.\n"
                    f"Al egreso: {RESULTADOS.get(str(valores.get('RESULTADO')), '')}."
                )
        if not cerrado:
            st.caption("Disponible al egreso.")

    with c:
        st.markdown("**Auditoría de infecciones**")
        declaradas = sorted({
            e.payload_json.get("codigo") for e in eventos
            if e.tipo_evento == "evento_adverso"
            and e.payload_json.get("codigo") in ("NEUMONIA", "INFCATETER", "INFURINARIA", "INFHERIDAS")
        })
        if not declaradas:
            st.caption("No hay infecciones declaradas en este paciente.")
        else:
            cual = st.selectbox("Infección", declaradas, format_func=lambda c: ADVERSOS.get(c, c), label_visibility="collapsed")
            if st.button("Revisar documentación", use_container_width=True):
                registro = "\n".join(
                    f"[{e.timestamp_clinico[:16]}] {e.texto_crudo}"
                    for e in sorted(eventos, key=lambda x: x.timestamp_clinico)
                )
                with st.spinner("Revisando la historia contra los criterios VIHDA…"):
                    st.session_state["vihda"] = (cual, gemma.verificar_vihda(cual, registro))

    if st.session_state.get("resumen"):
        st.markdown("##### Resumen para el paciente y su familia")
        with st.container(border=True):
            st.markdown(st.session_state["resumen"])

    if st.session_state.get("vihda"):
        codigo, res = st.session_state["vihda"]
        st.markdown(f"##### Documentación de {ADVERSOS.get(codigo, codigo).lower()}")
        st.caption(
            "El diagnóstico es tuyo y no está en discusión. Esto sólo revisa si la historia "
            "documenta lo que el programa de vigilancia exige para poder reportar el caso."
        )
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Documentado**")
            for x in res["cumplidos"]:
                st.success(f"{x.get('id')}\n\n*“{x.get('evidencia','')}”*")
        with c2:
            st.markdown("**Falta documentar**")
            for x in res["faltantes"]:
                st.warning(f"{x.get('id')}\n\n{x.get('que_falta','')}")
            if res["faltantes"]:
                st.error("Con la documentación incompleta, SATI-Q puede rechazar el caso.")
