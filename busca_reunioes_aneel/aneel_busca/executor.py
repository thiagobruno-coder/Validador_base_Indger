"""Execução de uma busca completa, usada pela linha de comando e pelo painel."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from .busca import Buscador
from .cliente import BloqueioCloudflare, Cache, Cliente
from .processamento import BuscaCancelada, Resultado, Varredura
from .relatorio import COLUNAS_DOCUMENTOS, COLUNAS_OCORRENCIAS, gravar_csv, gravar_excel

log = logging.getLogger("aneel_busca")

AREAS_PADRAO = {425: "Pautas e Atas", 424: "Distribuição de Processos"}


class ErroConfiguracao(ValueError):
    pass


def ler_data(valor) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(valor).strip(), formato).date()
        except ValueError:
            continue
    raise ErroConfiguracao(f"Data inválida: {valor} (use AAAA-MM-DD ou DD/MM/AAAA)")


def carregar_config(caminho: Path) -> dict:
    if not caminho.exists():
        raise ErroConfiguracao(f"Arquivo de configuração não encontrado: {caminho}")
    with caminho.open(encoding="utf-8") as arquivo:
        return yaml.safe_load(arquivo) or {}


def salvar_config(caminho: Path, config: dict) -> None:
    cabecalho = ("# Configuração da busca nas Reuniões Públicas da ANEEL.\n"
                 "# Pode ser editada aqui ou pelo painel (iniciar_painel.bat).\n\n")
    texto = yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120)
    caminho.write_text(cabecalho + texto, encoding="utf-8")


@dataclass
class Execucao:
    resultado: Resultado
    xlsx: Path | None
    situacao: str  # "concluida" | "cancelada" | "bloqueada" | "parcial"
    mensagem: str = ""


def executar(config: dict, *, comando: str, saida: Path, pasta: Path | None = None, areas=None,
             desde=None, ate=None, max_paginas: int | None = None, modo: str = "auto",
             headless: bool = False, sem_cache: bool = False, salvar_textos: bool = False,
             cancelar=None, pasta_cache: Path = Path(".cache_aneel"),
             perfil_navegador: Path = Path(".perfil_navegador"),
             ao_criar_varredura=None) -> Execucao:
    """Roda a busca (comando "online" ou "local") e grava os relatórios em `saida`."""
    buscador = Buscador.de_config(config)
    if not buscador.termos:
        raise ErroConfiguracao("Nenhum termo de busca configurado (processos, empresas ou palavras-chave).")
    log.info("%d termo(s) de busca carregado(s)", len(buscador.termos))

    saida.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    varredura = Varredura(buscador, saida / f"textos_{carimbo}" if salvar_textos else None, cancelar=cancelar)
    if ao_criar_varredura:
        ao_criar_varredura(varredura)

    situacao, mensagem = "concluida", ""
    try:
        if comando == "online":
            todas = config.get("areas") or AREAS_PADRAO
            todas = {int(k): v for k, v in todas.items()}
            if areas:
                todas = {int(a): todas.get(int(a), f"Área {a}") for a in areas}
            cache = Cache(None if sem_cache else pasta_cache)
            with Cliente(modo=modo, cache=cache, headless=headless, perfil_navegador=perfil_navegador,
                         intervalo=float(config.get("intervalo_requisicoes") or 1.5)) as cliente:
                varredura.varrer_online(
                    cliente, todas,
                    desde=ler_data(desde) or ler_data(config.get("desde")),
                    ate=ler_data(ate) or ler_data(config.get("ate")),
                    max_paginas=int(max_paginas or config.get("max_paginas") or 10),
                )
        elif comando == "local":
            if not pasta or not Path(pasta).is_dir():
                raise ErroConfiguracao(f"Pasta não encontrada: {pasta}")
            varredura.varrer_pasta(Path(pasta))
        else:
            raise ErroConfiguracao(f"Comando desconhecido: {comando}")
    except BloqueioCloudflare as erro:
        log.error("%s", erro)
        situacao, mensagem = "bloqueada", str(erro)
    except (BuscaCancelada, KeyboardInterrupt):
        log.warning("Busca interrompida; gerando relatório parcial.")
        situacao, mensagem = "cancelada", "Busca interrompida pelo usuário"

    resultado = varredura.resultado
    if situacao == "bloqueada" and not resultado.documentos:
        return Execucao(resultado, None, situacao, mensagem)

    xlsx = saida / f"busca_aneel_{carimbo}.xlsx"
    gravar_excel(xlsx, resultado.ocorrencias, resultado.documentos)
    gravar_csv(saida / f"ocorrencias_{carimbo}.csv", resultado.ocorrencias, COLUNAS_OCORRENCIAS)
    gravar_csv(saida / f"documentos_{carimbo}.csv", resultado.documentos, COLUNAS_DOCUMENTOS)
    if situacao == "bloqueada":
        situacao = "parcial"
    log.info("Relatório gravado: %s", xlsx.resolve())
    return Execucao(resultado, xlsx, situacao, mensagem)
