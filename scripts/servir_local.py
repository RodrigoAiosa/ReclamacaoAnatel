"""Servidor local para ver o dashboard sem a Vercel CLI: serve public/ e a rota /api/dados (mesmo código da função serverless).

    python scripts/servir_local.py [porta]      # padrão 8765 -> http://localhost:8765  (versão celular: /m.html)
"""
import importlib.util, os, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
porta = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
spec = importlib.util.spec_from_file_location("dados", os.path.join(RAIZ, "api", "dados.py"))
dados = importlib.util.module_from_spec(spec); spec.loader.exec_module(dados)


class Servidor(dados.handler, SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/dados"):
            return dados.handler.do_GET(self)
        SimpleHTTPRequestHandler.do_GET(self)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    os.chdir(os.path.join(RAIZ, "public"))
    print(f"Dashboard em http://localhost:{porta}  (Ctrl+C para sair)")
    ThreadingHTTPServer(("127.0.0.1", porta), Servidor).serve_forever()
