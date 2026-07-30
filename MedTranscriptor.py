"""
Lanzador de MedTranscriptor: abre la app en una ventana de escritorio.

    python MedTranscriptor.py

Levanta el servidor de Streamlit en un puerto libre y abre una ventana nativa
apuntada ahi. No hay que acordarse de ninguna URL ni de ningun puerto.

Si pywebview no esta instalado (pip install pywebview), cae a abrir el
navegador por defecto. La app es la misma en los dos casos.

SOBRE EL MICROFONO: la ventana nativa usa WebView2 (el motor de Edge). El
permiso de microfono puede comportarse distinto que en un navegador comun.
Si la grabacion no anda en la ventana, arranca con:

    python MedTranscriptor.py --navegador

que abre exactamente lo mismo en Chrome/Edge, donde el permiso funciona
seguro. Es el plan B de la demo.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DIRECTORIO = Path(__file__).parent
APP = DIRECTORIO / "app.py"
TITULO = "MedTranscriptor - Registro UCI SATI-Q"


def puerto_libre() -> int:
    """Pide un puerto al sistema en vez de asumir el 8501: si quedo una
    instancia colgada de una corrida anterior, no chocamos con ella."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def arrancar_streamlit(puerto: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", str(APP),
            "--server.port", str(puerto),
            "--server.headless", "true",       # no abre navegador por su cuenta
            "--browser.gatherUsageStats", "false",
            "--server.fileWatcherType", "none",  # menos CPU en una maquina justa
        ],
        cwd=str(DIRECTORIO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def esperar_servidor(puerto: int, proceso: subprocess.Popen, timeout: int = 60) -> bool:
    """Espera a que el servidor conteste. Si el proceso muere antes, muestra
    su salida: sin esto, un error de import queda invisible."""
    url = f"http://localhost:{puerto}"
    limite = time.time() + timeout
    while time.time() < limite:
        if proceso.poll() is not None:
            print("\nStreamlit se cerro solo. Esto es lo que dijo:\n")
            print(proceso.stdout.read() if proceso.stdout else "(sin salida)")
            return False
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    print(f"\nEl servidor no respondio en {timeout}s.")
    return False


def main() -> None:
    if not APP.exists():
        print(f"No encuentro {APP}")
        sys.exit(1)

    forzar_navegador = "--navegador" in sys.argv
    puerto = puerto_libre()
    url = f"http://localhost:{puerto}"

    print("MedTranscriptor")
    print(f"  arrancando servidor en {url} ...")
    proceso = arrancar_streamlit(puerto)

    try:
        if not esperar_servidor(puerto, proceso):
            sys.exit(1)
        print("  listo.\n")

        if not forzar_navegador:
            try:
                import webview

                webview.create_window(TITULO, url, width=1400, height=900)
                # El servidor queda corriendo mientras la ventana este abierta.
                webview.start()
                return
            except ImportError:
                print("  pywebview no esta instalado, abro el navegador.")
                print("  (para la ventana nativa: pip install pywebview)\n")
            except Exception as e:
                print(f"  no se pudo abrir la ventana nativa ({e}), abro el navegador.\n")

        import webbrowser

        webbrowser.open(url)
        print(f"  Abierto en el navegador: {url}")
        print("  Dejá esta consola abierta. Ctrl+C para cerrar.\n")
        proceso.wait()

    except KeyboardInterrupt:
        print("\n  cerrando...")
    finally:
        proceso.terminate()
        try:
            proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proceso.kill()


if __name__ == "__main__":
    main()
