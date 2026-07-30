"""
Transcripcion local de audio con faster-whisper.

Corre 100% en la maquina: no es una API, no sale nada a internet. El modelo
se baja la primera vez y queda cacheado.

Eleccion de modelo: en una maquina sin GPU (como el i5-7200U donde se
desarrollo esto) 'large-v3-turbo' anda cerca de tiempo real o mas lento, lo
que en una demo en vivo son 40 segundos de pantalla quieta. 'small' es varias
veces mas rapido y para dictado claro en espaniol alcanza de sobra: Gemma
despues interpreta, no necesita una transcripcion perfecta.

    python voz.py grabacion.wav              transcribe y muestra el texto
    python voz.py grabacion.wav --modelo large-v3-turbo
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MODELO_DEFECTO = os.environ.get("WHISPER_MODELO", "small")

_modelos_cargados: dict[str, object] = {}


class ErrorVoz(Exception):
    pass


def cargar_modelo(nombre: str = MODELO_DEFECTO):
    """Carga el modelo una sola vez por proceso. La primera llamada baja los
    pesos (small ~460MB, large-v3-turbo ~1.6GB)."""
    if nombre in _modelos_cargados:
        return _modelos_cargados[nombre]
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ErrorVoz(
            "faster-whisper no esta instalado. Corre: pip install faster-whisper\n"
            "La app funciona igual sin esto: se pega el texto a mano."
        ) from e

    # int8 en CPU: sin GPU no hay otra opcion razonable, y entra en RAM comoda.
    modelo = WhisperModel(nombre, device="cpu", compute_type="int8")
    _modelos_cargados[nombre] = modelo
    return modelo


def transcribir(audio: str | Path, modelo: str = MODELO_DEFECTO) -> tuple[str, float]:
    """Devuelve (texto, segundos_que_tardo)."""
    ruta = Path(audio)
    if not ruta.exists():
        raise ErrorVoz(f"no existe el archivo {ruta}")

    inicio = time.time()
    segmentos, _ = cargar_modelo(modelo).transcribe(
        str(ruta),
        language="es",
        vad_filter=True,          # descarta silencios: acelera y limpia
        beam_size=5,
    )
    texto = " ".join(s.text.strip() for s in segmentos).strip()
    return texto, round(time.time() - inicio, 1)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    audio = sys.argv[1]
    modelo = MODELO_DEFECTO
    if "--modelo" in sys.argv:
        modelo = sys.argv[sys.argv.index("--modelo") + 1]

    print(f"Modelo: {modelo}  (la primera vez baja los pesos, puede tardar)")
    texto, segundos = transcribir(audio, modelo)

    duracion = ""
    try:
        import wave
        with wave.open(audio, "rb") as w:
            seg_audio = w.getnframes() / w.getframerate()
            duracion = f"  |  audio de {seg_audio:.0f}s  ->  {segundos / seg_audio:.1f}x tiempo real"
    except Exception:
        pass

    print(f"Transcripcion en {segundos}s{duracion}\n")
    print(texto)


if __name__ == "__main__":
    main()
