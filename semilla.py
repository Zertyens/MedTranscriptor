"""
Paciente sintetico para la demo.

TODO ACA ES INVENTADO. No hay ni un dato de paciente real: el repositorio es
publico y eso no se negocia.

El episodio (datos administrativos del ingreso) se carga como formulario, que
es como funciona en la realidad. Todo lo demas sale de las notas dictadas que
estan abajo: los dispositivos, los eventos adversos, los valores fisiologicos
y el egreso los extrae Gemma de este texto.

Las notas estan escritas para ejercitar a proposito los casos dificiles:
  - dos CVC simultaneos (instancias que suman por separado)
  - un dispositivo de menos de 24h
  - fechas relativas ("ayer", "el martes")
  - una correccion en una nota posterior
  - un evento adverso no infeccioso (escara)
  - una infeccion declarada por el medico, con criterios VIHDA parciales
  - un fragmento deliberadamente ambiguo que tiene que caer en no_entendido
"""
from __future__ import annotations

from modelos import Episodio

IDCENTRO = "96a3974cc1311994464e4402591fbd73"


def crear_episodio() -> Episodio:
    """Hombre de 62 anios, patologia medica, ingresa desde la guardia."""
    return Episodio(
        idcentro=IDCENTRO,
        idpaciente=1,
        reingreso=0,
        fecha_ingreso="2025-03-01",
        hora_ingreso="15:00:00",
        tipo="A",
        edad=62,
        sexo="M",
        moting=1,          # Patologia Medica
        procedencia=1,     # Guardia
        enfermedad_cronica_grave=True,
    )


# Cada nota es lo que el medico dicta al pasar visita. La fecha_referencia es
# el dia en que la dicta: Gemma resuelve contra ella las fechas relativas.
NOTAS = [
    {
        "fecha_referencia": "2025-03-01",
        "autor": "Dra. Pereyra",
        "texto": (
            "Ingresa a las tres de la tarde derivado de guardia, sepsis a foco respiratorio. "
            "Mal estado general, taquipneico, satura mal con mascara. A las cuatro de la tarde "
            "lo intubo y queda en ARM. Temperatura 39.8, frecuencia cardiaca 142, tensión arterial "
            "media 62, frecuencia respiratoria 28, Glasgow 8. Gases con FiO2 al 40 por ciento: "
            "PaO2 58, PaCO2 48, pH 7.28. Laboratorio: creatinina 3.1, sodio 128, potasio 5.1, "
            "hematocrito 28, globulos blancos 21 mil. Tiene falla renal aguda."
        ),
    },
    {
        "fecha_referencia": "2025-03-02",
        "autor": "Dra. Pereyra",
        "texto": (
            "Sigue en ARM, sedado. Hoy a la manana le coloque una via central subclavia derecha. "
            "A la tarde tuve que poner una segunda central yugular porque necesitabamos mas "
            "accesos para los inotropicos. Le pase una sonda nasogastrica tambien. "
            "TISS del dia 32 puntos."
        ),
    },
    {
        "fecha_referencia": "2025-03-03",
        "autor": "Dr. Molina",
        "texto": (
            "Paciente estable dentro de la gravedad. Le colocamos sonda vesical a la manana "
            "y sonda enteral para empezar alimentacion. Estuvo un rato con canula de alto flujo "
            "durante una prueba, unas ocho horas, despues volvio a ARM. TISS 28."
        ),
    },
    {
        "fecha_referencia": "2025-03-04",
        "autor": "Dr. Molina",
        "texto": (
            "Hoy hace un pico febril de 38.7. La radiografia de torax de hoy muestra un infiltrado "
            "nuevo en la base derecha que no estaba en la del lunes. Globulos blancos en 21.400. "
            "Lo interpreto como neumonia asociada a la ventilacion mecanica y arranco antibioticos. "
            "Le saque la central yugular, la subclavia queda. TISS 30."
        ),
    },
    {
        "fecha_referencia": "2025-03-05",
        "autor": "Dra. Pereyra",
        "texto": (
            "Mejor. Ayer a la tarde le sacamos la sonda nasogastrica. Hoy al mediodia lo extubo, "
            "queda respirando espontaneamente. Le encontramos una escara sacra grado 2 en la "
            "curacion de la manana. Hubo un tema con la familia el otro dia pero no viene al caso. "
            "TISS 22."
        ),
    },
    {
        "fecha_referencia": "2025-03-06",
        "autor": "Dr. Molina",
        "texto": (
            "Corrijo lo que quedo anotado ayer: la sonda vesical se retiro hoy a la manana, "
            "no el martes como figura. Paciente bien, tolera la alimentacion. TISS 18."
        ),
    },
    {
        "fecha_referencia": "2025-03-08",
        "autor": "Dra. Pereyra",
        "texto": (
            "Le retiro la central subclavia y la sonda enteral esta manana. Paciente en condiciones "
            "de salir de terapia. Hoy a las once de la manana pasa a sala de internacion general."
        ),
    },
]
