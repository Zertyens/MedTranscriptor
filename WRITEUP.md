## 1. El problema

SATI-Q es el registro nacional de calidad en terapia intensiva de Argentina. Cada UCI
adherida está obligada a reportar **una fila de 49 campos por cada paciente egresado**:
días de uso de cada dispositivo invasivo, eventos adversos, infecciones asociadas a
dispositivo y scores de gravedad.

Hoy esa planilla se llena **a mano, al egreso**, reconstruyendo desde el legajo clínico
qué pasó durante toda la internación. Tres consecuencias:

1. **Se pierden horas de personal** que debería estar en la cama del paciente.
2. **Se cometen errores**, porque nadie recuerda con precisión cuántos días estuvo puesto
   un catéter tres semanas después.
3. **No hay forma de auditar** de dónde salió cada número.

El tercer punto es el más grave. Los días de dispositivo son el **denominador** de las
tasas de infección con las que se compara a cada unidad a nivel nacional. Un denominador
mal reconstruido no es un dato de menos: es una tasa mal calculada que puede hacer que una
UCI parezca mejor o peor de lo que es.

---

## 2. La idea

**Es como un resumen bancario.**

Hoy la UCI anota **el saldo**: *"6 días de catéter"*.
Nosotros guardamos **los movimientos**: *"se colocó el martes a las 10"*, *"se retiró el
domingo"*.

El saldo se calcula solo, y cada número queda rastreable hasta los movimientos que lo
generaron.

Técnicamente es *event sourcing*: los eventos fechados son el único dato que se almacena,
y **la fila de 49 campos nunca se guarda — se proyecta al vuelo cada vez que se pide**.

---

## 3. Cómo usamos Gemma

Esta es la decisión que estructura todo el proyecto:

> ### Gemma decide QUÉ PASÓ clínicamente. Python LLEVA LA CUENTA.

**Gemma hace exactamente tres cosas, y ninguna involucra un cálculo:**

**① Traduce.** Convierte lo que el médico dicta en eventos fechados. De *"ayer a la mañana
le puse una central subclavia derecha, y a la tarde una segunda yugular"* extrae dos
eventos con instancias distintas (`CVC-1`, `CVC-2`), resolviendo las fechas relativas
contra la fecha de referencia. Cada evento lleva **la cita textual literal** que lo
justifica y un nivel de confianza propio.

**② Verifica.** El médico declara una infección — es su diagnóstico y su responsabilidad.
Gemma **no la confirma ni la cuestiona**: compara el registro clínico contra los criterios
del programa nacional de vigilancia VIHDA y devuelve qué está documentado y qué falta.

**③ Explica.** Al egreso redacta el resumen de la internación en lenguaje llano para el
paciente y su familia, sin siglas ni puntajes.

**Python determinista hace todo lo demás:** suma de días de dispositivo, cálculo de
estadía, APACHE II, TISS-28, las 49 reglas de validación y la exportación del CSV.

### Por qué Gemma nunca calcula

No es una preferencia de estilo. **Un error aritmético de un modelo de lenguaje en un
registro clínico nacional es inaceptable.** Si un módulo necesita un número, lo calcula
Python. Cada evento que Gemma devuelve pasa por un validador antes de tocar la base; el
que no valida no se inserta *y no se descarta en silencio*: queda en una lista de
rechazados con el motivo.

### El reencuadre que hicimos con las infecciones

Nuestro primer diseño tenía a Gemma **adjudicando** infecciones: decidiendo si un cuadro
clínico cumple los criterios VIHDA. Lo cambiamos, y el cambio mejoró el proyecto en las
tres dimensiones.

Los criterios de vigilancia epidemiológica no son criterios diagnósticos — son
definiciones de caso para contar y comparar entre instituciones. Técnicamente, aplicarlos
es codificar, no diagnosticar. Pero esa distinción es sutil, y un sistema que decide solo
termina usándose como si diagnosticara.

**Así quedó:**

```
Neumonía asociada a ventilación mecánica — declarada por Dr. Molina

  ✓ criterio radiológico     "infiltrado nuevo en la base derecha"
  ✓ fiebre > 38 °C           "pico febril de 38.7"
  ✓ leucocitosis             "glóbulos blancos en 21.400"
  ✗ aspirado purulento        falta documentar

  Con documentación incompleta, SATI-Q puede rechazar este caso.
```

Cero diagnóstico: el diagnóstico ya existía cuando Gemma entró. Y es **mejor demo**,
porque mostrar un hallazgo que le ahorra a la UCI un caso rechazado por el registro
nacional es impacto concreto y verificable.

### Detalles de integración que descubrimos midiendo

Estos no estaban documentados; salieron de probar la API:

- **`gemma-4-26b-a4b-it` es un modelo con razonamiento.** La respuesta viene en varias
  `parts` y la primera trae `thought: true`. Tomar `parts[0]` a ciegas — que es lo que
  hace cualquier tutorial — te devuelve el pensamiento del modelo, no su respuesta.
- **El razonamiento se controla por nivel, no por presupuesto.** `thinkingBudget` devuelve
  HTTP 400; `thinkingLevel: "minimal"` funciona y baja la latencia de 71 s a 16 s.
- **Pero `minimal` degrada la calidad clínica de forma peligrosa:** en nuestras pruebas
  mapeó *"creatinina 3.1"* a `leucocitos`. Para un registro clínico eso descalifica la
  opción. **El default quedó en `high`.**
- **`responseSchema` existe pero es inestable con esquemas anidados:** combinado con
  razonamiento alto devolvía timestamps basura del tipo `"2thoughtful_timestamp_format"`.
  Quedó apagado; el JSON se pide por prompt y Python normaliza el payload después.

La configuración final —`systemInstruction` + razonamiento alto + normalización
determinista en Python— da **3 corridas idénticas sobre la misma nota en ~13,5 s**.

---

## 4. Trazabilidad: el diferencial

Cada campo proyectado arrastra la lista de eventos que lo originaron. En la demo se
clickea un número y aparece de dónde salió, con hora, autor y la frase textual del médico.

Para el APACHE II el desglose es completo:

```
SCORE = 41

  Escala de Glasgow          8   →  7 pts      gas del 01/03 18:40, Dra. Pereyra
  Creatinina sérica        3.1   →  6 pts      duplicados por falla renal aguda
  Salud crónica                  →  5 pts      no quirúrgico
  Temperatura central     39.8   →  3 pts
  Frecuencia cardíaca      142   →  3 pts
  Oxigenación               58   →  3 pts      PaO₂ con FiO₂ 0.4
  Edad                      62   →  3 pts
  ...
```

Un score clínico compuesto, desarmado en sus partes, con el evento y el autor de cada una.

---

## 5. Qué está verificado

**La fila real del Anexo A4 de SATI-Q pasa nuestro validador con 0 errores.** No es un
test armado para que dé bien: es el dato oficial del organismo contra nuestras 49 reglas.

Una auditoría de 11 bloques **recalcula todo a mano** en vez de confiar en el motor, y
los 33 chequeos pasan:

| Verificación | Resultado |
|---|---|
| 49 campos en el orden exacto del CSV oficial | ✓ |
| Días de dispositivo recalculados para los 7 dispositivos | ✓ |
| Dos catéteres simultáneos suman por separado (10 días) | ✓ |
| `ESTADIA` cuenta el día de ingreso y no el de egreso | ✓ |
| `SCORE` igual a la suma de sus 14 componentes | ✓ |
| 17 tests de propiedad del APACHE II | ✓ |
| Eventos anulados siguen en la base y no cuentan | ✓ |
| 25 campos derivados citan sus eventos, 0 ids huérfanos | ✓ |
| 9 mutaciones inválidas rechazadas con el motivo | ✓ |

---

## 6. Hallazgos sobre la especificación

Trabajar en serio contra la norma produjo cosas que no esperábamos:

**El Anexo A4 tiene 49 columnas, no 48.** La documentación de referencia decía 48.
Contamos el header oficial y la fila de ejemplo: son 49. El CSV tiene que matchear o el
registro lo rechaza.

**Los días de dispositivo se cuentan por fecha calendario, no por horas transcurridas.**
Lo confirmamos consultando el manual VIHDA: *"un día calendario no debe interpretarse como
24 horas"*. Un catéter puesto a las 23:00 y retirado a la 01:00 del día siguiente cuenta
**2 días**, no 1. Nuestra primera implementación estaba mal y la corregimos.

**Encontramos una contradicción entre el manual VIHDA y el diccionario SATI-Q.** VIHDA
dice contar *"solo un catéter central por paciente por día calendario sin importar cuántas
líneas tenga"*. El Anexo A1 dice *"si un paciente tiene 2 CVC simultáneos se contarán los
días que cada CVC está colocado"*. Son incompatibles: 5 días contra 8 en el mismo
paciente. Resolvimos a favor de A1 —es la definición del campo que estamos exportando— y
adoptamos de VIHDA la unidad de conteo. Está documentado en el schema, no escondido.

---

## 7. Innovación e impacto

**Avisa el primer día, no al egreso.** Decirle al médico que faltan variables fisiológicas
para el APACHE II *al egreso* es inútil: la ventana de las primeras 24 horas ya pasó y el
dato no se puede recuperar. El sistema lo dice desde el día uno, cuando todavía se puede
medir — y explica la consecuencia sin vueltas: *"APACHE II las asume normales, así que el
puntaje de gravedad queda por debajo del real"*.

**Corregir nunca borra.** Un dato mal cargado se corrige insertando un registro nuevo que
anula al anterior. Los dos quedan a la vista. Es la promesa del resumen bancario llevada
hasta el final, y es lo que hace que el registro sea auditable de verdad.

**Admite lo que no entendió.** Cuando el médico dice algo que no se puede fechar con
certeza, va a una sección "Esto no lo entendí" en vez de inventarse un evento. En nuestra
prueba con voz real, Gemma mandó ahí los totales dictados (*"se contabilizaron 7 días de
respirador"*) — correctamente, porque esos números los calcula Python desde los
movimientos. Si los hubiera aceptado, tendríamos el problema que el proyecto existe para
resolver.

**Nada se guarda sin firma.** Los 9 puntos de guardado están bloqueados si el profesional
no se identificó. La trazabilidad entera se apoya en saber quién dijo qué.

---

## 8. Privacidad

El CSV de SATI-Q ya es anónimo por diseño: `IDCENTRO` es un hash e `IDPACIENTE` un entero
secuencial. No hay nombre ni documento.

**La transcripción de voz corre local** (faster-whisper): el audio nunca sale de la
máquina.

Gemma corre contra la API de Google AI para esta demo. `gemma.py` tiene un backend de
Ollama implementado: pasar a on-premise, donde el dato del paciente tampoco sale de la
institución, **es cambiar una variable de entorno**, no reescribir nada.

**Este repositorio no contiene ni un dato de paciente real.** Todo lo que se ve es un
paciente sintético inventado.

---

## 9. Limitaciones honestas

Preferimos decirlas nosotros:

1. **Las tablas del APACHE II no están validadas contra el paper original de Knaus 1985.**
   Fueron transcriptas de la tabla clásica y el archivo lleva la marca
   `_validacion_pendiente: true`. Ningún número clínico debería llegar a producción sin
   ese segundo par de ojos.
2. **TISS-28 se carga como puntaje, no como ítems.** El diseño correcto es que Gemma
   identifique qué intervenciones se aplicaron y Python sume los pesos. Falta la tabla de
   los 28 ítems.
3. **`PROBABMORT` tiene un sesgo sistemático conocido.** La regresión de Knaus usa ~50
   categorías diagnósticas; el campo `MOTING` de SATI-Q solo tiene 4 que no mapean. Se
   calcula con peso diagnóstico 0 y se emite una advertencia visible en cada proyección.
4. **Solo pacientes adultos.** Los rangos pediátricos están documentados en el schema pero
   PIM3 no está implementado.
5. **Whisper es el eslabón débil, no Gemma.** Sin GPU transcribe a ~1,4× tiempo real y
   puede errarle a fechas y términos médicos. Por eso el texto transcripto **siempre** se
   muestra editable antes de registrarse.

---

## 10. Cómo probarlo

```bash
git clone https://github.com/Zertyens/MedTranscriptor.git
cd MedTranscriptor
pip install streamlit faster-whisper pywebview
cp .env.example .env          # poner la API key de Google AI Studio
python MedTranscriptor.py     # abre la aplicación de escritorio
```

Recorrido completo por consola, sin red y sin consumir API:

```bash
python demo.py --cache
```

Muestra el pipeline entero: notas dictadas → eventos → proyección → trazabilidad →
verificación VIHDA → validación de las 49 reglas → CSV.

---

## 11. Qué sigue

- Validar las tablas del APACHE II y cargar los pesos de TISS-28 con un comité clínico
- Persistir el texto completo de las notas para que la auditoría VIHDA trabaje sobre el
  registro íntegro
- Migrar a Ollama on-premise (una variable de entorno)
- Extender a pacientes pediátricos y neonatales (PIM3)
- Derivar `REINGRESO` automáticamente del episodio previo del mismo paciente

---

**Stack:** Python puro · SQLite insert-only · Streamlit + pywebview ·
faster-whisper local · Gemma 4 vía Google AI (Ollama listo para enchufar)
