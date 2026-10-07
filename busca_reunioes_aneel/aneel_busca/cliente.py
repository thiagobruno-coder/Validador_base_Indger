"""Download das páginas e documentos, com cache, novas tentativas e modo navegador.

O site www2.aneel.gov.br é protegido pelo Cloudflare. Quando o acesso direto (requests)
é bloqueado, o modo navegador abre o Chrome/Chromium de verdade (visível), para que a
verificação "Um momento…" seja concluída normalmente; os downloads seguintes reutilizam
a mesma sessão do navegador.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .site_aneel import eh_desafio_cloudflare

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class BloqueioCloudflare(RuntimeError):
    pass


@dataclass
class Resposta:
    url: str
    status: int
    conteudo: bytes
    content_type: str


class Cache:
    def __init__(self, pasta: Path | None):
        self.pasta = pasta
        if pasta:
            pasta.mkdir(parents=True, exist_ok=True)

    def _caminhos(self, url: str) -> tuple[Path, Path]:
        chave = hashlib.sha1(url.encode()).hexdigest()
        return self.pasta / f"{chave}.bin", self.pasta / f"{chave}.json"

    def ler(self, url: str) -> Resposta | None:
        if not self.pasta:
            return None
        dados, meta = self._caminhos(url)
        if not (dados.exists() and meta.exists()):
            return None
        info = json.loads(meta.read_text(encoding="utf-8"))
        return Resposta(url, 200, dados.read_bytes(), info.get("content_type", ""))

    def gravar(self, resposta: Resposta) -> None:
        if not self.pasta:
            return
        dados, meta = self._caminhos(resposta.url)
        dados.write_bytes(resposta.conteudo)
        meta.write_text(json.dumps({"url": resposta.url, "content_type": resposta.content_type,
                                    "baixado_em": time.strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False), encoding="utf-8")


class Cliente:
    def __init__(self, *, modo: str = "auto", cache: Cache | None = None, intervalo: float = 1.5,
                 tentativas: int = 3, timeout: int = 60, perfil_navegador: Path | None = None,
                 headless: bool = False):
        """modo: "requests" (só acesso direto), "navegador" (sempre navegador) ou
        "auto" (tenta direto e passa para o navegador se o Cloudflare bloquear)."""
        self.modo = modo
        self.cache = cache or Cache(None)
        self.intervalo = intervalo
        self.tentativas = tentativas
        self.timeout = timeout
        self.perfil_navegador = perfil_navegador or Path(".perfil_navegador")
        self.headless = headless
        self._ultimo = 0.0
        self._sessao = requests.Session()
        self._sessao.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        })
        self._pw = None
        self._contexto = None
        self._pagina = None
        self._origens_liberadas: set[str] = set()

    # ------------------------------------------------------------------ público
    def obter(self, url: str, *, usar_cache: bool = True) -> Resposta:
        if usar_cache:
            em_cache = self.cache.ler(url)
            if em_cache:
                log.debug("cache: %s", url)
                return em_cache
        resposta = self._obter_com_tentativas(url)
        if usar_cache and resposta.status == 200:
            self.cache.gravar(resposta)
        return resposta

    def fechar(self) -> None:
        if self._contexto:
            self._contexto.close()
        if self._pw:
            self._pw.stop()
        self._sessao.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.fechar()

    # ------------------------------------------------------------------ interno
    def _aguardar_intervalo(self) -> None:
        espera = self.intervalo - (time.monotonic() - self._ultimo)
        if espera > 0:
            time.sleep(espera)
        self._ultimo = time.monotonic()

    def _obter_com_tentativas(self, url: str) -> Resposta:
        ultimo_erro: Exception | None = None
        for tentativa in range(1, self.tentativas + 1):
            self._aguardar_intervalo()
            try:
                if self.modo == "navegador" or self._contexto is not None:
                    resposta = self._via_navegador(url)
                else:
                    resposta = self._via_requests(url)
                    if eh_desafio_cloudflare(resposta.status, resposta.conteudo):
                        if self.modo == "requests":
                            raise BloqueioCloudflare(
                                "O site da ANEEL (Cloudflare) bloqueou o acesso direto. Use o acesso pelo "
                                "Chrome (modo 'auto' ou 'navegador') ou busque em arquivos já baixados.")
                        log.info("Cloudflare detectado; abrindo o navegador para concluir a verificação…")
                        resposta = self._via_navegador(url)
                if resposta.status >= 500 or resposta.status == 429:
                    raise requests.HTTPError(f"HTTP {resposta.status}")
                return resposta
            except BloqueioCloudflare:
                raise
            except Exception as erro:  # noqa: BLE001
                ultimo_erro = erro
                espera = 2 ** tentativa
                log.warning("Falha ao baixar %s (tentativa %d/%d): %s — nova tentativa em %ds",
                            url, tentativa, self.tentativas, erro, espera)
                time.sleep(espera)
        raise RuntimeError(f"Não foi possível baixar {url}: {ultimo_erro}")

    def _via_requests(self, url: str) -> Resposta:
        r = self._sessao.get(url, timeout=self.timeout)
        return Resposta(r.url, r.status_code, r.content, r.headers.get("Content-Type", ""))

    def _iniciar_navegador(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as erro:
            raise BloqueioCloudflare(
                "O modo navegador precisa do Playwright: pip install playwright && "
                "python -m playwright install chromium") from erro
        self._pw = sync_playwright().start()
        # chromium_sandbox=True evita o aviso "--no-sandbox" na barra do Chrome.
        opcoes = dict(headless=self.headless, locale="pt-BR", accept_downloads=True, chromium_sandbox=True)
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:  # redes corporativas: o navegador não lê essa variável sozinho
            opcoes["proxy"] = {"server": proxy}
            opcoes["ignore_https_errors"] = bool(os.environ.get("ANEEL_IGNORAR_CERTIFICADO"))
        executavel = os.environ.get("ANEEL_NAVEGADOR")  # caminho de um Chrome/Chromium específico
        tentativas = [dict(executable_path=executavel)] if executavel else [dict(channel="chrome"), {}]
        erros = []
        for extra in tentativas:  # prefere o Google Chrome instalado; senão, o Chromium do Playwright
            try:
                self._contexto = self._pw.chromium.launch_persistent_context(
                    str(self.perfil_navegador), **opcoes, **extra)
                break
            except Exception as erro:  # noqa: BLE001
                erros.append(str(erro).splitlines()[0])
        if self._contexto is None:
            self._pw.stop()
            self._pw = None
            raise BloqueioCloudflare(
                "Não foi possível abrir o navegador. Instale o Google Chrome ou rode "
                "'python -m playwright install chromium'. Detalhes: " + " | ".join(erros))
        self._pagina = self._contexto.pages[0] if self._contexto.pages else self._contexto.new_page()

    def _via_navegador(self, url: str) -> Resposta:
        if self._contexto is None:
            self._iniciar_navegador()
        origem = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if origem not in self._origens_liberadas:
            self._passar_verificacao(url)
            self._origens_liberadas.add(origem)
        resposta = self._buscar_na_pagina(url)
        if resposta is None or eh_desafio_cloudflare(resposta.status, resposta.conteudo):
            # A liberação expirou (ou o fetch não pôde ser feito): verifica de novo e repete.
            self._passar_verificacao(url)
            resposta = self._buscar_na_pagina(url)
        if resposta is None:  # outro domínio sem CORS: usa a API de requisições do navegador
            r = self._contexto.request.get(url, timeout=self.timeout * 1000)
            resposta = Resposta(r.url, r.status, r.body(), r.headers.get("content-type", ""))
        if eh_desafio_cloudflare(resposta.status, resposta.conteudo):
            raise BloqueioCloudflare("O Cloudflare continuou bloqueando mesmo após a verificação.")
        return resposta

    def _passar_verificacao(self, url: str) -> None:
        """Abre a URL na aba do navegador e espera a tela "Um momento…" do Cloudflare sumir."""
        try:
            self._pagina.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        except Exception as erro:  # noqa: BLE001  (ex.: a URL é um PDF e virou download)
            log.debug("Navegação para %s: %s", url, erro)
        limite = time.monotonic() + 180
        avisou = False
        while time.monotonic() < limite:
            try:
                html = self._pagina.content().encode("utf-8", "ignore")
            except Exception:  # noqa: BLE001  (a página recarrega durante a verificação)
                html = b"cf_chl just a moment"
            if not eh_desafio_cloudflare(403, html):
                return
            if not avisou:
                log.info("Aguardando a verificação do Cloudflare no navegador "
                         "(se aparecer uma caixa \"Confirme que você é humano\", clique nela)…")
                avisou = True
            time.sleep(2)
        raise BloqueioCloudflare(
            "A verificação do Cloudflare não foi concluída em 3 minutos: o site costuma recusar navegadores "
            "abertos automaticamente. Use a opção 'Pelo meu Chrome (recomendado)' no painel.")

    def _buscar_na_pagina(self, url: str) -> Resposta | None:
        """Baixa a URL com fetch() de dentro da página: usa os mesmos cookies e a mesma
        conexão que o Cloudflare liberou. Retorna None se o navegador recusar (CORS)."""
        try:
            dados = self._pagina.evaluate(_JS_FETCH, url)
        except Exception as erro:  # noqa: BLE001
            log.debug("fetch na página falhou para %s: %s", url, erro)
            return None
        return Resposta(dados["url"] or url, dados["status"], base64.b64decode(dados["b64"]), dados["tipo"])


_JS_FETCH = """
async (url) => {
  const r = await fetch(url, {credentials: "include"});
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return {status: r.status, tipo: r.headers.get("content-type") || "", url: r.url, b64: btoa(bin)};
}
"""
