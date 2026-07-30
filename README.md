# 🩺 MedTranscriptor

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B.svg)](https://streamlit.io/)
[![pywebview](https://img.shields.io/badge/Desktop-pywebview-0284c7.svg)](https://pywebview.flowrl.com/)
[![Whisper](https://img.shields.io/badge/Speech--to--Text-faster--whisper-00A67E.svg)](https://github.com/SYSTRAN/faster-whisper)
[![Google Gemma 4](https://img.shields.io/badge/AI-Gemma%204-4285F4.svg)](https://ai.google.dev/)
[![SATI-Q](https://img.shields.io/badge/Standard-SATI--Q%202026-0284c7.svg)](https://sati.org.ar/)

**El médico dicta. El sistema calcula. Cada número se puede rastrear hasta la frase que lo originó.**

Registro de internación para Unidades de Cuidados Intensivos que convierte lo que el
médico dicta en eventos fechados y, al egreso, proyecta automáticamente la fila del
registro nacional **SATI-Q** (Argentina) — validada, exportable y auditable campo por campo.

---

## El problema

SATI-Q es el registro nacional de calidad en terapia intensiva. Cada UCI adherida debe
reportar **una fila de 49 campos por paciente egresado**: días de uso de cada dispositivo,
eventos adversos, infecciones asociadas a dispositivo, y scores de gravedad.

Hoy esa planilla se llena **a mano al egreso**, reconstruyendo días y eventos desde el
legajo clínico. Se pierden horas de personal, se cometen errores, y no hay forma de
auditar de dónde salió cada número.

## La idea

**Es como un resumen bancario.** Hoy la UCI anota *el saldo* ("6 días de catéter").
Nosotros guardamos *los movimientos* ("se colocó el martes a las 10", "se retiró el
domingo"). El saldo se calcula solo, y cada número es rastreable hasta los movimientos
que lo generaron.

Técnicamente es *event sourcing*: los eventos son el dato guardado, y **la fila de 49
campos nunca se almacena — se proyecta al vuelo cada vez**.

---

## El reparto de responsabilidades

Es la regla que estructura todo el proyecto:

> **Gemma decide QUÉ PASÓ clínicamente. Python LLEVA LA CUENTA.**

**Gemma** (`gemma-4-26b-a4b-it`) hace exactamente tres cosas:

| | Función | Qué hace |
|---|---|---|
| 1 | `traducir_nota()` | Convierte lo dictado en eventos fechados, con la cita textual que justifica cada uno |
| 2 | `verificar_vihda()` | Revisa si el registro documenta lo que el programa de vigilancia exige |
| 3 | `explicar_egreso()` | Redacta el resumen de la internación en lenguaje llano para la familia |

**Python determinista** hace todo lo demás: suma de días de dispositivo, cálculo de
estadía, APACHE II, TISS-28, las 49 reglas de validación y la exportación del CSV.

**Gemma nunca calcula, suma, cuenta días ni valida.** No es una preferencia de estilo:
un error aritmético de un modelo de lenguaje en un registro clínico es inaceptable.

### Sobre la adjudicación de infecciones

Gemma **no decide si hay infección**. El médico la declara — es su diagnóstico y su
responsabilidad. Gemma compara esa declaración contra los criterios VIHDA y devuelve
**qué está documentado y qué falta**:

```
Neumonía asociada a ventilación mecánica — declarada por Dr. Molina

  ✓ criterio radiológico     "infiltrado nuevo en la base derecha"
  ✓ fiebre > 38 °C           "pico febril de 38.7"
  ✓ leucocitosis             "glóbulos blancos en 21.400"
  ✗ aspirado purulento       falta documentar

  Con documentación incompleta, SATI-Q puede rechazar este caso.
```

Es una auditoría de completitud del registro, no un diagnóstico.

---

## Arquitectura

```
   AUDIO del médico
        │  Whisper local (faster-whisper) — el audio no sale de la máquina
        ▼
   TEXTO  "le puse una central subclavia ayer a las 10"
        │  Gemma
        ▼
   EVENTO  {dispositivo_inicio, CVC, CVC-1, 2025-03-02T10:00,
            confianza 0.9, cita: "central subclavia ayer a las 10"}
        │  validación (modelos.py) → SQLite insert-only (db.py)
        ▼
   PROYECCIÓN  DIASCVC = 10 · SCORE = 41 · ESTADIA = 7 ...
        │  cada campo trae los ids de evento que lo originaron
        ▼
   VALIDACIÓN de las 49 reglas → CSV para SATI-Q
```

### Los archivos

| Archivo | Responsabilidad |
|---|---|
| `schema/satiq_campos.json` | **Fuente única de verdad.** Los 49 campos: orden del CSV, cómo se proyecta cada uno, cómo se valida |
| `schema/eventos.json` | Contrato del libro de movimientos: tipos de evento, payloads, catálogos |
| `schema/apache2.json` | Tablas de puntos del APACHE II |
| `schema/vihda_criterios.json` | Criterios de vigilancia de las 4 infecciones |
| `modelos.py` | `Episodio` y `Evento`, con validación contra el schema |
| `db.py` | Persistencia. **Insert-only sobre eventos** |
| `proyector.py` | 15 funciones que proyectan los 49 campos, con trazabilidad |
| `apache2.py` | APACHE II y probabilidad de muerte |
| `validador.py` | Las 49 reglas, genérico: lee el schema, no tiene reglas escritas a mano |
| `exportador.py` | CSV en el orden exacto del Anexo A4 |
| `gemma.py` | Cliente del modelo. Backend Google AI u Ollama, intercambiable |
| `voz.py` | Transcripción local |
| `app.py` | Interfaz clínica (Streamlit) |
| `demo.py` | Recorrido completo por consola |

---

## Cómo se usa

### Inicio rápido (Quick Start)

1. Clonar e instalar dependencias

```bash
git clone https://github.com/Zertyens/MedTranscriptor.git
cd MedTranscriptor

python -m venv .venv
# En Windows: .venv\Scripts\activate | En Linux/macOS: source .venv/bin/activate

pip install streamlit faster-whisper pywebview
```

2. Configurar credenciales (`.env`)

Copiar `.env.example` a `.env` e ingresar tu clave de Google AI Studio si usarás el backend de Google:

```ini
GOOGLE_AI_API_KEY=tu_api_key_de_google_ai_studio
GEMMA_MODEL=gemma-4-26b-a4b-it
GEMMA_BACKEND=google
GEMMA_THINKING=high
```

3. Iniciar MedTranscriptor

```bash
# Modo Aplicación de Escritorio Nativa (Recomendado):
python MedTranscriptor.py

# Modo Demo CLI Offline (Instantáneo sin consumo de API):
python demo.py --cache
```

Si el micrófono no funciona en la ventana nativa:

```bash
python MedTranscriptor.py --navegador
```

### El recorrido por consola

```bash
python demo.py --cache
```

Muestra el pipeline completo sin llamar a la red: notas dictadas → eventos →
proyección → trazabilidad → verificación VIHDA → validación → CSV.

---

## Qué está verificado

No son afirmaciones: hay tests que lo comprueban.

**La fila real del Anexo A4 de SATI-Q pasa nuestro validador con 0 errores.**
No es un test armado para que dé bien — es el dato oficial contra nuestras 49 reglas.

Además:

- **49 campos** en el orden exacto del CSV oficial
- **Días de dispositivo** recalculados a mano para los 7 dispositivos, incluidos dos
  catéteres simultáneos que suman sus días por separado (10 días)
- **APACHE II**: 17 tests de propiedad — el peor valor gana por puntos y no por
  magnitud (33 °C y 41 °C ambos puntúan), la creatinina se duplica con falla renal,
  la oxigenación elige su rama según el FiO₂ del mismo gas
- **Inmutabilidad**: los eventos anulados siguen en la base y no cuentan en ningún campo
- **Trazabilidad**: los 25 campos derivados con valor citan sus eventos, cero ids huérfanos
- **9 mutaciones inválidas rechazadas** por el validador, con el motivo explicado

---

## Decisiones que vale la pena conocer

**El Anexo A4 tiene 49 columnas, no 48.** Contamos el header oficial y la fila de
ejemplo. Manda el A4, porque el CSV tiene que matchear o SATI-Q lo rechaza.

**Los días de dispositivo se cuentan por fecha calendario, no por horas.** Lo
confirmamos consultando el manual VIHDA: *"un día calendario no debe interpretarse como
24 horas"*. Un catéter puesto a las 23:00 y retirado a la 01:00 del día siguiente
cuenta 2 días, no 1.

**Encontramos una contradicción entre el manual VIHDA y el diccionario SATI-Q.** VIHDA
dice contar un solo catéter central por paciente por día; el Anexo A1 dice contar los
días de cada catéter por separado. Resolvimos a favor de A1 — es la definición del campo
que estamos exportando — y adoptamos de VIHDA la unidad de conteo. Está documentado en
el schema.

**`PROBABMORT` tiene un sesgo sistemático conocido y declarado.** La regresión de Knaus
usa una tabla de ~50 categorías diagnósticas; el campo `MOTING` de SATI-Q solo tiene 4
categorías amplias que no mapean. Se calcula con peso diagnóstico 0 y se emite una
advertencia visible en cada proyección. Preferimos declararlo antes que esconderlo.

**Corregir nunca borra.** Un dato mal cargado se corrige insertando un registro nuevo
que anula al anterior. Los dos quedan a la vista. Es la promesa del resumen bancario.

**El sistema avisa el primer día, no al egreso.** Si faltan variables fisiológicas para
el APACHE II, decirlo al egreso es inútil: esa ventana ya pasó. El panel de pendientes
lo muestra desde el día uno, cuando todavía se puede medir.

---

## Privacidad

El CSV de SATI-Q ya es anónimo por diseño: `IDCENTRO` es un hash e `IDPACIENTE` un
entero secuencial. No hay nombre ni documento.

La transcripción de voz **corre local**: el audio nunca sale de la máquina.

Gemma corre hoy contra la API de Google AI para la demo. `gemma.py` tiene un backend de
Ollama listo: pasar a on-premise, donde el dato del paciente tampoco sale de la
institución, **es cambiar una variable de entorno**, no reescribir nada.

**Este repositorio no contiene ni un dato de paciente real.** Todo lo que se ve es un
paciente sintético inventado.

---

## Limitaciones honestas

Cosas que un jurado podría preguntar y que preferimos decir nosotros:

1. **`schema/apache2.json` tiene `_validacion_pendiente: true`.** Las tablas de puntos
   fueron transcriptas de la tabla clásica del APACHE II y **no están verificadas contra
   el paper original de Knaus 1985**. Ningún número clínico debería llegar a producción
   sin ese segundo par de ojos.

2. **TISS-28 se carga como puntaje, no como ítems.** El diseño correcto es que Gemma
   identifique qué intervenciones se aplicaron y Python sume los pesos. Falta la tabla
   de los 28 ítems del paper de Reis Miranda 1996.

3. **No se persiste el texto completo de las notas.** Se guarda la cita textual de cada
   evento (`texto_crudo`), pero no la nota entera. La auditoría VIHDA reconstruye el
   registro desde esas citas.

4. **Solo pacientes adultos** (`TIPO=A`, APACHE II). Los rangos pediátricos y neonatales
   están documentados en el schema pero PIM3 no está implementado.

5. **Whisper es el eslabón débil, no Gemma.** En una máquina sin GPU, `small` transcribe
   a ~1,4× tiempo real y puede errarle a fechas y términos médicos. Por eso el texto
   transcripto siempre se muestra editable antes de registrarse.

---

## Créditos

Hackathon Gemma. El manual de vigilancia VIHDA se consultó vía NotebookLM sobre el PDF
oficial del Programa Nacional de Epidemiología y Control de Infecciones Hospitalarias.

Especificación: *EDS V2026 SATI-Q*, Anexos A1 a A4.

