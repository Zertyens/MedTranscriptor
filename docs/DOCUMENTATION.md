# 🩺 MedTranscriptor - Manual y Documentación Completa

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/ui-Streamlit-FF4B4B.svg)](https://streamlit.io/)
[![pywebview](https://img.shields.io/badge/Desktop-pywebview-0284c7.svg)](https://pywebview.flowrl.com/)
[![Whisper](https://img.shields.io/badge/Speech--to--Text-faster--whisper-00A67E.svg)](https://github.com/SYSTRAN/faster-whisper)
[![Google Gemma 4](https://img.shields.io/badge/AI-Gemma%204-4285F4.svg)](https://ai.google.dev/)
[![SATI-Q](https://img.shields.io/badge/Standard-SATI--Q%202026-0284c7.svg)](https://sati.org.ar/)

> **MedTranscriptor** es una plataforma integral para Unidades de Cuidados Intensivos (UCI) que registra la internación del paciente día a día mediante **Event Sourcing inmutable**. Permite el dictado directo por voz mediante **Whisper local offline**, y al egreso genera automáticamente y en tiempo real la fila de reporte oficial de **49 campos** para el registro nacional **SATI-Q** (Argentina), validada, trazable punto a punto y acompañada de un resumen en lenguaje llano para el paciente y su familia.

---

## 📋 Tabla de Contenidos

- [1. Descripción General](#1-descripción-general)
- [2. Estructura del Repositorio](#2-estructura-del-repositorio)
- [3. Instalación y Entorno](#3-instalación-y-entorno)
- [4. Pipeline y Flujo de Trabajo](#4-pipeline-y-flujo-de-trabajo)
- [5. Decisiones Técnicas Clave](#5-decisiones-técnicas-clave)
- [6. Resultados Principales y Demos](#6-resultados-principales-y-demos)
- [7. Reproducción del Análisis desde Cero](#7-reproducción-del-análisis-desde-cero)
- [📄 Arquitectura Detallada](ARCHITECTURE.md)

---

## 1. Descripción General

### El Problema Real
En Argentina, las Unidades de Cuidados Intensivos adheridas al programa nacional de calidad **SATI-Q** (Sociedad Argentina de Terapia Intensiva) deben reportar una planilla con **49 campos estandarizados** por cada paciente egresado. Estos campos incluyen:
- Días de uso de dispositivos invasivos (Ventilación Invasiva `VI`, Ventilación No Invasiva `VNI`, Cánula de Alto Flujo `CAFO`, Catéter Venoso Central `CVC`, Sonda Vesical `SV`, Sonda Enteral `SE`, Sonda Nasogástrica `SNG`).
- Eventos adversos e infecciones (Neumonía NAV, Infección por Catéter, Infección Urinaria, Escaras, Autoextubaciones, etc.).
- Scores fisiológicos y de carga asistencial de gravedad (**APACHE II** y **TISS-28**).

Tradicionalmente, esta planilla se confecciona **manualmente al momento del egreso**, reconstruyendo datos desde la historia clínica en papel o digital. Este proceso consume horas de médicos y enfermeros, introduce errores aritméticos/de memoria y carece de mecanismos para auditar de qué nota o timestamp proviene cada valor.

### La Solución MedTranscriptor
MedTranscriptor implementa la metáfora de un **resumen bancario**:
- En lugar de registrar el "saldo final" a mano al egreso, el sistema almacena **los movimientos diarios** (eventos fechados inmutables).
- La fila oficial de **49 campos nunca se almacena en la base de datos**; se proyecta en tiempo real calculando la aritmética de los eventos registrados.
- Cada número resultante es 100% auditable y trazable hasta el evento clínico originario con su autor, fecha y cita textual.

### División de Responsabilidades (Regla de Oro)
- **Speech-to-Text (`voz.py` con `faster-whisper`)**: Convierte notas de audio dictadas en vivo por el médico a texto en español de manera 100% local y offline en la CPU de la computadora, sin enviar audio a servidores externos.
- **Gemma (Google LLM - `gemma-4-26b-a4b-it`)**: Se encarga de comprender el lenguaje médico natural. Traduce notas dictadas a eventos estructurados fechados, audita evidencias según el manual VIHDA y redacta la explicación final en lenguaje sencillo para los familiares.
- **Python Determinista**: Realiza **todos** los cálculos matemáticos (conteo de días de dispositivos, cálculo de estadía, cálculo del score APACHE II peor valor en 24h, promedios TISS-28, validación de reglas SATI-Q y exportación CSV). *Gemma NUNCA suma, cuenta días ni calcula scores.*

### Datasets y Referencias Incorporadas
1. **Especificaciones SATI-Q 2026** (`EDS V2026 SATI-Q.xlsx - Anexo A1.csv` a `Anexo A4.csv`): Tablas oficiales de referencia de variables y la estructura exacta del header de 49 columnas (~22 KB).
2. **Esquemas JSON** (`schema/`):
   - `schema/satiq_campos.json`: Definición de los 49 campos y sus 49+ reglas de validación (tipos, rangos, condicionales).
   - `schema/apache2.json`: Rangos de puntuación fisiológica, oxigenación PaO2/A-aDO2 y ecuación de regresión de Knaus.
   - `schema/eventos.json`: Contrato estricto de tipos de eventos y esquemas de payload JSON.
   - `schema/vihda_criterios.json`: Criterios oficiales de vigilancia epidemiológica VIHDA para infecciones intrahospitalarias.
3. **Paciente Sintético de Prueba** (`semilla.py`): Episodio clínico inventado (62 años, sepsis respiratoria) con 7 notas de evolución médica preparadas para probar casos bordes reales (2 CVC simultáneos, fechas relativas "ayer/el martes", autoextubación, escara sacra y corrección de registros).
4. **Privacidad de Datos**: No se utilizan ni exponen datos reales de pacientes. El CSV de SATI-Q es anónimo por diseño (`IDCENTRO` hash, `IDPACIENTE` secuencial). La transcripción de voz es local (Whisper) y el modelo Gemma puede correrse de forma 100% local a través de Ollama.

---

## 2. Estructura del Repositorio

```text
MedTranscriptor/
├── MedTranscriptor.py         # Lanzador principal de la app en ventana de escritorio nativa (pywebview / Streamlit)
├── voz.py                     # Módulo de transcripción local offline de voz con faster-whisper (modelo small en CPU int8)
├── probar_voz.py              # Script CLI para validar la cadena completa: Audio -> Whisper -> Gemma -> SQLite BD
├── app.py                     # Interfaz web/escritorio interactiva en Streamlit (Grabar voz, Revisar, Fila SATI-Q)
├── gemma.py                   # Cliente e integración del modelo Gemma 4 (Google AI Studio / Ollama)
├── proyector.py               # Motor determinista de proyección de los 49 campos de SATI-Q y trazabilidad
├── apache2.py                 # Algoritmo de cálculo de APACHE II (peor valor 24h) y probabilidad de mortalidad
├── validador.py               # Validador de reglas clínicas y de formato sobre la fila proyectada SATI-Q
├── exportador.py              # Exportador de la fila validada a formato CSV oficial SATI-Q (delimitador ;)
├── db.py                      # Capa de persistencia SQLite de arquitectura Event Sourcing (Insert-Only)
├── modelos.py                 # Dataclasses de Episodio, Evento y esquemas de validación de payloads
├── semilla.py                 # Datos del paciente sintético de prueba y las 7 notas de evolución clínica
├── demo.py                    # Script CLI end-to-end para ejecutar y validar la demo completa (con caché)
├── CONTEXT.txt                # Documento original con las reglas de negocio y restricciones del proyecto
├── .env.example               # Plantilla de variables de entorno (API Keys, Backend, Modelo)
├── medtranscriptor.db         # Base de datos SQLite local (creada en ejecución)
├── schema/                    # Definiciones JSON y contratos de validación
│   ├── apache2.json           # Tablas de rangos y ponderación para el cálculo de APACHE II
│   ├── eventos.json           # Schema JSON de tipos de eventos y payloads admitidos
│   ├── satiq_campos.json      # Especificación completa de los 49 campos de la planilla SATI-Q
│   └── vihda_criterios.json   # Criterios del programa VIHDA para auditar evidencia de infecciones
├── demo/                      # Artefactos y caché de demostración
│   ├── eventos_cache.json     # Caché de eventos extraídos por Gemma para pruebas offline instantáneas
│   ├── vihda_cache.json       # Caché precalculado para auditoría de evidencia epidemiológica VIHDA
│   ├── salida_satiq.csv       # Archivo CSV generado por la demo oficial
│   └── audio/                 # Almacenamiento de notas de voz dictadas/probadas
├── docs/                      # Documentación técnica avanzada
│   ├── DOCUMENTATION.md       # Manual de usuario y documentación técnica completa
│   └── ARCHITECTURE.md        # Especificación detallada de arquitectura, schemas e ingeniería de prompts
└── EDS V2026 SATI-Q.xlsx - Anexo*.csv # Archivos de referencia oficiales del estándar SATI-Q 2026
```

---

## 3. Instalación y Entorno

### Requisitos Previos
- Python 3.10 o superior.
- Git (opcional).
- API Key de Google AI Studio (si se usa el backend en la nube) u Ollama (si se usa localmente).

### Pasos de Instalación

1. **Clonar / Ubicarse en el directorio del proyecto:**
   ```bash
   cd c:\Users\Pc\Desktop\Gemma\MedTranscriptor
   ```

2. **Crear y activar un entorno virtual de Python:**
   - **En Windows (PowerShell / CMD):**
     ```powershell
     python -m venv .venv
     .venv\Scripts\activate
     ```
   - **En Linux / macOS:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **Instalar dependencias del proyecto:**
   ```bash
   pip install streamlit faster-whisper pywebview
   ```

4. **Configurar el archivo de entorno `.env`:**
   Copiar la plantilla `.env.example` a `.env`:
   ```bash
   copy .env.example .env
   ```
   Editar `.env` e ingresar las credenciales:
   ```ini
   GOOGLE_AI_API_KEY=tu_api_key_de_google_ai_studio
   GEMMA_MODEL=gemma-4-26b-a4b-it
   GEMMA_BACKEND=google
   GEMMA_THINKING=high
   WHISPER_MODELO=small
   ```

---

## 4. Pipeline y Flujo de Trabajo

```mermaid
flowchart TD
    A[Dictado por Voz / Audio .wav .m4a] -->|voz.py - Whisper Local Offline int8| B[Texto Transcrito]
    B -->|gemma.py - LLM| C[Eventos Estructurados Fechados]
    C -->|modelos.py - Validación Payload| D[(db.py - SQLite Event Sourcing)]
    D -->|Proyectar Fila| E[proyector.py & apache2.py]
    E -->|49 Campos + Trazabilidad| F[validador.py - 49 Reglas SATI-Q]
    F -->|Fila Validada| G[exportador.py - CSV SATI-Q]
    F -->|Resumen Paciente / VIHDA| H[gemma.py - Redacción Llana & Criterios]
```

### Etapas del Pipeline

1. **Ingreso Administrativo (`Episodio`)**: Se registran al ingreso los datos fijos del paciente (Edad, Sexo, Procedencia, Motivo de Ingreso, Antecedentes Crónicos).
2. **Transcripción por Voz Offline (`voz.py`)**: El profesional dicta el parte médico. `faster-whisper` procesa el audio localmente en la CPU.
3. **Estructuración y Extracción por Gemma (`gemma.py`)**: Gemma analiza la nota transcripta en el contexto de la fecha de referencia y la lista de dispositivos previamente colocados (`instancias_abiertas`).
4. **Inserción Inmutable en Event Store (`db.py`)**: Si la confianza del evento es `< 0.75`, se marca para revisión humana en la UI. Se insertan sin `UPDATE` ni `DELETE`.
5. **Proyección Determinista (`proyector.py`)**: Se leen todos los eventos vigentes del episodio y se calculan determinísticamente los 49 campos SATI-Q.
6. **Cálculo de Gravamen APACHE II (`apache2.py`)**: Se analizan los eventos de las primeras 24 horas y se selecciona el peor valor fisiológico.
7. **Validación de Integridad (`validador.py`)**: Se contrasta la fila proyectada contra las 49+ reglas del estándar SATI-Q.
8. **Exportación e Informes (`exportador.py` / `gemma.py`)**: Generación del CSV oficial y redacción del informe explicativo en lenguaje llano.

---

## 5. Decisiones Técnicas Clave

1. **Lanzador de Escritorio Nativo (`MedTranscriptor.py`)**: Servidor Streamlit headless con puerto asignado dinámicamente (`socket`) para evitar conflictos, abierto en ventana nativa Edge WebView2 mediante `pywebview`.
2. **Transcripción Local Offline con Whisper (`voz.py`)**: Modelo `small` cuantizado en CPU (`int8`) con filtro VAD (`vad_filter=True`) para descartar silencios y acelerar la inferencia manteniendo 100% de privacidad.
3. **Arquitectura Event Sourcing Inmutable (`db.py`)**: Registro *Insert-Only*. Las correcciones médicas se insertan como un nuevo evento con `corrige_a_evento_id`.
4. **Proyección al Vuelo**: La fila SATI-Q nunca se almacena fija en la base de datos; se proyecta determinísticamente a partir de los eventos.
5. **Selección del Peor Valor en APACHE II (`apache2.py`)**: Evaluación por puntos de gravedad (no simple máximo/mínimo) en variables fisiológicas bidireccionales.
6. **Integración con Gemma 4 (`gemma.py`)**: `GEMMA_THINKING=high` para prevenir desvíos clínicos, descarte automático de *thought tokens* y normalización defensiva del JSON de salida en Python.

---

## 6. Resultados Principales y Demos

### 1. Aplicación de Escritorio Nativa (`MedTranscriptor.py`)
```bash
python MedTranscriptor.py
```

### 2. Prueba CLI de Dictado por Voz (`probar_voz.py`)
```bash
python probar_voz.py "C:\Ruta\A\Tu\grabacion.m4a" --guardar
```

### 3. Ejecución CLI de Demostración con Caché (`demo.py`)
```bash
python demo.py --cache
```

### 4. Interfaz Gráfica Streamlit (`app.py`)
```bash
streamlit run app.py
```

---

## 📄 Arquitectura Detallada

Para una profundización técnica sobre los esquemas de datos, la lógica del proyector, los algoritmos de APACHE II y la integración de Gemma 4, consultar:

👉 **[docs/ARCHITECTURE.md](ARCHITECTURE.md)**
