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

import json
import os
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

    /* Tarjetas de eventos */
    .evento-card {
        background-color: #ffffff;
        border: 1px solid #e2e8f0;
        border-left: 5px solid #0284c7;
        border-radius: 8px;
        padding: 1rem 1.25rem;
        margin-bottom: 1rem;
        box-shadow: 0 2px 4px rgba(0,0,0,0.03);
    }
    .evento-card-revision {
        border-left: 5px solid #eab308 !important;
        background-color: #fefce8 !important;
    }
    .evento-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 0.5rem;
    }
    .evento-tipo {
        font-weight: 700;
        color: #0f172a;
        font-size: 1.1rem;
    }
    .evento-meta {
        color: #64748b;
        font-size: 0.88rem;
    }
    .cita-textual {
        background-color: #f8fafc;
        border-left: 3px solid #cbd5e1;
        padding: 0.5rem 0.75rem;
        font-style: italic;
        color: #334155;
        font-size: 0.95rem;
        margin-top: 0.5rem;
        border-radius: 4px;
    }
    
    /* Badges */
    .badge-revision {
        background-color: #fef08a;
        color: #854d0e;
        padding: 0.25rem 0.6rem;
        border-radius: 9999px;
        font-size: 0.8rem;
        font-weight: 600;
        display: inline-block;
    }
    .badge-confianza {
        background-color: #e0f2fe;
        color: #0369a1;
        padding: 0.2rem 0.5rem;
        border-radius: 4px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    
    /* Sección SATI-Q */
    .satiq-seccion {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 1.2rem;
        margin-bottom: 1.5rem;
    }
    .satiq-seccion-titulo {
        font-size: 1.25rem;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 1rem;
        border-bottom: 2px solid #cbd5e1;
        padding-bottom: 0.4rem;
    }
    .campo-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 0.6rem 0;
        border-bottom: 1px solid #f1f5f9;
    }
    .campo-nombre {
        font-weight: 600;
        color: #334155;
        font-size: 0.95rem;
    }
    .campo-codigo {
        color: #94a3b8;
        font-size: 0.8rem;
        margin-left: 0.4rem;
    }
    .campo-valor {
        font-weight: 700;
        color: #0f172a;
        font-size: 1rem;
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

# Intentar transcripción con faster-whisper (con fallback seguro)
def transcribir_con_whisper(ruta_audio: Path) -> str | None:
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", compute_type="float32")
        segments, _ = model.transcribe(str(ruta_audio), language="es")
        texto = " ".join([s.text for s in segments]).strip()
        return texto if texto else None
    except Exception:
        return None

# Mapeo de campos a sus definiciones en el schema
CAMPOS_DICT = {c["nombre"]: c for c in validador.CAMPOS}

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
    st.image("https://img.icons8.com/color/96/hospital-room.png", width=70)
    st.title("MedTranscriptor")
    st.caption("Sistema Inteligente de Registro UCI")
    
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
            st.session_state["texto_transcripto"] = nota_sel["texto"]
            st.session_state["autor_nota"] = nota_sel["autor"]
            try:
                st.session_state["fecha_ref"] = datetime.strptime(nota_sel["fecha_referencia"], "%Y-%m-%d").date()
            except Exception:
                pass
            st.success(f"Nota {opcion_nota+1} cargada en el editor.")
            st.rerun()

    with col_input:
        st.subheader("Grabación de Voz")
        audio_dictado = st.audio_input("Dictá la evolución médica")
        
        texto_inicial = st.session_state.get("texto_transcripto", "")
        
        if audio_dictado is not None:
            audio_bytes = audio_dictado.getvalue()
            ruta_audio = guardar_archivo_audio(audio_bytes)
            st.toast(f"Audio guardado en `{ruta_audio}`", icon="💾")
            
            # Intentar Whisper
            with st.spinner("Transcribiendo audio con Whisper (modelo small)..."):
                whisper_texto = transcribir_con_whisper(ruta_audio)
                if whisper_texto:
                    texto_inicial = whisper_texto
                    st.success("Transcripción automática completada con Whisper.")
                else:
                    st.warning("No se detectó `faster-whisper` instalado o falló el reconocimiento. Podés editar o pegar la nota a mano a continuación.")
        
        texto_nota = st.text_area(
            "Texto de la nota de evolución (revisá o editá el texto antes de procesar):",
            value=texto_inicial,
            height=200,
            placeholder="Ej: Ingresa a las tres de la tarde derivado de guardia, sepsis a foco respiratorio. A las cuatro de la tarde lo intubo..."
        )
        
        if st.button("🤖 Procesar con Gemma", type="primary", use_container_width=True, disabled=not texto_nota.strip()):
            eventos_existentes = db.get_eventos(con, episodio_actual.id)
            abiertas = gemma.instancias_abiertas(eventos_existentes)
            
            fecha_str = fecha_ref.strftime("%Y-%m-%d")
            
            with st.spinner("Gemma está analizando la nota y extrayendo los eventos clínicos (~13 segundos)..."):
                resultado = gemma.traducir_nota(
                    episodio_id=episodio_actual.id,
                    nota=texto_nota,
                    fecha_referencia=fecha_str,
                    autor=autor_nota,
                    abiertas=abiertas
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
            
            st.markdown(
                f"""
                <div class="{card_class}">
                    <div class="evento-header">
                        <span class="evento-tipo">#{i} {ev.tipo_evento.upper()}</span>
                        <span class="evento-meta">🕒 {ev.timestamp_clinico} | 👤 {ev.autor}</span>
                    </div>
                    <div><strong>Payload:</strong> <code>{json.dumps(ev.payload_json, ensure_ascii=False)}</code></div>
                    <div class="cita-textual">"{ev.texto_crudo}"</div>
                    <div style="margin-top: 0.5rem;">
                        <span class="badge-confianza">Confianza: {ev.confianza}</span>
                        {'<span class="badge-revision">⚠️ Requiere revisión humana</span>' if es_dudoso else ''}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
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
            for e in sorted(eventos_bd, key=lambda x: x.timestamp_clinico):
                p = e.payload_json
                detalle = p.get("dispositivo") or p.get("codigo") or ""
                if "mediciones" in p:
                    detalle = ", ".join(f"{k}={v}" for k, v in list(p["mediciones"].items())[:3])
                instancia = p.get("instancia_id", "")
                
                color_conf = "🟡" if e.confianza < modelos.UMBRAL_REVISION else "🟢"
                st.markdown(
                    f"{color_conf} **`{e.timestamp_clinico[:16]}`** | **`{e.tipo_evento}`** | {detalle} `{instancia}`  \n"
                    f"└ *{e.autor}* (conf: `{e.confianza}`) — *\"{e.texto_crudo}\"*"
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
        if st.button("🔍 Criterios Infección Neumonía", use_container_width=True):
            texto_registro = "Radiografía con infiltrado nuevo. Fiebre 38.7, glóbulos blancos 21.400, secreción purulenta."
            with st.spinner("Gemma está evaluando los criterios VIHDA..."):
                eval_vihda = gemma.verificar_vihda("NEUMONIA", texto_registro)
                st.session_state["eval_vihda"] = eval_vihda

    # Mostrar explicaciones si existen
    if "explicacion_familia" in st.session_state and st.session_state["explicacion_familia"]:
        st.markdown("#### 💬 Resumen para la Familia (en lenguaje llano):")
        st.info(st.session_state["explicacion_familia"])

    if "eval_vihda" in st.session_state and st.session_state["eval_vihda"]:
        st.markdown("#### 🔬 Verificación Criterios VIHDA (Neumonía):")
        eval_v = st.session_state["eval_vihda"]
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.markdown("**Cumplidos:**")
            for c in eval_v.get("cumplidos", []):
                st.success(f"✓ {c}")
        with col_c2:
            st.markdown("**Faltantes:**")
            for f in eval_v.get("faltantes", []):
                st.warning(f"✗ {f}")
