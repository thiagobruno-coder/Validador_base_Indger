"""Leitura das páginas de notícias da ANEEL (listagens e detalhe de cada reunião)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

from .extracao import EXTENSOES_DOCUMENTO, sopa, texto_de_html

BASE = "https://www2.aneel.gov.br/aplicacoes_liferay/noticias_area/"


def url_listagem(id_area: int, pagina: int = 1) -> str:
    return (f"{BASE}dsp_listarNoticias.cfm?idAreaNoticia={id_area}&idarea={id_area}"
            f"&page={pagina}&txtPesq=&anoPesq=&mesPesq=")


@dataclass
class Reuniao:
    data: date | None
    titulo: str
    url: str
    id_noticia: str = ""
    id_area: str = ""

    @property
    def previa(self) -> bool:
        return "PRÉVIA" in self.titulo.upper() or "PREVIA" in self.titulo.upper()


@dataclass
class Listagem:
    reunioes: list[Reuniao] = field(default_factory=list)
    proxima_pagina: str | None = None


@dataclass
class Detalhe:
    texto: str
    anexos: list[tuple[str, str]]  # (nome do link, URL)


def _data(texto: str) -> date | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", texto)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%d/%m/%Y").date()
    except ValueError:
        return None


def ler_listagem(conteudo: bytes | str, url_base: str = BASE) -> Listagem:
    doc = sopa(conteudo)
    listagem = Listagem()
    for link in doc.select("a.titulo_noticias"):
        href = link.get("href", "")
        if not href:
            continue
        url = urljoin(url_base, href)
        consulta = parse_qs(urlparse(url).query)
        linha = link.find_parent("tr")
        data = _data(linha.get_text(" ")) if linha else None
        listagem.reunioes.append(Reuniao(
            data=data,
            titulo=re.sub(r"\s+", " ", link.get_text()).strip(),
            url=url,
            id_noticia=(consulta.get("idNoticia") or [""])[0],
            id_area=(consulta.get("idAreaNoticia") or [""])[0],
        ))
    for link in doc.find_all("a", href=True):
        if "xima" in link.get_text() and "page=" in link["href"]:  # "Próximas 15 >>"
            listagem.proxima_pagina = urljoin(url_base, link["href"])
            break
    return listagem


def _parece_documento(url: str, texto_link: str) -> bool:
    caminho = urlparse(url).path.lower()
    if caminho.endswith(EXTENSOES_DOCUMENTO) and not caminho.endswith((".htm", ".html")):
        return True
    alvo = (url + " " + texto_link).lower()
    return any(chave in alvo for chave in (
        "download", "arquivo", "anexo", "/documents/", "cedoc", "sei.aneel.gov.br",
        "controlador_externo", "documento_consulta", ".pdf",
    ))


def ler_detalhe(conteudo: bytes | str, url_base: str) -> Detalhe:
    doc = sopa(conteudo)
    anexos: list[tuple[str, str]] = []
    vistos = set()
    for link in doc.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(url_base, href)
        nome = re.sub(r"\s+", " ", link.get_text()).strip()
        if url not in vistos and _parece_documento(url, nome):
            vistos.add(url)
            anexos.append((nome or url.rsplit("/", 1)[-1], url))
    return Detalhe(texto=texto_de_html(conteudo), anexos=anexos)


def eh_desafio_cloudflare(status: int, conteudo: bytes) -> bool:
    if status not in (403, 429, 503) and b"cf_chl" not in conteudo[:20000]:
        return False
    trecho = conteudo[:30000].lower()
    return any(marca in trecho for marca in (
        b"just a moment", b"um momento", b"cf_chl_opt", b"challenge-platform", b"enable javascript and cookies",
    ))
