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
CACHE_VIHDA = Path(__file__).parent / "demo" / "vihda_cache.json"
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
            eventos_previos=eventos,
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
        for aviso in resultado.correcciones:
            print(f"        {VERDE}correccion:{FIN} {aviso}")
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


def verificar_infecciones(eventos: list[Evento], usar_cache: bool) -> list[dict]:
    """Para cada infeccion declarada por el medico, revisa si el registro
    documenta lo que VIHDA exige.

    Ojo con el orden de las cosas: el medico YA declaro la infeccion antes de
    que esto corra. Gemma no decide si hay infeccion, revisa si el registro la
    sostiene. Si falta documentacion, SATI-Q puede rechazar el caso.

    No se guarda en el evento: la completitud del registro cambia a medida que
    llegan notas, asi que se recalcula igual que el resto de la proyeccion."""
    import json as _json

    criterios = _json.loads(
        (Path(__file__).parent / "schema" / "vihda_criterios.json").read_text(encoding="utf-8")
    )
    declaradas = [
        e for e in eventos
        if e.tipo_evento == "evento_adverso"
        and criterios.get(e.payload_json.get("codigo"), {}).get("_meta") is None
        and e.payload_json.get("codigo") in criterios
    ]
    if not declaradas:
        return []

    if usar_cache and CACHE_VIHDA.exists():
        return _json.loads(CACHE_VIHDA.read_text(encoding="utf-8"))

    import gemma

    # El registro clinico son las notas dictadas. NOTA: hoy el sistema guarda
    # el fragmento citado (texto_crudo) pero no la nota completa. Para produccion
    # habria que persistir la nota entera; aca se usan las de semilla.py.
    registro = "\n\n".join(f"[{n['fecha_referencia']}] {n['texto']}" for n in semilla.NOTAS)

    resultados = []
    for evento in declaradas:
        codigo = evento.payload_json["codigo"]
        print(f"  verificando {codigo} contra criterios VIHDA... ", end="", flush=True)
        v = gemma.verificar_vihda(codigo, registro)
        print(f"{len(v['cumplidos'])} documentados, {len(v['faltantes'])} faltantes")
        resultados.append({
            "codigo": codigo,
            "nombre": criterios[codigo]["nombre"],
            "declarado_por": evento.autor,
            "cita_declaracion": evento.texto_crudo,
            **v,
        })

    CACHE_VIHDA.parent.mkdir(parents=True, exist_ok=True)
    CACHE_VIHDA.write_text(_json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    return resultados


def main() -> None:
    usar_cache = "--cache" in sys.argv
    con_resumen = "--resumen" in sys.argv

    # La demo arranca de cero cada vez. Si la app Streamlit esta abierta tiene
    # el archivo tomado y Windows no deja borrarlo: se avisa en castellano en
    # vez de escupir un traceback.
    if DB.exists():
        try:
            DB.unlink()
        except PermissionError:
            print(f"{ROJO}No puedo borrar {DB.name}: hay otro proceso usandolo.{FIN}")
            print("  Cerra la app (la ventana de MedTranscriptor) y volve a correr esto.")
            sys.exit(1)
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

    titulo("5. VERIFICACION VIHDA  (el medico declara, Gemma revisa el registro)")
    verificaciones = verificar_infecciones(eventos, usar_cache)
    if not verificaciones:
        print("  (no hay infecciones declaradas en este episodio)")
    for v in verificaciones:
        print(f"\n  {NEGRITA}{v['nombre']}{FIN}")
        print(f"  {GRIS}declarada por {v['declarado_por']}: {v['cita_declaracion'][:66]!r}{FIN}\n")
        for c in v["cumplidos"]:
            print(f"    {VERDE}[documentado]{FIN} {c['id']}")
            print(f"        {GRIS}{c.get('evidencia', '')[:70]!r}{FIN}")
        for c in v["faltantes"]:
            print(f"    {ROJO}[FALTA]{FIN}       {c['id']}")
            print(f"        {GRIS}{c.get('que_falta', '')[:70]}{FIN}")
        if v["faltantes"]:
            print(f"\n    {AMARILLO}Con documentacion incompleta, SATI-Q puede rechazar este caso.{FIN}")

    titulo("6. VALIDACION DE LAS 49 REGLAS")
    hallazgos = validador.validar_fila(valores, advertencias_fila(proyeccion))
    errores = validador.errores(hallazgos)
    advertencias = validador.advertencias(hallazgos)
    print(f"  errores: {ROJO if errores else VERDE}{len(errores)}{FIN}   advertencias: {len(advertencias)}")
    for h in errores:
        print(f"    {ROJO}{h}{FIN}")
    for h in advertencias:
        print(f"    {AMARILLO}{str(h)[:110]}{FIN}")

    titulo("7. CSV PARA SATI-Q")
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
        titulo("8. RESUMEN PARA EL PACIENTE Y SU FAMILIA  (Gemma explica)")
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
