"""
Demo end-to-end por consola: notas dictadas -> Gemma -> eventos -> proyeccion
-> validacion -> CSV, con el panel de trazabilidad.

    python demo.py            corre contra Gemma y guarda el resultado en cache
    python demo.py --cache    reproduce desde el cache, sin red (instantaneo)
    python demo.py --resumen  ademas genera el resumen para el paciente

Por que hay cache: cada nota tarda ~13s contra la API. Siete notas son casi
dos minutos de pantalla quieta, y encima la demo en vivo dependeria de que
haya internet. Se corre una vez, se guarda, y la demo reproduce desde disco.
El cache guarda los eventos que Gemma ya devolvio: no cambia ningun numero,
solo evita volver a preguntar.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import db
import semilla
import validador
from exportador import exportar_csv
from modelos import Evento
from proyector import advertencias_fila, fila_a_valores, proyectar_fila

CACHE = Path(__file__).parent / "demo" / "eventos_cache.json"
DB = Path(__file__).parent / "medtranscriptor.db"

VERDE, ROJO, AMARILLO, GRIS, NEGRITA, FIN = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[1m", "\033[0m"


def titulo(texto: str) -> None:
    print(f"\n{NEGRITA}{'=' * 78}\n{texto}\n{'=' * 78}{FIN}")


def cargar_eventos_desde_gemma(episodio_id: str) -> list[Evento]:
    """Manda cada nota a Gemma y devuelve los eventos validados."""
    import gemma

    eventos: list[Evento] = []
    for i, nota in enumerate(semilla.NOTAS, start=1):
        print(f"  [{i}/{len(semilla.NOTAS)}] {nota['fecha_referencia']} ({nota['autor']})... ", end="", flush=True)
        # Cada nota se procesa con el estado que dejaron las anteriores: que
        # dispositivos siguen colocados y con que instancia_id.
        resultado = gemma.traducir_nota(
            episodio_id=episodio_id,
            nota=nota["texto"],
            fecha_referencia=nota["fecha_referencia"],
            autor=nota["autor"],
            fuente="texto_gemma",
            abiertas=gemma.instancias_abiertas(eventos),
        )
        print(f"{resultado.segundos}s -> {len(resultado.eventos)} eventos", end="")
        if resultado.rechazados:
            print(f" {ROJO}({len(resultado.rechazados)} rechazados){FIN}", end="")
        if resultado.no_entendido:
            print(f" {AMARILLO}({len(resultado.no_entendido)} no entendido){FIN}", end="")
        print()
        for r in resultado.rechazados:
            print(f"        {ROJO}rechazado:{FIN} {r['motivo'][:90]}")
        for frag in resultado.no_entendido:
            print(f"        {AMARILLO}no entendido:{FIN} {frag[:90]!r}")
        eventos.extend(resultado.eventos)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps([e.to_dict() for e in eventos], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  {GRIS}cache guardado en {CACHE.name}{FIN}")
    return eventos


def cargar_eventos_desde_cache(episodio_id: str) -> list[Evento]:
    if not CACHE.exists():
        print(f"{ROJO}No hay cache todavia. Corre 'python demo.py' una vez sin --cache.{FIN}")
        sys.exit(1)
    eventos = []
    for d in json.loads(CACHE.read_text(encoding="utf-8")):
        d["episodio_id"] = episodio_id
        eventos.append(Evento.from_dict(d))
    print(f"  {len(eventos)} eventos leidos del cache (sin llamar a Gemma)")
    return eventos


def main() -> None:
    usar_cache = "--cache" in sys.argv
    con_resumen = "--resumen" in sys.argv

    if DB.exists():
        DB.unlink()
    con = db.conectar(DB)

    episodio = semilla.crear_episodio()
    db.insert_episodio(con, episodio)

    titulo("1. NOTAS DICTADAS  ->  EVENTOS  (Gemma traduce)")
    eventos = cargar_eventos_desde_cache(episodio.id) if usar_cache else cargar_eventos_desde_gemma(episodio.id)
    for evento in eventos:
        db.insert_evento(con, evento)

    titulo("2. LIBRO DE MOVIMIENTOS")
    for e in sorted(eventos, key=lambda x: x.timestamp_clinico):
        p = e.payload_json
        detalle = p.get("dispositivo") or p.get("codigo") or ""
        if "mediciones" in p:
            detalle = ", ".join(f"{k}={v}" for k, v in list(p["mediciones"].items())[:4])
        instancia = p.get("instancia_id", "")
        color = AMARILLO if e.confianza < 0.75 else ""
        print(f"  {e.timestamp_clinico[:16]}  {e.tipo_evento:<19} {detalle} {instancia}")
        print(f"        {GRIS}{e.autor} | conf {color}{e.confianza}{FIN}{GRIS} | {e.texto_crudo[:62]!r}{FIN}")

    revision = [e for e in eventos if e.confianza < 0.75]
    if revision:
        print(f"\n  {AMARILLO}{len(revision)} eventos con confianza < 0.75 -> van a revision humana, "
              f"no se autocompletan en silencio.{FIN}")

    titulo("3. FILA SATI-Q PROYECTADA  (Python calcula, nadie carga estos numeros)")
    proyeccion = proyectar_fila(episodio, db.get_eventos(con, episodio.id))
    valores = fila_a_valores(proyeccion)
    interesantes = [
        "ESTADIA", "SCORE", "PROBABMORT", "VI", "DIASVI", "CAFO", "DIASCAFO",
        "CVC", "DIASCVC", "SE", "DIASSE", "SV", "DIASSV", "SNG", "DIASSNG",
        "NEUMONIA", "NEUMONIANUM", "ESCARAS", "ESCARASNUM",
        "TISSMIN", "TISSMAX", "TISSPROMEDIO", "RESULTADO",
    ]
    for nombre in interesantes:
        campo = proyeccion[nombre]
        marca = f"{GRIS}({len(campo.evento_ids)} eventos){FIN}" if campo.evento_ids else ""
        print(f"  {nombre:<14} = {str(campo.valor):<10} {marca}")

    titulo("4. TRAZABILIDAD  (el jurado clickea un numero y ve de donde salio)")
    for objetivo in ("DIASCVC", "SCORE"):
        campo = proyeccion[objetivo]
        print(f"\n  {NEGRITA}{objetivo} = {campo.valor}{FIN}  sale de:")
        if campo.detalle and "componentes" in campo.detalle:
            for c in campo.detalle["componentes"]:
                if c["puntos"] == 0 and c["faltante"]:
                    continue
                nota = f"  {GRIS}({c['nota']}){FIN}" if c["nota"] else ""
                print(f"      {c['etiqueta']:<24} {str(c['valor'] or ''):>8}  ->  {c['puntos']} pts{nota}")
        else:
            por_id = {e.id: e for e in eventos}
            for eid in campo.evento_ids:
                e = por_id.get(eid)
                if e:
                    print(f"      {e.timestamp_clinico[:16]}  {e.tipo_evento:<19} {e.autor}")
                    print(f"          {GRIS}{e.texto_crudo[:66]!r}{FIN}")

    titulo("5. VALIDACION DE LAS 49 REGLAS")
    hallazgos = validador.validar_fila(valores, advertencias_fila(proyeccion))
    errores = validador.errores(hallazgos)
    advertencias = validador.advertencias(hallazgos)
    print(f"  errores: {ROJO if errores else VERDE}{len(errores)}{FIN}   advertencias: {len(advertencias)}")
    for h in errores:
        print(f"    {ROJO}{h}{FIN}")
    for h in advertencias:
        print(f"    {AMARILLO}{str(h)[:110]}{FIN}")

    titulo("6. CSV PARA SATI-Q")
    if errores:
        print(f"  {ROJO}No se exporta: hay errores de validacion.{FIN}")
    else:
        destino = Path(__file__).parent / "demo" / "salida_satiq.csv"
        destino.parent.mkdir(parents=True, exist_ok=True)
        contenido = exportar_csv([valores], destino=destino)
        for linea in contenido.strip().split("\r\n"):
            print(f"  {linea}")
        print(f"\n  {VERDE}Escrito en {destino}{FIN}")

    if con_resumen:
        titulo("7. RESUMEN PARA EL PACIENTE Y SU FAMILIA  (Gemma explica)")
        import gemma
        texto = gemma.explicar_egreso(
            f"Hombre de {episodio.edad} anios. Estuvo {valores['ESTADIA']} dias en terapia intensiva.\n"
            f"Dias con respirador: {valores['DIASVI']}. Vias centrales: {valores['DIASCVC']} dias en total.\n"
            f"Tuvo una neumonia asociada al respirador: {'si' if valores['NEUMONIA'] else 'no'}.\n"
            f"Tuvo escaras: {'si' if valores['ESCARAS'] else 'no'}.\n"
            f"Al egreso pasa a sala de internacion general."
        )
        for parrafo in texto.split("\n"):
            print(f"  {parrafo}")

    con.close()
    print()


if __name__ == "__main__":
    main()
