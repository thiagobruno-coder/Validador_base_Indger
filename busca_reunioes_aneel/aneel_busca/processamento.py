"""Orquestra a varredura: listagens → reuniões → anexos → busca → relatório."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .busca import Buscador
from .cliente import BloqueioCloudflare, Cliente
from .extracao import EXTENSOES_DOCUMENTO, extrair_texto, extrair_texto_arquivo
from .relatorio import LinhaDocumento, LinhaOcorrencia
from .site_aneel import Reuniao, ler_detalhe, ler_listagem, url_listagem

log = logging.getLogger(__name__)


@dataclass
class Resultado:
    ocorrencias: list[LinhaOcorrencia] = field(default_factory=list)
    documentos: list[LinhaDocumento] = field(default_factory=list)


class Varredura:
    def __init__(self, buscador: Buscador, pasta_textos: Path | None = None):
        self.buscador = buscador
        self.pasta_textos = pasta_textos
        self.resultado = Resultado()
        if pasta_textos:
            pasta_textos.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ registro
    def analisar_texto(self, texto: str, *, data: str, area: str, reuniao: str, url_reuniao: str,
                       documento: str, url_documento: str, tipo: str) -> int:
        ocorrencias = self.buscador.buscar(texto)
        for o in ocorrencias:
            self.resultado.ocorrencias.append(LinhaOcorrencia(
                data=data, area=area, reuniao=reuniao, url_reuniao=url_reuniao, documento=documento,
                url_documento=url_documento, categoria=o.categoria, termo=o.termo, encontrado=o.encontrado,
                item=o.item, trecho=o.trecho,
            ))
        self.resultado.documentos.append(LinhaDocumento(
            data=data, area=area, reuniao=reuniao, documento=documento, url_documento=url_documento, tipo=tipo,
            caracteres=len(texto), ocorrencias=len(ocorrencias),
            processos_citados=", ".join(self.buscador.processos_citados(texto)), situacao="OK",
        ))
        if self.pasta_textos:
            nome = re.sub(r"[^\w\-]+", "_", f"{data}_{reuniao}_{documento}")[:150] + ".txt"
            (self.pasta_textos / nome).write_text(texto, encoding="utf-8")
        return len(ocorrencias)

    def registrar_erro(self, erro: Exception, *, data: str, area: str, reuniao: str, documento: str,
                       url_documento: str) -> None:
        log.warning("Erro em '%s' (%s): %s", documento, reuniao, erro)
        self.resultado.documentos.append(LinhaDocumento(
            data=data, area=area, reuniao=reuniao, documento=documento, url_documento=url_documento, tipo="",
            caracteres=0, ocorrencias=0, processos_citados="", situacao=f"ERRO: {erro}",
        ))

    # ------------------------------------------------------------------ online
    def varrer_online(self, cliente: Cliente, areas: dict, *, desde: date | None, ate: date | None,
                      max_paginas: int) -> Resultado:
        for id_area, nome_area in areas.items():
            log.info("=== Área %s — %s ===", id_area, nome_area)
            url = url_listagem(int(id_area), 1)
            for pagina in range(1, max_paginas + 1):
                resposta = cliente.obter(url, usar_cache=False)  # listagem muda toda semana
                listagem = ler_listagem(resposta.conteudo, resposta.url)
                if not listagem.reunioes:
                    log.warning("Nenhuma reunião encontrada na página %d (layout mudou?)", pagina)
                    break
                passou_do_periodo = False
                for reuniao in listagem.reunioes:
                    if reuniao.data and desde and reuniao.data < desde:
                        passou_do_periodo = True
                        continue
                    if reuniao.data and ate and reuniao.data > ate:
                        continue
                    self._processar_reuniao(cliente, reuniao, str(nome_area))
                if passou_do_periodo or not listagem.proxima_pagina:
                    break
                url = listagem.proxima_pagina
        return self.resultado

    def _processar_reuniao(self, cliente: Cliente, reuniao: Reuniao, area: str) -> None:
        data = reuniao.data.strftime("%d/%m/%Y") if reuniao.data else ""
        log.info("%s  %s", data, reuniao.titulo)
        base = dict(data=data, area=area, reuniao=reuniao.titulo)
        usar_cache = not reuniao.previa  # pauta prévia ainda pode mudar
        try:
            resposta = cliente.obter(reuniao.url, usar_cache=usar_cache)
            if resposta.status >= 400:
                raise RuntimeError(f"HTTP {resposta.status}")
            detalhe = ler_detalhe(resposta.conteudo, resposta.url)
        except BloqueioCloudflare:
            raise
        except Exception as erro:  # noqa: BLE001
            self.registrar_erro(erro, documento="Página da reunião", url_documento=reuniao.url, **base)
            return
        n = self.analisar_texto(detalhe.texto, url_reuniao=reuniao.url, documento="Página da reunião",
                                url_documento=reuniao.url, tipo="html", **base)
        log.info("    página da reunião: %d ocorrência(s); %d anexo(s)", n, len(detalhe.anexos))
        for nome, url_anexo in detalhe.anexos:
            try:
                r = cliente.obter(url_anexo, usar_cache=usar_cache)
                if r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}")
                tipo, texto = extrair_texto(r.conteudo, r.content_type, url_anexo)
            except BloqueioCloudflare:
                raise
            except Exception as erro:  # noqa: BLE001
                self.registrar_erro(erro, documento=nome, url_documento=url_anexo, **base)
                continue
            n = self.analisar_texto(texto, url_reuniao=reuniao.url, documento=nome, url_documento=url_anexo,
                                    tipo=tipo, **base)
            log.info("    %s: %d ocorrência(s)", nome[:70], n)

    # ------------------------------------------------------------------ local
    def varrer_pasta(self, pasta: Path) -> Resultado:
        arquivos = sorted(p for p in pasta.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSOES_DOCUMENTO)
        if not arquivos:
            log.warning("Nenhum documento (.pdf, .html, .docx…) encontrado em %s", pasta)
        for caminho in arquivos:
            if "_files" in caminho.parent.name or "_arquivos" in caminho.parent.name:
                continue  # recursos auxiliares salvos pelo navegador
            relativo = str(caminho.relative_to(pasta))
            try:
                tipo, texto = extrair_texto_arquivo(caminho)
            except Exception as erro:  # noqa: BLE001
                self.registrar_erro(erro, data="", area="Arquivos locais", reuniao=relativo, documento=caminho.name,
                                    url_documento=str(caminho))
                continue
            data = _data_no_texto(texto)
            n = self.analisar_texto(texto, data=data, area="Arquivos locais", reuniao=relativo, url_reuniao="",
                                    documento=caminho.name, url_documento=str(caminho.resolve()), tipo=tipo)
            log.info("%s: %d ocorrência(s)", relativo, n)
        return self.resultado


def _data_no_texto(texto: str) -> str:
    m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", texto[:3000])
    return m.group(1) if m else ""
