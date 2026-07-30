"""
MedTranscriptor - Interfaz Streamlit
====================================
Sistema de transcripción y estructuración de evoluciones en Terapia Intensiva (UCI)
para el reporte automático del registro nacional SATI-Q.

REGLAS DE DISEÑO E INTERFAZ:
1. No se realiza NINGÚN cálculo en la interfaz (sumas, días, scores). Todos los números
   provienen exclusivamente de proyectar_fila().
2. Los archivos del motor (modelos.py, db.py, proyector.py, etc.) NO SE TOCAN.
3. Se mantiene trazabilidad completa de cada dato clínico hasta su cita textual original.
4. Idioma y estilo: Español rioplatense.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
import streamlit as st

import db
import semilla
import validador
import gemma
import modelos
from proyector import proyectar_fila, fila_a_valores, advertencias_fila, episodio_abierto
from exportador import exportar_csv

# Configuración de página Streamlit
st.set_page_config(
    page_title="MedTranscriptor - UCI SATI-Q",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Estilos CSS personalizados para estética médica moderna y profesional
CUSTOM_CSS = """
<style>
    /* Estilo general */
    .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    
    /* Header principal */
    .main-header {
        background: linear-gradient(135deg, #1e3a8a 0%, #0284c7 100%);
        color: white;
        padding: 1.5rem 2rem;
        border-radius: 12px;
        margin-bottom: 1.5rem;
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }
    .main-header h1 {
        color: white !important;
        margin: 0;
        font-size: 2.2rem;
        font-weight: 700;
    }
    .main-header p {
        color: #e0f2fe;
        margin: 0.3rem 0 0 0;
        font-size: 1.05rem;
    }

    /* Tarjetas de eventos.
       Los colores son semitransparentes a proposito: asi funcionan igual en
       tema claro y oscuro, heredando el fondo de Streamlit. Con colores fijos
       (#ffffff) las tarjetas quedaban blancas encandilando sobre tema oscuro. */
    .evento-card {
        background-color: rgba(128, 128, 128, 0.07);
        border-left: 4px solid #0ea5e9;
        border-radius: 8px;
        padding: 0.9rem 1.1rem;
        margin-bottom: 0.8rem;
    }
    .evento-card-revision {
        border-left: 4px solid #eab308 !important;
        background-color: rgba(234, 179, 8, 0.10) !important;
    }
    .evento-header {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 1rem;
        margin-bottom: 0.35rem;
        flex-wrap: wrap;
    }
    .evento-tipo {
        font-weight: 700;
        font-size: 1.05rem;
    }
    .evento-meta {
        opacity: 0.65;
        font-size: 0.85rem;
    }
    .cita-textual {
        border-left: 3px solid rgba(128, 128, 128, 0.4);
        padding: 0.4rem 0.7rem;
        font-style: italic;
        opacity: 0.85;
        font-size: 0.93rem;
        margin-top: 0.5rem;
    }

    /* Badges */
    .badge-revision {
        background-color: rgba(234, 179, 8, 0.22);
        color: #eab308;
        padding: 0.2rem 0.6rem;
        border-radius: 9999px;
        font-size: 0.78rem;
        font-weight: 600;
        display: inline-block;
    }
    .badge-confianza {
        background-color: rgba(14, 165, 233, 0.18);
        color: #0ea5e9;
        padding: 0.18rem 0.5rem;
        border-radius: 4px;
        font-size: 0.78rem;
        font-weight: 600;
    }

    /* Filas de la fila SATI-Q */
    .campo-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 0.75rem;
        padding: 0.5rem 0 0.2rem 0;
    }
    .campo-nombre {
        font-weight: 600;
        font-size: 0.92rem;
    }
    .campo-codigo {
        opacity: 0.45;
        font-size: 0.75rem;
        margin-left: 0.35rem;
        font-family: monospace;
    }
    .campo-valor {
        font-weight: 700;
        font-size: 1.15rem;
        white-space: nowrap;
    }
    .campo-derivado {
        color: #0ea5e9;
    }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Base de Datos y Estado Global
# ---------------------------------------------------------------------------
@st.cache_resource
def obtener_conexion() -> db.sqlite3.Connection:
    return db.conectar("medtranscriptor.db")

con = obtener_conexion()

def inicializar_episodio() -> modelos.Episodio:
    episodios = db.listar_episodios(con)
    if not episodios:
        ep = semilla.crear_episodio()
        db.insert_episodio(con, ep)
        return ep
    return modelos.Episodio.from_dict(episodios[0])

episodio_actual = inicializar_episodio()

# Guardar audio en disco
def guardar_archivo_audio(audio_bytes: bytes) -> Path:
    carpeta = Path("demo/audio")
    carpeta.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = carpeta / f"nota_{timestamp}.wav"
    ruta.write_bytes(audio_bytes)
    return ruta

# Transcripción con faster-whisper.
#
# Ojo con dos cosas que en Streamlit importan mucho:
#
# 1. EL MODELO SE CARGA UNA SOLA VEZ. Streamlit re-ejecuta el script entero en
#    cada interacción; sin @st.cache_resource se recargaría el modelo (10-20s)
#    en cada click. Se cachea acá.
# 2. NO SE RE-TRANSCRIBE EL MISMO AUDIO. Idem: sin un guard, cada rerun vuelve
#    a transcribir la grabación que ya estaba procesada. Se usa un hash del
#    audio como clave.
#
# Esta maquina no tiene GPU utilizable (Intel HD 620), asi que va en CPU con
# int8. 'small' rinde ~1.4x tiempo real; 'large-v3-turbo' es mas preciso pero
# bastante mas lento. Se puede elegir desde el sidebar.
@st.cache_resource(show_spinner=False)
def cargar_modelo_whisper(nombre: str):
    import voz
    return voz.cargar_modelo(nombre)


def transcribir_con_whisper(ruta_audio: Path, modelo: str) -> tuple[str | None, str | None]:
    """Devuelve (texto, error). Si falla, el motivo se muestra en pantalla:
    tragarse la excepcion deja al usuario sin saber por que no anda."""
    try:
        cargar_modelo_whisper(modelo)  # calienta el cache
        import voz
        texto, _ = voz.transcribir(ruta_audio, modelo)
        return (texto or None), None
    except ImportError:
        return None, "faster-whisper no está instalado. Corré: pip install faster-whisper"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

# Mapeo de campos a sus definiciones en el schema
CAMPOS_DICT = {c["nombre"]: c for c in validador.CAMPOS}

# -----------------------------------------------------------------------
# Traduccion de eventos a castellano.
#
# Un medico no tiene por que leer {"dispositivo": "CVC", "instancia_id":
# "CVC-1", "sitio": "subclavia derecha"}. Estas funciones convierten el
# payload en una frase. Los nombres salen de los catalogos del schema, no
# estan escritos a mano dos veces.
# -----------------------------------------------------------------------
_SCHEMA_EVENTOS = json.loads(Path("schema/eventos.json").read_text(encoding="utf-8"))
DISPOSITIVOS_NOMBRE = _SCHEMA_EVENTOS["catalogos"]["dispositivos"]
ADVERSOS_NOMBRE = {k: v["descripcion"] for k, v in _SCHEMA_EVENTOS["catalogos"]["eventos_adversos"].items()}
RESULTADO_NOMBRE = _SCHEMA_EVENTOS["catalogos"]["resultado_egreso"]
MEDICIONES_NOMBRE = _SCHEMA_EVENTOS["payloads"]["fisiologico_24h"]["campos"]["mediciones"]["claves"]

ICONO_EVENTO = {
    "dispositivo_inicio": "🔌",
    "dispositivo_fin": "🔓",
    "evento_adverso": "⚠️",
    "fisiologico_24h": "🌡️",
    "tiss_diario": "📊",
    "egreso": "🚪",
}


_PLACEHOLDERS = {
    "no mencionado", "no especificado", "no especifica", "desconocido",
    "n/a", "na", "none", "null", "no aplica", "sin especificar", "no indicado",
}


def _texto_util(valor) -> str:
    """Descarta los rellenos tipo 'no mencionado' que a veces devuelve el
    modelo. gemma.py ya los filtra al normalizar, pero los eventos que quedaron
    guardados antes de ese arreglo los siguen teniendo."""
    if not isinstance(valor, str) or valor.strip().lower() in _PLACEHOLDERS:
        return ""
    return valor.strip()


def describir_evento(ev) -> str:
    """Una frase en castellano que describe lo que pasó."""
    p = ev.payload_json
    tipo = ev.tipo_evento

    if tipo == "dispositivo_inicio":
        nombre = DISPOSITIVOS_NOMBRE.get(p.get("dispositivo"), p.get("dispositivo", ""))
        sitio_txt = _texto_util(p.get("sitio"))
        sitio = f" en {sitio_txt}" if sitio_txt else ""
        return f"Se colocó **{nombre.lower()}**{sitio}"

    if tipo == "dispositivo_fin":
        nombre = DISPOSITIVOS_NOMBRE.get(p.get("dispositivo"), p.get("dispositivo", ""))
        motivo = p.get("motivo")
        extra = ""
        if motivo == "accidental":
            extra = " (retiro accidental)"
        elif motivo == "recambio":
            extra = " (por recambio)"
        return f"Se retiró **{nombre.lower()}**{extra}"

    if tipo == "evento_adverso":
        nombre = ADVERSOS_NOMBRE.get(p.get("codigo"), p.get("codigo", ""))
        return f"**{nombre}** — declarado por el médico"

    if tipo == "fisiologico_24h":
        med = p.get("mediciones") or {}
        if not med:
            return "Registro de valores fisiológicos"
        partes = [f"{MEDICIONES_NOMBRE.get(k, k).split(',')[0]}: **{v}**" for k, v in med.items()]
        texto = " · ".join(partes)
        if p.get("falla_renal_aguda"):
            texto += " · con falla renal aguda"
        return texto

    if tipo == "tiss_diario":
        pts = p.get("puntaje_manual")
        return f"Carga de trabajo del día (TISS-28): **{pts} puntos**" if pts is not None else "Carga de trabajo del día (TISS-28)"

    if tipo == "egreso":
        destino = RESULTADO_NOMBRE.get(str(p.get("resultado")), p.get("resultado", ""))
        return f"**Egreso de la unidad** → {destino}"

    return tipo


def instancia_legible(ev) -> str:
    """CVC-2 se muestra como '2º catéter venoso central'."""
    inst = (ev.payload_json or {}).get("instancia_id") or ""
    if "-" not in inst:
        return ""
    codigo, _, numero = inst.partition("-")
    if not numero.isdigit() or numero == "1":
        return ""
    return f"{numero}º {DISPOSITIVOS_NOMBRE.get(codigo, codigo).lower()}"

# Agrupación humana de las 49 columnas de SATI-Q
SECCIONES_SATIQ = {
    "👤 Identificación del Paciente y Centro": [
        "IDCENTRO", "IDPACIENTE", "REINGRESO", "FECHING", "HORAING", "TIPO", "EDAD", "SEXO", "MOTING", "PROCEDENCIA"
    ],
    "🏥 Estadía en UCI": [
        "FECEGR", "HORAEGR", "ESTADIA"
    ],
    "🩺 Gravedad y Pronóstico (APACHE II)": [
        "SCORE", "PROBABMORT"
    ],
    "🔌 Dispositivos y Soporte Vital": [
        "VI", "DIASVI", "VNI", "DIASVNI", "CAFO", "DIASCAFO", "CVC", "DIASCVC", "SE", "DIASSE", "SV", "DIASSV", "SNG", "DIASSNG"
    ],
    "⚠️ Eventos Adversos e Infecciones": [
        "NEUMONIA", "NEUMONIANUM", "AUTOEXTUBACION", "AUTOEXTUBACIONNUM", "INFCATETER", "INFCATETERNUM", "INFURINARIA", "INFURINARIANUM", "ESCARAS", "ESCARASNUM", "INFHERIDAS", "INFHERIDASNUM", "DESLIZSNG", "DESLIZSNGNUM", "DESLIZCAMA", "DESLIZCAMANUM"
    ],
    "📊 Puntaje TISS-28": [
        "TISSMIN", "TISSMAX", "TISSPROMEDIO"
    ],
    "🚪 Egreso y Condición Final": [
        "RESULTADO"
    ]
}

# ---------------------------------------------------------------------------
# Sidebar - Información del Paciente y Accesos Rápidos
# ---------------------------------------------------------------------------
with st.sidebar:
    # Sin imágenes remotas: si en la demo no hay internet, la página cuelga
    # esperando el request.
    st.title("🩺 MedTranscriptor")
    st.caption("Sistema Inteligente de Registro UCI")

    st.divider()
    st.subheader("🎙️ Transcripción")
    modelo_whisper = st.selectbox(
        "Modelo de Whisper",
        options=["small", "base", "large-v3-turbo"],
        index=0,
        help=(
            "Corre local, sin internet. Esta máquina no tiene GPU: "
            "'base' es el más rápido, 'small' equilibrado, "
            "'large-v3-turbo' el más preciso pero bastante más lento "
            "(y baja 1.6 GB la primera vez)."
        ),
    )

    st.divider()
    st.subheader("📌 Paciente Sintético Activo")
    st.markdown(f"**ID Paciente:** `{episodio_actual.idpaciente}`")
    st.markdown(f"**Edad / Sexo:** `{episodio_actual.edad} años` | `{episodio_actual.sexo}`")
    st.markdown(f"**Ingreso:** `{episodio_actual.fecha_ingreso}` ({episodio_actual.hora_ingreso})")
    st.markdown(f"**Motivo:** Patología Médica")
    
    st.divider()
    st.subheader("⚡ Cargar Demo en Vivo")
    st.caption("Cargá los 24 eventos precalculados desde el caché para probar el sistema sin esperar llamadas a la API.")
    
    if st.button("🚀 Cargar 24 Eventos (Caché)", use_container_width=True):
        cache_path = Path("demo/eventos_cache.json")
        if cache_path.exists():
            datos = json.loads(cache_path.read_text(encoding="utf-8"))
            # Limpiar eventos previos si se recarga demo
            con.execute("DELETE FROM evento WHERE episodio_id = ?", (episodio_actual.id,))
            con.commit()
            for d in datos:
                d["episodio_id"] = episodio_actual.id
                e = modelos.Evento.from_dict(d)
                db.insert_evento(con, e)
            st.success("¡24 eventos de la demo cargados exitosamente!")
            st.rerun()
        else:
            st.error("No se encontró `demo/eventos_cache.json`. Corré `python demo.py` primero.")

    st.divider()
    st.info("💡 **Gemma** clasifica los eventos. **Python** realiza todos los cálculos deterministas de días, estadía y APACHE II.")

# ---------------------------------------------------------------------------
# Header Principal
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div class="main-header">
        <h1>🩺 MedTranscriptor UI</h1>
        <p>Transcripción por voz de evoluciones de Terapia Intensiva con generación automática de la fila SATI-Q y trazabilidad punto a punto.</p>
    </div>
    """,
    unsafe_allow_html=True
)

# Control de Pestañas
tab_grabar, tab_anotado, tab_satiq = st.tabs([
    "🎙️ 1. GRABAR EVOLUCIÓN",
    "📋 2. QUÉ SE ANOTÓ",
    "📊 3. FILA SATI-Q, EN CRISTIANO"
])

# ---------------------------------------------------------------------------
# PESTAÑA 1: GRABAR
# ---------------------------------------------------------------------------
with tab_grabar:
    st.header("Dictado y Transcripción de Evoluciones")
    st.write("Grabá con el micrófono o pegá el texto del parte diario. Gemma extraerá los eventos clínicos automáticamente.")
    
    col_input, col_meta = st.columns([2, 1])
    
    with col_meta:
        st.subheader("Datos de la Nota")
        fecha_ref = st.date_input("Fecha de referencia", datetime.now(), help="Fecha clínica sobre la que se resuelven expresiones como 'hoy' o 'ayer'")
        autor_nota = st.text_input("Médico / Autor", "Dra. Pereyra")
        
        st.divider()
        st.markdown("##### 📝 Plan B: Notas de Ejemplo Demo")
        st.caption("Si no disponés de micrófono o querés probar un caso específico:")
        
        opcion_nota = st.selectbox(
            "Seleccionar nota de la demo:",
            options=range(len(semilla.NOTAS)),
            format_func=lambda i: f"Nota {i+1} ({semilla.NOTAS[i]['fecha_referencia']}) - {semilla.NOTAS[i]['autor']}"
        )
        
        if st.button("Usar esta nota de ejemplo", use_container_width=True):
            nota_sel = semilla.NOTAS[opcion_nota]
            # Igual que con la transcripción: hay que escribir la key del
            # text_area, no una variable aparte, o el editor queda vacío.
            st.session_state["editor_nota"] = nota_sel["texto"]
            st.session_state["transcripcion_original"] = None
            st.session_state["transcripcion_error"] = None
            st.rerun()

    with col_input:
        st.subheader("Grabación de Voz")
        audio_dictado = st.audio_input("Dictá la evolución médica")

        if audio_dictado is not None:
            audio_bytes = audio_dictado.getvalue()
            # Un mismo audio se transcribe UNA sola vez. Streamlit re-ejecuta
            # el script en cada interaccion, y sin este guard cada click
            # volveria a pasar Whisper sobre la misma grabacion.
            firma = hashlib.md5(audio_bytes).hexdigest()

            if st.session_state.get("audio_firma") != firma:
                ruta_audio = guardar_archivo_audio(audio_bytes)
                st.session_state["audio_firma"] = firma
                st.session_state["audio_ruta"] = str(ruta_audio)

                with st.spinner("Transcribiendo…"):
                    inicio = time.time()
                    texto_whisper, error = transcribir_con_whisper(ruta_audio, modelo_whisper)
                    st.session_state["transcripcion_segundos"] = round(time.time() - inicio, 1)

                if texto_whisper:
                    # El texto va DIRECTO al editor. Con key= el text_area
                    # ignora el value=, asi que hay que escribir la session_state
                    # de la key antes del rerun o el cuadro queda vacio.
                    st.session_state["editor_nota"] = texto_whisper
                    st.session_state["transcripcion_original"] = texto_whisper
                    st.session_state["transcripcion_error"] = None
                else:
                    st.session_state["transcripcion_error"] = error or "No se detectó voz en el audio."
                st.rerun()

        if st.session_state.get("transcripcion_error"):
            st.error(f"No se pudo transcribir. {st.session_state['transcripcion_error']}")
            st.caption("La app sigue funcionando: escribí o pegá la nota a mano abajo.")
        elif st.session_state.get("transcripcion_original"):
            st.success(
                f"Listo en {st.session_state.get('transcripcion_segundos', '?')}s. "
                "Revisá el texto abajo antes de procesar."
            )

        texto_nota = st.text_area(
            "📝 Nota de evolución — corregí acá lo que haga falta",
            height=190,
            key="editor_nota",
            help=(
                "Whisper puede errarle a las fechas y a los términos médicos. "
                "Una fecha mal transcripta arruina el cálculo de días de dispositivo, "
                "así que conviene leerlo antes de procesar."
            ),
            placeholder="Dictá arriba, o escribí acá directamente.\n\nEj: Hoy a las diez de la mañana le coloqué una vía central subclavia derecha. Temperatura 38.5, frecuencia cardíaca 110. Ayer le sacamos la sonda vesical.",
        )

        if st.session_state.get("audio_ruta"):
            with st.expander("Detalles de la grabación", expanded=False):
                st.caption(f"Audio guardado en `{st.session_state['audio_ruta']}`")
                if st.session_state.get("transcripcion_original"):
                    st.caption("Transcripción original, antes de tus correcciones:")
                    st.code(st.session_state["transcripcion_original"], language=None)
        
        if st.button("🤖 Procesar con Gemma", type="primary", use_container_width=True, disabled=not texto_nota.strip()):
            eventos_existentes = db.get_eventos(con, episodio_actual.id)
            fecha_str = fecha_ref.strftime("%Y-%m-%d")

            with st.spinner("Gemma está analizando la nota y extrayendo los eventos clínicos (~13 segundos)..."):
                # eventos_previos (en vez de 'abiertas') le da al modelo el
                # estado del paciente Y habilita la resolución de correcciones:
                # Gemma marca que un evento corrige algo, Python resuelve a cuál.
                resultado = gemma.traducir_nota(
                    episodio_id=episodio_actual.id,
                    nota=texto_nota,
                    fecha_referencia=fecha_str,
                    autor=autor_nota,
                    fuente="audio_gemma" if st.session_state.get("audio_ruta") else "texto_gemma",
                    eventos_previos=eventos_existentes,
                )
                st.session_state["resultado_gemma"] = resultado
                st.session_state["nota_procesada_reciente"] = texto_nota
                st.success(f"¡Procesamiento completado en {resultado.segundos} segundos!")
                st.info("👉 Pasá a la pestaña **'2. QUÉ SE ANOTÓ'** para revisar y confirmar los eventos hallados.")

# ---------------------------------------------------------------------------
# PESTAÑA 2: QUÉ SE ANOTÓ
# ---------------------------------------------------------------------------
with tab_anotado:
    st.header("Confirmación de Eventos Comprendidos")
    st.write("Revisá los eventos estructurados por Gemma antes de guardarlos definitivamente en el libro de movimientos.")
    
    resultado = st.session_state.get("resultado_gemma")
    
    if resultado is not None:
        st.subheader("Eventos de la Nota Recién Procesada")
        st.caption(f"Tiempo de ejecución: {resultado.segundos}s | Eventos validados: {len(resultado.eventos)}")
        
        # Bloque de revisión de confianza < 0.75
        eventos_dudosos = [e for e in resultado.eventos if e.confianza < modelos.UMBRAL_REVISION]
        if eventos_dudosos:
            st.warning(
                f"⚠️ **{len(eventos_dudosos)} evento(s) requieren revisión humana** "
                f"(confianza por debajo de {modelos.UMBRAL_REVISION}). El sistema duda en voz alta y no autocompleta a ciegas."
            )
            
        # 1. Timeline de eventos validados
        for i, ev in enumerate(resultado.eventos, start=1):
            es_dudoso = ev.confianza < modelos.UMBRAL_REVISION
            card_class = "evento-card-revision" if es_dudoso else "evento-card"

            fecha, _, hora = ev.timestamp_clinico.partition("T")
            fecha_legible = "/".join(reversed(fecha.split("-")))
            instancia = instancia_legible(ev)
            etiqueta_inst = f" · {instancia}" if instancia else ""

            with st.container():
                st.markdown(
                    f"""
                    <div class="{card_class}">
                        <div class="evento-header">
                            <span class="evento-tipo">{ICONO_EVENTO.get(ev.tipo_evento, '•')} {describir_evento(ev)}{etiqueta_inst}</span>
                            <span class="evento-meta">{fecha_legible} · {hora[:5]} · {ev.autor}</span>
                        </div>
                        <div class="cita-textual">"{ev.texto_crudo}"</div>
                        <div style="margin-top: 0.5rem;">
                            <span class="badge-confianza">Certeza {int(ev.confianza * 100)}%</span>
                            {'<span class="badge-revision">Revisá esto</span>' if es_dudoso else ''}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
                # El JSON sigue disponible, pero escondido: le sirve al que
                # audita el sistema, no al medico que confirma la evolucion.
                with st.expander("Ver dato técnico", expanded=False):
                    st.code(json.dumps(ev.payload_json, ensure_ascii=False, indent=2), language="json")
            
        # 2. Bloque "Esto no lo entendí"
        if resultado.no_entendido:
            st.markdown("### ❓ Esto no lo entendí")
            st.info("Fragmentos de la nota que no pudieron ser mapeados a ningún evento clínico formal:")
            for frag in resultado.no_entendido:
                st.markdown(f"- *\"{frag}\"*")
                
        # 3. Bloque de Rechazados
        if resultado.rechazados:
            st.markdown("### ❌ Eventos Rechazados por Validación")
            for rech in resultado.rechazados:
                st.error(f"**Motivo:** {rech['motivo']}")

        col_save, _ = st.columns([1, 2])
        with col_save:
            if st.button("💾 Confirmar y Guardar Eventos en la BD", type="primary", use_container_width=True):
                for e in resultado.eventos:
                    db.insert_evento(con, e)
                st.session_state["resultado_gemma"] = None
                st.success("¡Eventos insertados correctamente en el libro de movimientos!")
                st.rerun()

    st.divider()
    st.subheader("📖 Libro de Movimientos Completo en la BD")
    eventos_bd = db.get_eventos(con, episodio_actual.id)
    
    if not eventos_bd:
        st.info("Todavía no hay eventos guardados para este episodio. Dictá una nota en la pestaña 1 o cargá la demo desde el sidebar.")
    else:
        st.write(f"Total de eventos registrados en la BD: **{len(eventos_bd)}**")
        
        # Filtro o visualización de eventos de la BD
        with st.expander("Ver lista de eventos almacenados", expanded=True):
            dia_actual = None
            for e in sorted(eventos_bd, key=lambda x: x.timestamp_clinico):
                fecha, _, hora = e.timestamp_clinico.partition("T")
                # Separador por dia: la internacion se lee como una linea de
                # tiempo, no como una lista plana de 24 filas.
                if fecha != dia_actual:
                    dia_actual = fecha
                    st.markdown(f"**📅 {'/'.join(reversed(fecha.split('-')))}**")

                marca = "🟡" if e.confianza < modelos.UMBRAL_REVISION else "🟢"
                instancia = instancia_legible(e)
                etiqueta_inst = f" · {instancia}" if instancia else ""
                anulado = " · ↩️ *corrige un registro anterior*" if e.corrige_a_evento_id else ""
                st.markdown(
                    f"{marca} `{hora[:5]}` {ICONO_EVENTO.get(e.tipo_evento, '•')} "
                    f"{describir_evento(e)}{etiqueta_inst}{anulado}  \n"
                    f"&nbsp;&nbsp;&nbsp;&nbsp;<small>{e.autor} — *\"{e.texto_crudo}\"*</small>",
                    unsafe_allow_html=True,
                )

# ---------------------------------------------------------------------------
# PESTAÑA 3: FILA SATI-Q, EN CRISTIANO
# ---------------------------------------------------------------------------
with tab_satiq:
    st.header("Fila Oficial SATI-Q (49 Campos Proyectados)")
    st.write("Generación en tiempo real a partir del libro de movimientos. **Ningún número se calcula en la interfaz.**")
    
    eventos_actuales = db.get_eventos(con, episodio_actual.id)
    proyeccion = proyectar_fila(episodio_actual, eventos_actuales)
    valores_fila = fila_a_valores(proyeccion)
    hallazgos = validador.validar_fila(valores_fila, advertencias_fila(proyeccion))
    
    eventos_por_id = {e.id: e for e in eventos_actuales}
    
    # -----------------------------------------------------------------------
    # Recorrido de los 49 campos agrupados en 7 secciones
    # -----------------------------------------------------------------------
    for titulo_seccion, nombres_campos in SECCIONES_SATIQ.items():
        with st.container():
            st.markdown(f"### {titulo_seccion}")
            
            cols = st.columns(2)
            for idx, nombre in enumerate(nombres_campos):
                def_campo = CAMPOS_DICT.get(nombre, {})
                campo_proy = proyeccion[nombre]
                valor_raw = campo_proy.valor
                
                # Mapeo a etiqueta comprensible si existe enum con etiquetas
                val_display = valor_raw
                etiquetas = def_campo.get("validacion", {}).get("etiquetas")
                if etiquetas and valor_raw is not None:
                    val_display = etiquetas.get(str(valor_raw), str(valor_raw))
                
                if val_display is None:
                    val_display = "— (Pendiente)"
                
                col_target = cols[idx % 2]
                
                with col_target:
                    st.markdown(
                        f"""
                        <div class="campo-row">
                            <div>
                                <span class="campo-nombre">{def_campo.get('descripcion', nombre)}</span>
                                <span class="campo-codigo">({nombre})</span>
                            </div>
                            <div class="campo-valor">{val_display}</div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                    
                    # -------------------------------------------------------
                    # TRAZABILIDAD — Click para ver de dónde salió el dato
                    # -------------------------------------------------------
                    with st.popover(f"📍 Trazabilidad {nombre} ({len(campo_proy.evento_ids)} eventos)"):
                        st.markdown(f"#### Trazabilidad de **{nombre}** = `{valor_raw}`")
                        
                        # Caso especial: SCORE APACHE II con desglose de 12 variables
                        if nombre == "SCORE" and campo_proy.detalle and "componentes" in campo_proy.detalle:
                            st.markdown("##### Desglose de Puntos APACHE II:")
                            for c in campo_proy.detalle["componentes"]:
                                if c["puntos"] == 0 and c.get("faltante"):
                                    continue
                                nota_str = f" *({c['nota']})*" if c.get("nota") else ""
                                val_str = str(c['valor']) if c['valor'] is not None else ""
                                st.markdown(f"- **{c['etiqueta']}**: `{val_str}` ➔ **{c['puntos']} pts**{nota_str}")
                        
                        # Eventos que originaron el dato
                        if campo_proy.evento_ids:
                            st.markdown("##### Eventos originarios:")
                            for eid in campo_proy.evento_ids:
                                ev = eventos_por_id.get(eid)
                                if ev:
                                    st.markdown(
                                        f"📅 **`{ev.timestamp_clinico[:16]}`** | **`{ev.tipo_evento}`** | 👤 *{ev.autor}*  \n"
                                        f"> \"*{ev.texto_crudo}*\""
                                    )
                        elif nombre != "SCORE":
                            st.caption("Dato administrativo fijo del episodio (cargado al ingreso).")

    st.divider()
    
    # -----------------------------------------------------------------------
    # Panel de Validación y Exportación
    # -----------------------------------------------------------------------
    st.subheader("⚙️ Validación y Exportación SATI-Q")
    
    errs = validador.errores(hallazgos)
    advs = validador.advertencias(hallazgos)
    
    col_v1, col_v2 = st.columns(2)
    with col_v1:
        st.metric("Errores de Validación", len(errs), delta_color="inverse")
    with col_v2:
        st.metric("Advertencias Clínicas", len(advs), delta_color="off")
        
    if errs:
        st.error("❌ Hay errores de validación que impiden la exportación del CSV:")
        for h in errs:
            st.write(f"- {h}")
    else:
        st.success("✅ La fila SATI-Q cumple con las reglas de validación.")
        
    if advs:
        with st.expander("Ver advertencias de validación", expanded=False):
            for h in advs:
                st.warning(f"- {h}")
                
    st.divider()
    
    col_exp1, col_exp2, col_exp3 = st.columns(3)
    
    with col_exp1:
        st.markdown("##### 1. Exportación SATI-Q")
        if validador.es_exportable(hallazgos):
            csv_contenido = exportar_csv([valores_fila])
            st.download_button(
                label="📥 Descargar CSV Oficial",
                data=csv_contenido,
                file_name=f"satiq_episodio_{episodio_actual.idpaciente}.csv",
                mime="text/csv",
                use_container_width=True
            )
        else:
            st.button("📥 Descargar CSV Oficial", disabled=True, use_container_width=True)

    with col_exp2:
        st.markdown("##### 2. Informe para la Familia")
        if st.button("👨‍👩‍👧‍👦 Explicar Egreso (Gemma)", use_container_width=True):
            resumen_prompt = (
                f"Hombre de {episodio_actual.edad} años. Estuvo {valores_fila.get('ESTADIA', 0)} días en terapia intensiva.\n"
                f"Días con respirador: {valores_fila.get('DIASVI', 0)}. Vías centrales: {valores_fila.get('DIASCVC', 0)} días en total.\n"
                f"Tuvo una neumonía asociada al respirador: {'sí' if valores_fila.get('NEUMONIA') else 'no'}.\n"
                f"Tuvo escaras: {'sí' if valores_fila.get('ESCARAS') else 'no'}.\n"
                f"Al egreso pasa a sala de internación general."
            )
            with st.spinner("Gemma está generando la explicación en lenguaje llano..."):
                explicacion = gemma.explicar_egreso(resumen_prompt)
                st.session_state["explicacion_familia"] = explicacion
                
    with col_exp3:
        st.markdown("##### 3. Verificación VIHDA")
        infecciones = sorted({
            e.payload_json.get("codigo") for e in eventos_actuales
            if e.tipo_evento == "evento_adverso"
            and e.payload_json.get("codigo") in ("NEUMONIA", "INFCATETER", "INFURINARIA", "INFHERIDAS")
        })
        if not infecciones:
            st.button("🔍 Verificar criterios VIHDA", disabled=True, use_container_width=True)
            st.caption("No hay infecciones declaradas por el médico en este episodio.")
        else:
            codigo_sel = st.selectbox("Infección declarada", infecciones, label_visibility="collapsed")
            if st.button("🔍 Verificar criterios VIHDA", use_container_width=True):
                # El registro son las citas textuales de los eventos guardados,
                # no un texto de ejemplo: si se le pasa texto inventado, la
                # verificación no dice nada sobre este paciente.
                texto_registro = "\n".join(
                    f"[{e.timestamp_clinico[:16]}] {e.texto_crudo}"
                    for e in sorted(eventos_actuales, key=lambda x: x.timestamp_clinico)
                )
                with st.spinner("Gemma está revisando si el registro documenta los criterios..."):
                    st.session_state["eval_vihda"] = gemma.verificar_vihda(codigo_sel, texto_registro)

    # Mostrar explicaciones si existen
    if "explicacion_familia" in st.session_state and st.session_state["explicacion_familia"]:
        st.markdown("#### 💬 Resumen para la Familia (en lenguaje llano):")
        st.info(st.session_state["explicacion_familia"])

    if "eval_vihda" in st.session_state and st.session_state["eval_vihda"]:
        st.markdown("#### 🔬 Verificación Criterios VIHDA (Neumonía):")
        eval_v = st.session_state["eval_vihda"]
        st.caption(
            "El médico ya declaró la infección. Gemma no la confirma ni la discute: "
            "sólo revisa si el registro documenta lo que VIHDA exige para poder reportarla."
        )
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.markdown("**Documentado:**")
            for c in eval_v.get("cumplidos", []):
                st.success(f"✓ **{c.get('id', '')}**\n\n> *{c.get('evidencia', '')}*")
        with col_c2:
            st.markdown("**Falta documentar:**")
            for f in eval_v.get("faltantes", []):
                st.warning(f"✗ **{f.get('id', '')}**\n\n{f.get('que_falta', '')}")
            if eval_v.get("faltantes"):
                st.error("Con documentación incompleta, SATI-Q puede rechazar este caso.")
