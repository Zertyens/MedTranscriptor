"""
Prueba de la cadena completa: TU VOZ -> texto -> eventos -> base de datos.

Es el eslabon que faltaba probar. El resto del pipeline (texto -> eventos ->
proyeccion -> CSV) ya esta verificado en demo.py.

COMO GRABAR (sin instalar nada)
  1. Tecla Windows, escribi "Grabadora de voz", abrila.
  2. Grabá la nota como se la dictarias a un colega al pasar visita.
  3. Los archivos quedan en Documentos\\Grabaciones de sonido (o Sound recordings).

COMO PROBAR
  python probar_voz.py "C:\\ruta\\a\\tu\\grabacion.m4a"
  python probar_voz.py grabacion.m4a --guardar     tambien inserta en la base

QUE DICTAR PARA QUE LA PRUEBA SIRVA
  Mencioná dispositivos con fecha y hora, valores, y algo vago a proposito.
  Por ejemplo:

    "Hoy a las diez de la manana le coloque una via central subclavia derecha.
     Temperatura 38 y medio, frecuencia cardiaca 110, Glasgow 14.
     Ayer le sacamos la sonda vesical. Despues vemos lo otro."

  Deberia salir: un dispositivo_inicio de CVC, un fisiologico_24h con tres
  mediciones, un dispositivo_fin de SV con la fecha de ayer, y "Despues vemos
  lo otro" en no_entendido.
"""
from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

AUDIO_DIR = Path(__file__).parent / "demo" / "audio"

VERDE, ROJO, AMARILLO, GRIS, NEGRITA, FIN = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[1m", "\033[0m"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    origen = Path(sys.argv[1])
    guardar = "--guardar" in sys.argv
    if not origen.exists():
        print(f"{ROJO}No existe: {origen}{FIN}")
        sys.exit(1)

    # El audio es el respaldo de lo que se registro: se guarda siempre.
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    destino = AUDIO_DIR / f"nota_{date.today().isoformat()}_{origen.name}"
    if origen.resolve() != destino.resolve():
        shutil.copy2(origen, destino)
    print(f"{GRIS}audio guardado en {destino}{FIN}\n")

    print(f"{NEGRITA}1. TRANSCRIPCION (Whisper local, sin internet){FIN}")
    import voz

    try:
        texto, segundos = voz.transcribir(destino)
    except voz.ErrorVoz as e:
        print(f"{ROJO}{e}{FIN}")
        sys.exit(1)
    print(f"  {segundos}s\n")
    print(f"  {NEGRITA}{texto}{FIN}\n")

    if not texto.strip():
        print(f"{ROJO}Whisper no saco nada. Revisa que el audio tenga voz audible.{FIN}")
        sys.exit(1)

    print(f"{NEGRITA}2. GEMMA TRADUCE A EVENTOS{FIN}")
    import db
    import gemma
    import semilla

    episodio = semilla.crear_episodio()
    hoy = date.today().isoformat()

    resultado = gemma.traducir_nota(
        episodio_id=episodio.id,
        nota=texto,
        fecha_referencia=hoy,
        autor="dictado en vivo",
        fuente="audio_gemma",
    )
    print(f"  {resultado.segundos}s -> {len(resultado.eventos)} eventos\n")

    for e in resultado.eventos:
        p = e.payload_json
        detalle = p.get("dispositivo") or p.get("codigo") or ""
        if "mediciones" in p:
            detalle = ", ".join(f"{k}={v}" for k, v in p["mediciones"].items())
        color = AMARILLO if e.confianza < 0.75 else VERDE
        print(f"  {e.tipo_evento:<19} {e.timestamp_clinico[:16]}  {detalle} {p.get('instancia_id', '')}")
        print(f"      {GRIS}confianza {color}{e.confianza}{FIN}{GRIS} | cita: {e.texto_crudo[:60]!r}{FIN}")

    if resultado.no_entendido:
        print(f"\n  {AMARILLO}Esto no lo entendio (y esta bien que lo diga):{FIN}")
        for frag in resultado.no_entendido:
            print(f"      {frag[:80]!r}")

    if resultado.rechazados:
        print(f"\n  {ROJO}Rechazados por el validador:{FIN}")
        for r in resultado.rechazados:
            print(f"      {r['motivo'][:90]}")

    revision = resultado.requieren_revision
    if revision:
        print(f"\n  {AMARILLO}{len(revision)} de {len(resultado.eventos)} eventos van a revision humana "
              f"(confianza < 0.75).{FIN}")

    if guardar:
        print(f"\n{NEGRITA}3. GUARDADO EN LA BASE{FIN}")
        con = db.conectar(Path(__file__).parent / "medtranscriptor.db")
        if db.get_episodio(con, episodio.id) is None:
            db.insert_episodio(con, episodio)
        for e in resultado.eventos:
            db.insert_evento(con, e)
        guardados = db.get_eventos(con, episodio.id)
        print(f"  {VERDE}{len(resultado.eventos)} eventos insertados. "
              f"El episodio tiene {len(guardados)} en total.{FIN}")
        con.close()
    else:
        print(f"\n  {GRIS}(no se guardo nada: agrega --guardar para insertarlo en la base){FIN}")

    print()


if __name__ == "__main__":
    main()
