"""Painel no navegador para configurar e rodar a busca sem usar a linha de comando.

Sobe um servidor HTTP local (só 127.0.0.1) que entrega a página `web/painel.html`
e uma pequena API JSON usada por ela.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .executor import AREAS_PADRAO, ErroConfiguracao, carregar_config, executar, salvar_config

log = logging.getLogger("aneel_busca")

PAGINA = Path(__file__).parent / "web" / "painel.html"
LIMITE_LOG = 3000
LIMITE_OCORRENCIAS = 5000
EXTENSOES_ENVIO = (".pdf", ".docx", ".xlsx", ".htm", ".html", ".txt", ".rtf", ".zip")


class _CapturaLog(logging.Handler):
    def __init__(self, estado: "Estado"):
        super().__init__(level=logging.INFO)
        self.estado = estado
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, registro: logging.LogRecord) -> None:
        try:
            self.estado.adicionar_log(registro.levelname, self.format(registro))
        except Exception:  # noqa: BLE001
            self.handleError(registro)


class Estado:
    """Tudo que o painel precisa saber sobre a busca em andamento e a última concluída."""

    def __init__(self, caminho_config: Path, saida: Path):
        self.caminho_config = caminho_config
        self.saida = saida
        self.pasta_envios = saida / "arquivos_enviados"
        self.trava = threading.Lock()
        self.logs: list[dict] = []
        self.seq_log = 0
        self.thread: threading.Thread | None = None
        self.cancelar = threading.Event()
        self.varredura = None
        self.inicio: float | None = None
        self.ultima: dict | None = None  # resumo da última execução
        self.ultimas_ocorrencias: list[dict] = []
        self.ultimos_documentos: list[dict] = []

    # ------------------------------------------------------------------ logs
    def adicionar_log(self, nivel: str, texto: str) -> None:
        with self.trava:
            self.seq_log += 1
            self.logs.append({"seq": self.seq_log, "nivel": nivel, "texto": texto})
            if len(self.logs) > LIMITE_LOG:
                del self.logs[: len(self.logs) - LIMITE_LOG]

    def logs_desde(self, seq: int) -> list[dict]:
        with self.trava:
            return [l for l in self.logs if l["seq"] > seq]

    # ------------------------------------------------------------------ execução
    @property
    def rodando(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def iniciar(self, pedido: dict) -> None:
        if self.rodando:
            raise ErroConfiguracao("Já existe uma busca em andamento.")
        config = carregar_config(self.caminho_config)
        comando = pedido.get("comando", "online")
        pasta = None
        if comando == "local":
            if pedido.get("usar_enviados"):
                pasta = self.pasta_envios
            else:
                texto = str(pedido.get("pasta") or "").strip().strip('"')
                if not texto:
                    raise ErroConfiguracao("Informe a pasta com os arquivos ou envie arquivos pelo painel.")
                pasta = Path(texto)
            if not pasta.is_dir() or not any(pasta.iterdir()):
                raise ErroConfiguracao(f"Pasta vazia ou não encontrada: {pasta}")
        opcoes = dict(
            comando=comando, saida=self.saida, pasta=pasta,
            areas=[int(a) for a in pedido.get("areas") or []] or None,
            desde=pedido.get("desde") or None, ate=pedido.get("ate") or None,
            max_paginas=int(pedido["max_paginas"]) if pedido.get("max_paginas") else None,
            modo=pedido.get("modo") or "auto", sem_cache=bool(pedido.get("sem_cache")),
            salvar_textos=bool(pedido.get("salvar_textos")),
        )
        if comando == "online" and not opcoes["areas"] and not config.get("areas"):
            opcoes["areas"] = list(AREAS_PADRAO)
        self.cancelar.clear()
        self.varredura = None
        self.inicio = time.time()
        self.ultima = None
        self.thread = threading.Thread(target=self._rodar, args=(config, opcoes), daemon=True, name="busca")
        self.thread.start()

    def _rodar(self, config: dict, opcoes: dict) -> None:
        log.info("Iniciando busca (%s)…", "site da ANEEL" if opcoes["comando"] == "online" else "arquivos locais")

        def guardar(varredura):
            self.varredura = varredura

        try:
            execucao = executar(config, cancelar=self.cancelar, ao_criar_varredura=guardar, **opcoes)
        except ErroConfiguracao as erro:
            log.error("%s", erro)
            self.ultima = {"situacao": "erro", "mensagem": str(erro), "arquivo": None,
                           "fim": datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
            return
        except Exception as erro:  # noqa: BLE001
            log.exception("Erro inesperado na busca: %s", erro)
            self.ultima = {"situacao": "erro", "mensagem": str(erro), "arquivo": None,
                           "fim": datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
            return
        resultado = execucao.resultado
        self.ultimas_ocorrencias = [asdict(o) for o in resultado.ocorrencias[:LIMITE_OCORRENCIAS]]
        self.ultimos_documentos = [asdict(d) for d in resultado.documentos]
        self.ultima = {
            "situacao": execucao.situacao,
            "mensagem": execucao.mensagem,
            "arquivo": execucao.xlsx.name if execucao.xlsx else None,
            "documentos": len(resultado.documentos),
            "reunioes": len({d.reuniao for d in resultado.documentos}),
            "erros": sum(1 for d in resultado.documentos if d.situacao != "OK"),
            "ocorrencias": len(resultado.ocorrencias),
            "fim": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        }
        log.info("Busca finalizada: %d documento(s), %d ocorrência(s).",
                 len(resultado.documentos), len(resultado.ocorrencias))

    def status(self, seq: int) -> dict:
        andamento = {}
        if self.varredura is not None:
            r = self.varredura.resultado
            andamento = {"documentos": len(r.documentos), "ocorrencias": len(r.ocorrencias),
                         "reunioes": len({d.reuniao for d in r.documentos})}
        return {
            "rodando": self.rodando,
            "cancelando": self.rodando and self.cancelar.is_set(),
            "segundos": int(time.time() - self.inicio) if self.inicio and self.rodando else None,
            "andamento": andamento,
            "ultima": self.ultima,
            "logs": self.logs_desde(seq),
        }

    # ------------------------------------------------------------------ arquivos
    def relatorios(self) -> list[dict]:
        if not self.saida.is_dir():
            return []
        arquivos = sorted(self.saida.glob("busca_aneel_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"nome": p.name, "tamanho": p.stat().st_size,
                 "data": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M")}
                for p in arquivos[:30]]

    def arquivos_enviados(self) -> list[str]:
        if not self.pasta_envios.is_dir():
            return []
        return sorted(p.name for p in self.pasta_envios.iterdir() if p.is_file())


def _config_para_tela(config: dict) -> dict:
    empresas = config.get("empresas") or {}
    ignoradas = set(config.get("empresas_ignoradas") or [])
    areas = {int(k): v for k, v in (config.get("areas") or AREAS_PADRAO).items()}
    return {
        "processos": [str(p) for p in config.get("processos") or []],
        "palavras_chave": [str(p) for p in config.get("palavras_chave") or []],
        "empresas": [{"nome": nome, "nomes": (d or {}).get("nomes") or [], "siglas": (d or {}).get("siglas") or [],
                      "ativa": nome not in ignoradas} for nome, d in empresas.items()],
        "areas": [{"id": k, "nome": v} for k, v in areas.items()],
        "desde": str(config.get("desde") or ""),
        "ate": str(config.get("ate") or ""),
        "max_paginas": config.get("max_paginas") or 10,
    }


def _limpar_lista(valores) -> list[str]:
    vistos: dict[str, None] = {}
    for v in valores or []:
        v = str(v).strip()
        if v:
            vistos.setdefault(v, None)
    return list(vistos)


def _aplicar_tela_na_config(config: dict, dados: dict) -> dict:
    config = dict(config)
    config["processos"] = _limpar_lista(dados.get("processos"))
    config["palavras_chave"] = _limpar_lista(dados.get("palavras_chave"))
    if "empresas" in dados:
        empresas, ignoradas = {}, []
        for e in dados["empresas"]:
            nome = str(e.get("nome") or "").strip()
            if not nome:
                continue
            empresas[nome] = {"nomes": _limpar_lista(e.get("nomes")), "siglas": _limpar_lista(e.get("siglas"))}
            if not e.get("ativa", True):
                ignoradas.append(nome)
        config["empresas"] = empresas
        config["empresas_ignoradas"] = ignoradas
    for chave in ("desde", "ate"):
        if chave in dados:
            config[chave] = str(dados[chave] or "")
    if dados.get("max_paginas"):
        config["max_paginas"] = int(dados["max_paginas"])
    return config


def _criar_handler(estado: Estado, servidor_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PainelANEEL/1.0"

        def log_message(self, *_):  # silencia o log de cada requisição no terminal
            pass

        # ---------------------------------------------------------- utilidades
        def _host_local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _json(self, dados, status=HTTPStatus.OK) -> None:
            corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _erro(self, mensagem: str, status=HTTPStatus.BAD_REQUEST) -> None:
            self._json({"erro": mensagem}, status)

        def _corpo(self) -> bytes:
            tamanho = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(tamanho) if tamanho else b""

        # ---------------------------------------------------------- GET
        def do_GET(self):  # noqa: N802
            if not self._host_local():
                return self._erro("Acesso permitido apenas pelo próprio computador", HTTPStatus.FORBIDDEN)
            url = urlparse(self.path)
            consulta = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                corpo = PAGINA.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(corpo)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(corpo)
            elif url.path == "/api/config":
                try:
                    self._json(_config_para_tela(carregar_config(estado.caminho_config)))
                except ErroConfiguracao as erro:
                    self._erro(str(erro))
            elif url.path == "/api/status":
                seq = int((consulta.get("desde_log") or ["0"])[0] or 0)
                self._json(estado.status(seq))
            elif url.path == "/api/resultados":
                self._json({"ocorrencias": estado.ultimas_ocorrencias, "documentos": estado.ultimos_documentos})
            elif url.path == "/api/relatorios":
                self._json({"relatorios": estado.relatorios(), "enviados": estado.arquivos_enviados()})
            elif url.path == "/baixar":
                self._baixar(unquote((consulta.get("arquivo") or [""])[0]))
            else:
                self._erro("Não encontrado", HTTPStatus.NOT_FOUND)

        def _baixar(self, nome: str) -> None:
            if not re.fullmatch(r"[\w\-. ]+\.(xlsx|csv)", nome or ""):
                return self._erro("Arquivo inválido")
            caminho = (estado.saida / nome).resolve()
            if caminho.parent != estado.saida.resolve() or not caminho.is_file():
                return self._erro("Arquivo não encontrado", HTTPStatus.NOT_FOUND)
            dados = caminho.read_bytes()
            tipo = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    if nome.endswith(".xlsx") else "text/csv; charset=utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Disposition", f'attachment; filename="{nome}"')
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        # ---------------------------------------------------------- POST
        def do_POST(self):  # noqa: N802
            # O cabeçalho próprio impede que outro site dispare ações no painel (CSRF).
            if not self._host_local() or self.headers.get("X-Painel") != "1":
                return self._erro("Requisição não autorizada", HTTPStatus.FORBIDDEN)
            url = urlparse(self.path)
            try:
                if url.path == "/api/enviar":
                    return self._receber_arquivo(parse_qs(url.query))
                dados = json.loads(self._corpo() or b"{}")
                if url.path == "/api/config":
                    if estado.rodando:
                        return self._erro("Aguarde a busca terminar para alterar a configuração.")
                    nova = _aplicar_tela_na_config(carregar_config(estado.caminho_config), dados)
                    salvar_config(estado.caminho_config, nova)
                    log.info("Configuração salva.")
                    self._json({"ok": True, "config": _config_para_tela(nova)})
                elif url.path == "/api/iniciar":
                    estado.iniciar(dados)
                    self._json({"ok": True})
                elif url.path == "/api/parar":
                    if estado.rodando:
                        estado.cancelar.set()
                        log.warning("Parando a busca… (termina o documento atual e gera o relatório parcial)")
                    self._json({"ok": True})
                elif url.path == "/api/limpar_enviados":
                    if estado.rodando:
                        return self._erro("Aguarde a busca terminar.")
                    for p in estado.arquivos_enviados():
                        (estado.pasta_envios / p).unlink(missing_ok=True)
                    self._json({"ok": True})
                elif url.path == "/api/encerrar":
                    self._json({"ok": True})
                    estado.cancelar.set()
                    threading.Thread(target=servidor_ref["servidor"].shutdown, daemon=True).start()
                else:
                    self._erro("Não encontrado", HTTPStatus.NOT_FOUND)
            except (ErroConfiguracao, ValueError) as erro:
                self._erro(str(erro))

        def _receber_arquivo(self, consulta: dict) -> None:
            nome = Path(unquote((consulta.get("nome") or [""])[0])).name
            nome = re.sub(r"[^\w\-. ()]+", "_", nome).strip()
            if not nome or not nome.lower().endswith(EXTENSOES_ENVIO):
                return self._erro(f"Tipo de arquivo não suportado: {nome}")
            estado.pasta_envios.mkdir(parents=True, exist_ok=True)
            (estado.pasta_envios / nome).write_bytes(self._corpo())
            self._json({"ok": True, "nome": nome})

    return Handler


def criar_servidor(caminho_config: Path, saida: Path, porta: int = 8765) -> tuple[ThreadingHTTPServer, Estado]:
    estado = Estado(caminho_config, saida)
    referencia: dict = {}
    handler = _criar_handler(estado, referencia)
    ultimo_erro = None
    for tentativa in range(porta, porta + 20):  # se a porta estiver ocupada, tenta as seguintes
        try:
            servidor = ThreadingHTTPServer(("127.0.0.1", tentativa), handler)
            break
        except OSError as erro:
            ultimo_erro = erro
    else:
        raise OSError(f"Nenhuma porta livre entre {porta} e {porta + 19}: {ultimo_erro}")
    servidor.daemon_threads = True
    referencia["servidor"] = servidor
    captura = _CapturaLog(estado)
    logging.getLogger("aneel_busca").addHandler(captura)
    logging.getLogger("aneel_busca").setLevel(logging.INFO)
    return servidor, estado


def iniciar_painel(caminho_config: Path, saida: Path, *, porta: int = 8765, abrir_navegador: bool = True) -> int:
    if not caminho_config.exists():
        print(f"Arquivo de configuração não encontrado: {caminho_config}")
        return 1
    servidor, _ = criar_servidor(caminho_config, saida, porta)
    endereco = f"http://127.0.0.1:{servidor.server_address[1]}/"
    print("=" * 64)
    print("  Painel da busca nas Reuniões Públicas da ANEEL")
    print(f"  Aberto em: {endereco}")
    print("  Para encerrar: feche esta janela ou use o botão 'Encerrar' no painel.")
    print("=" * 64)
    if abrir_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(endereco)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
    print("Painel encerrado.")
    return 0
