# 📐 Arquitectura Técnica y Diseño de MedTranscriptor

Este documento detalla la arquitectura del sistema, los principios de diseño, los contratos de datos y los algoritmos implementados en **MedTranscriptor**.

---

## 🛠️ 1. Principios Fundamentales de Diseño

### 1.1 Separation of Concerns (División Estricta de Responsabilidades)
El sistema establece un deslinde no negociable entre Inteligencia Artificial y Lógica Determinista:

| Capa / Componente | Dominio | Tecnología | Función Principal |
| :--- | :--- | :--- | :--- |
| **Speech-to-Text** | Transcripción de Voz | `faster-whisper` (`voz.py`) | Transcribe dictados médicos por voz a texto 100% local y offline en CPU (`int8`). |
| **Traducción y NLP** | Clínico / Lingüístico | Gemma 4 (`gemma-4-26b-a4b-it`) | Extrae eventos estructurados a partir de evoluciones médicas dictadas. |
| **Auditoría VIHDA** | Epidemiológico | Gemma 4 + Manual VIHDA | Verifica si la nota documenta la evidencia requerida para infecciones asociadas a dispositivos. |
| **Explicación al Paciente** | Comunicación Humanizada | Gemma 4 | Redacta resúmenes de egreso en lenguaje llano sin jerga médica. |
| **Persistencia** | Event Store Inmutable | SQLite (`db.py`) | Guarda episodios y eventos con semántica *Insert-Only*. |
| **Cálculo y Proyección** | Matemático / Determinista | Python (`proyector.py`, `apache2.py`) | Conteo de días, estadía, peores valores fisiológicos, score APACHE II y TISS-28. |
| **Validación y Formato** | Normativo / Reglas SATI-Q | Python (`validador.py`, `exportador.py`) | Aplica 49+ reglas de tipo, rango y consistencia; exporta el CSV oficial. |
| **Contenedor de Escritorio** | GUI Nativa | `pywebview` (`MedTranscriptor.py`) | Encapsula la app en una ventana de escritorio nativa WebView2 con puerto dinámico. |

### 1.2 Event Sourcing e Inmutabilidad
- **Base de Datos Insert-Only**: No se ejecutan consultas `UPDATE` ni `DELETE` sobre la tabla de eventos.
- **Correcciones Auditables**: Si un médico corrige una anotación previa (ej. *"La sonda se retiró hoy, no el martes"*), se inserta un evento nuevo que incluye `corrige_a_evento_id = <ID_EVENTO_ANTERIOR>`.
- **Filtro de Vigencia**: El motor de proyección invoca `eventos_vigentes()` para ignorar cualquier evento corregido por una entrada posterior.

---

## 🗄️ 2. Modelo de Datos y Esquemas JSON

### 2.1 Tablas Relacionales (`db.py`)

#### Tabla `episodio`
Almacena la información administrativa del ingreso a la UCI.
- `id` (TEXT PRIMARY KEY): UUIDv4 identificador único del episodio.
- `idcentro` (TEXT): Hash anónimo de la institución.
- `idpaciente` (INTEGER): Secuencial del paciente en el centro.
- `fecha_ingreso` (TEXT) / `hora_ingreso` (TEXT): ISO format (`YYYY-MM-DD` / `HH:mm:ss`).
- `tipo` (TEXT): Alcance `"A"` (Adultos).
- `edad` (INTEGER) / `sexo` (TEXT): Datos demográficos.
- `moting` (INTEGER): Motivo de ingreso (1: Patología Médica, 2: Quirúrgico Electivo, etc.).
- `procedencia` (INTEGER): Origen del paciente (1: Guardia, 2: Quirófano, etc.).
- `enfermedad_cronica_grave` (INTEGER): Flag `0/1`.

#### Tabla `evento`
Representa el libro de movimientos diarios.
- `id` (TEXT PRIMARY KEY): UUIDv4 identificador del evento.
- `episodio_id` (TEXT FOREIGN KEY): Enlace al episodio.
- `timestamp_clinico` (TEXT): Fecha y hora ISO del hecho clínico.
- `timestamp_carga` (TEXT): Timestamp del sistema al registrar el evento.
- `autor` (TEXT): Nombre o matrícula del profesional que dictó la nota.
- `tipo_evento` (TEXT): Enum cerrado (`dispositivo_inicio`, `dispositivo_fin`, `fisiologico_24h`, `evento_adverso`, `tiss_diario`, `egreso`).
- `payload_json` (TEXT): Datos específicos del evento codificados en JSON.
- `fuente` (TEXT): Origen del evento (`texto_gemma`, `audio_gemma`, `manual`, `importado`).
- `confianza` (REAL): Nivel de certeza de la extracción ($0.0 \le c \le 1.0$).
- `texto_crudo` (TEXT): Cita textual literal extraída de la nota.
- `corrige_a_evento_id` (TEXT NULL): Enlace al evento corregido si aplica.

---

## 🧮 3. Algoritmo APACHE II (`apache2.py`)

El score **APACHE II** evalúa la gravedad en las primeras 24 horas de internación en la UCI a partir de 12 variables fisiológicas, la edad y la salud crónica preexistente.

### 3.1 Criterio de Selección del Peor Valor
Para cada variable fisiológica en la ventana de las primeras 24 horas:
$$\text{Puntos}_{\text{variable}} = \max_{e \in \text{Eventos}_{24h}} \left( \text{LookupPuntos}(\text{Valor}(e)) \right)$$
Se evalúa la puntuación de gravedad de **cada** medición registrada y se selecciona el evento que genera la mayor cantidad de puntos (no necesariamente el valor numérico más alto o más bajo).

### 3.2 Oxigenación Fisiológica Atómica
El cálculo del componente de oxigenación requiere evaluar si la $\text{FiO}_2 \ge 0.50$ (para usar el Gradiente Alveolo-Arterial $A\text{-}a\text{DO}_2$) o $< 0.50$ (para usar $\text{PaO}_2$).

Si $\text{FiO}_2 \ge 0.50$ y no se registró $A\text{-}a\text{DO}_2$ de forma directa, se calcula mediante la ecuación de gas alveolar:
$$A\text{-}a\text{DO}_2 = \left[ \text{FiO}_2 \times (P_{\text{atm}} - 47) - \frac{\text{PaCO}_2}{0.8} \right] - \text{PaO}_2$$
Donde $P_{\text{atm}} = 760 \text{ mmHg}$ por defecto.

### 3.3 Probabilidad de Mortalidad (Knaus 1985)
La probabilidad de muerte intra-hospitalaria se calcula mediante la función logística:
$$\text{Logit} = -3.517 + (\text{Score APACHE II} \times 0.146) + \text{Ajuste}_{\text{urgencia}}$$
$$\text{Probabilidad (\%)} = \frac{100}{1 + e^{-\text{Logit}}}$$

---

## 📊 4. Motor de Proyección SATI-Q (`proyector.py`)

El proyector transforma la lista de eventos inmutables en los 49 campos normativos de SATI-Q.

### 4.1 Cálculo de Estadía
- Cuenta el día de ingreso, pero **no el día de egreso**.
- Si el ingreso y egreso suceden el mismo día calendario, la estadía es $1$ día.
$$\text{Estadía} = \max(1, \text{Fecha}_{\text{egreso}} - \text{Fecha}_{\text{ingreso}})$$

### 4.2 Días de Dispositivo e Instancias Simultáneas
- Cada dispositivo colocado lleva un `instancia_id` (ej. `CVC-1`, `CVC-2`).
- Dos instancias simultáneas del mismo dispositivo acumulan sus días por separado.
- Las fechas de inicio y fin cuentan según días calendario abarcados:
$$\text{Días}_{\text{instancia}} = (\text{Fecha}_{\text{fin}} - \text{Fecha}_{\text{inicio}}) + 1$$

---

## 🎙️ 5. Módulo de Transcripción de Voz Offline (`voz.py`)

### 5.1 Arquitectura con `faster-whisper`
- **Modelo por defecto**: `small` (~460 MB), equilibrado para procesamiento en CPUs estándar sin GPU dedicada (cuantización `int8`).
- **Filtrado de Actividad de Voz (VAD)**: Utiliza `vad_filter=True` para descartar automáticamente silencios y ruidos de fondo, acelerando la velocidad de inferencia hasta 3x.
- **Privacidad Local**: Inferencia 100% offline. Ningún archivo de audio sale de la institución.

### 5.2 Testeo y Cadena de Integración de Audio (`probar_voz.py`)
- Recibe archivos de audio en formatos estándar (`.wav`, `.m4a`).
- Copia y respalda el archivo en `demo/audio/nota_<fecha>_<nombre>`.
- Ejecuta `voz.transcribir()`, pasa el texto resultante a `gemma.traducir_nota()` e inserta en la base SQLite los eventos con el flag `--guardar`.

---

## 🖥️ 6. Lanzador de Aplicación de Escritorio (`MedTranscriptor.py`)

### 6.1 Gestión Dinámica de Puertos
Para evitar conflictos cuando existen servidores de Streamlit previamente abiertos o colgados:
```python
def puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
```

### 6.2 Integración con `pywebview`
- Arranca un subproceso de Streamlit en modo `--server.headless true`.
- Monitorea la disponibilidad del servidor antes de renderizar la ventana.
- Abre la interfaz nativa usando WebView2 (motor Edge en Windows) o realiza un fallback transparente al navegador por defecto si `pywebview` no está instalado o si se pasa el flag `--navegador`.

---

## 🤖 7. Integración con Gemma 4 (`gemma.py`)

### 7.1 Configuración de API y Razonamiento

```python
# Parámetros optimizados para Gemma 4 en Google AI Studio
config = {
    "temperature": 0.1,
    "thinkingConfig": {
        "thinkingLevel": "high" # Previene degradación en la extracción clínica
    }
}
```

### 7.2 Descarte de Bloques de Pensamiento (*Thought Tokens*)
Las respuestas de Gemma 4 incluyen explicaciones internas de razonamiento con el flag `thought=True`. La función de transporte en Python las filtra de forma segura:

```python
texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
```

### 7.3 Mantenimiento del Estado Abierto (`instancias_abiertas`)
Para que Gemma mantenga coherencia al procesar múltiples notas consecutivas del mismo paciente, Python le envía en el prompt el diccionario de dispositivos que aún permanecen colocados con sus respectivos `instancia_id`.

---

## 🛡️ 8. Validador y Exportación (`validador.py` / `exportador.py`)

### 8.1 Niveles de Severidad
1. **ERROR**: Violación de tipo, formato, valor fuera de rango o campo obligatorio omitido. **Bloquea la exportación del CSV.**
2. **ADVERTENCIA**: Indicadores de inconsistencia (ej. APACHE II calculado con más de 4 variables faltantes o estadías inusualmente prolongadas). **Permite la exportación pero alerta al usuario.**

### 8.2 Especificación del CSV SATI-Q (Anexo A4)
- **Delimitador**: Punto y coma (`;`).
- **Codificación**: Windows ANSI / ISO-8859-1 (compatibilidad con sistemas hospitalarios legado) o UTF-8.
- **Formato de Números**: Separador decimal de punto (`.`), máximo 2 decimales, sin ceros a la derecha innecesarios.
