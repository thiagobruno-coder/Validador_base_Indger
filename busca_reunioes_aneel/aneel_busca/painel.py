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

from .coleta import Coleta
from .executor import AREAS_PADRAO, ErroConfiguracao, Execucao, carregar_config, executar, salvar_config

log = logging.getLogger("aneel_busca")

PAGINA = Path(__file__).parent / "web" / "painel.html"
COLETOR = Path(__file__).parent / "web" / "coletor.js"
# Só a página da ANEEL (aberta no Chrome do usuário) pode enviar conteúdo para a coleta.
ORIGENS_COLETA = {"https://www2.aneel.gov.br", "http://www2.aneel.gov.br"}
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

    def __init__(self, caminho_config: Path, saida: Path, origens_coleta=()):
        self.caminho_config = caminho_config
        self.origens_coleta = ORIGENS_COLETA | set(origens_coleta)
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
        self.coleta: Coleta | None = None
        self.pedido_coleta: dict = {}

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
    def coletando(self) -> bool:
        return self.coleta is not None and self.coleta.ativa

    @property
    def rodando(self) -> bool:
        return (self.thread is not None and self.thread.is_alive()) or self.coletando

    # ------------------------------------------------------------------ coleta pelo navegador
    def iniciar_coleta(self) -> dict:
        if self.thread is not None and self.thread.is_alive():
            raise ErroConfiguracao("Já existe uma busca em andamento no painel.")
        if self.coletando:
            if time.time() - self.coleta.ultima_atividade < 60:
                raise ErroConfiguracao("Já existe uma coleta em andamento (em outra aba?).")
            self.finalizar_coleta()  # coleta abandonada: salva o que tinha
        self.coleta = Coleta(carregar_config(self.caminho_config), self.pedido_coleta)
        self.inicio = time.time()
        self.ultima = None
        self.varredura = self.coleta.varredura
        log.info("Coleta pelo navegador iniciada (%s até %s).",
                 self.coleta.desde.strftime("%d/%m/%Y") if self.coleta.desde else "início",
                 self.coleta.ate.strftime("%d/%m/%Y") if self.coleta.ate else "hoje")
        return self.coleta.plano()

    def coleta_ativa(self) -> Coleta:
        if not self.coletando:
            raise ErroConfiguracao("Nenhuma coleta em andamento. Clique no favorito de novo.")
        return self.coleta

    def finalizar_coleta(self) -> dict:
        coleta = self.coleta_ativa()
        self._registrar_execucao(coleta.finalizar(self.saida))
        return self.ultima

    def _verificar_coleta_parada(self) -> None:
        """Finaliza a coleta se o usuário pediu para parar e o coletor não respondeu,
        ou se a aba da ANEEL foi fechada no meio (sem atividade há 10 minutos)."""
        if not self.coletando:
            return
        ocioso = time.time() - self.coleta.ultima_atividade
        if (self.coleta.cancelar.is_set() and ocioso > 15) or ocioso > 600:
            log.warning("O coletor parou de responder; gerando relatório com o que foi coletado.")
            self.finalizar_coleta()

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
        self._registrar_execucao(execucao)

    def _registrar_execucao(self, execucao: Execucao) -> None:
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
        self._verificar_coleta_parada()
        andamento = {}
        if self.varredura is not None:
            r = self.varredura.resultado
            andamento = {"documentos": len(r.documentos), "ocorrencias": len(r.ocorrencias),
                         "reunioes": len({d.reuniao for d in r.documentos})}
        return {
            "rodando": self.rodando,
            "cancelando": self.rodando and (self.cancelar.is_set() or
                                            (self.coletando and self.coleta.cancelar.is_set())),
            "coletando": self.coletando,
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

        def _origem_coleta(self) -> str | None:
            """Origem da ANEEL autorizada a chamar as rotas /api/coleta/ (ou None)."""
            origem = self.headers.get("Origin") or ""
            if urlparse(self.path).path.startswith("/api/coleta/") and origem in estado.origens_coleta:
                return origem
            return None

        def _cabecalhos_cors(self) -> None:
            origem = self._origem_coleta()
            if origem:
                self.send_header("Access-Control-Allow-Origin", origem)
                self.send_header("Vary", "Origin")

        def _json(self, dados, status=HTTPStatus.OK) -> None:
            corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self._cabecalhos_cors()
            self.end_headers()
            self.wfile.write(corpo)

        def _erro(self, mensagem: str, status=HTTPStatus.BAD_REQUEST) -> None:
            self._json({"erro": mensagem}, status)

        def _corpo(self) -> bytes:
            tamanho = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(tamanho) if tamanho else b""

        # ---------------------------------------------------------- OPTIONS (pré-verificação do navegador)
        def do_OPTIONS(self):  # noqa: N802
            origem = self._origem_coleta()
            if not self._host_local() or not origem:
                self.send_response(HTTPStatus.FORBIDDEN)
                self.end_headers()
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", origem)
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Painel")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.end_headers()

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
            elif url.path == "/coletor.js":
                corpo = COLETOR.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
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
                if url.path.startswith("/api/coleta/"):
                    return self._coleta(url.path.rsplit("/", 1)[-1], parse_qs(url.query))
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
                elif url.path == "/api/preparar_coleta":
                    estado.pedido_coleta = dados
                    self._json({"ok": True})
                elif url.path == "/api/parar":
                    if estado.coletando:
                        estado.coleta.cancelar.set()
                        log.warning("Parando a coleta… (o coletor encerra após o documento atual)")
                    elif estado.rodando:
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

        def _coleta(self, acao: str, consulta: dict) -> None:
            """Rotas chamadas pelo coletor (web/coletor.js) a partir da página da ANEEL."""
            def q(nome: str, padrao: str = "") -> str:
                return (consulta.get(nome) or [padrao])[0]

            corpo = self._corpo()
            if acao == "inicio":
                return self._json(estado.iniciar_coleta())
            coleta = estado.coleta_ativa()
            area = int(q("area", "0") or 0)
            if acao == "listagem":
                self._json(coleta.receber_listagem(area, q("url"), corpo))
            elif acao == "pagina":
                self._json(coleta.receber_pagina(area, q("data"), q("titulo"), q("url"), corpo))
            elif acao == "anexo":
                self._json(coleta.receber_anexo(area, q("data"), q("titulo"), q("url_reuniao"), q("nome"), q("url"),
                                                corpo, q("tipo")))
            elif acao == "erro":
                self._json(coleta.receber_erro(area, q("data"), q("titulo"), q("nome"), q("url"), q("mensagem")))
            elif acao == "fim":
                self._json(estado.finalizar_coleta())
            else:
                self._erro("Não encontrado", HTTPStatus.NOT_FOUND)

        def _receber_arquivo(self, consulta: dict) -> None:
            nome = Path(unquote((consulta.get("nome") or [""])[0])).name
            nome = re.sub(r"[^\w\-. ()]+", "_", nome).strip()
            if not nome or not nome.lower().endswith(EXTENSOES_ENVIO):
                return self._erro(f"Tipo de arquivo não suportado: {nome}")
            estado.pasta_envios.mkdir(parents=True, exist_ok=True)
            (estado.pasta_envios / nome).write_bytes(self._corpo())
            self._json({"ok": True, "nome": nome})

    return Handler


def criar_servidor(caminho_config: Path, saida: Path, porta: int = 8765,
                   origens_coleta=()) -> tuple[ThreadingHTTPServer, Estado]:
    """origens_coleta: origens extras autorizadas a usar a coleta (usado nos testes)."""
    estado = Estado(caminho_config, saida, origens_coleta)
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
