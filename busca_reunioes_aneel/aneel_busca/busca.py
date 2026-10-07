"""Identificação de números de processo, empresas e palavras-chave em textos."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Processo ANEEL/SEI: 48500.001234/2025-11 (com variações de pontuação e espaços).
REGEX_PROCESSO = re.compile(r"(?<!\d)(\d{5})\s*\.?\s*(\d{6})\s*/?\s*(\d{4})\s*-?\s*(\d{2})(?!\d)")


def sem_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def normalizar_com_mapa(texto: str) -> tuple[str, list[int]]:
    """Minúsculas sem acentos, guardando o índice original de cada caractere.

    O mapa permite localizar a ocorrência no texto original para montar o trecho.
    """
    saida: list[str] = []
    mapa: list[int] = []
    for i, caractere in enumerate(texto):
        for c in sem_acentos(caractere).lower():
            saida.append(c)
            mapa.append(i)
    return "".join(saida), mapa


def formatar_processo(digitos: str) -> str:
    if len(digitos) == 17:
        return f"{digitos[:5]}.{digitos[5:11]}/{digitos[11:15]}-{digitos[15:]}"
    return digitos


def padrao_texto(termo: str) -> re.Pattern:
    """Termo normalizado como palavra inteira, tolerando espaços/hífens variáveis."""
    partes = [re.escape(p) for p in re.split(r"[\s\-]+", sem_acentos(termo).lower().strip()) if p]
    return re.compile(r"(?<![\w])" + r"[\s\-]+".join(partes) + r"(?![\w])")


def padrao_sigla(sigla: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + re.escape(sigla.strip()) + r"(?![\w])")


@dataclass
class Termo:
    categoria: str  # Processo | Empresa | Palavra-chave | Regex
    rotulo: str  # nome apresentado no relatório (ex.: "Energisa Sergipe (ESE)")
    padrao: re.Pattern | None = None
    no_original: bool = False  # True = busca no texto original (siglas, regex); False = no normalizado
    digitos: str = ""  # para processos
    generico: bool = False


@dataclass
class Ocorrencia:
    categoria: str
    termo: str
    encontrado: str
    trecho: str
    item: str
    posicao: int


@dataclass
class Buscador:
    termos: list[Termo] = field(default_factory=list)
    contexto: int = 250

    @classmethod
    def de_config(cls, config: dict) -> "Buscador":
        termos: list[Termo] = []
        for processo in config.get("processos") or []:
            digitos = re.sub(r"\D", "", str(processo))
            if len(digitos) >= 4:
                termos.append(Termo("Processo", formatar_processo(digitos), digitos=digitos))
        for nome_empresa, dados in (config.get("empresas") or {}).items():
            dados = dados or {}
            generico = "genérico" in nome_empresa.lower() or "generico" in nome_empresa.lower()
            for nome in dados.get("nomes") or []:
                termos.append(Termo("Empresa", nome_empresa, padrao_texto(nome), generico=generico))
            for sigla in dados.get("siglas") or []:
                termos.append(Termo("Empresa", nome_empresa, padrao_sigla(sigla), no_original=True, generico=generico))
        for palavra in config.get("palavras_chave") or []:
            termos.append(Termo("Palavra-chave", str(palavra), padrao_texto(str(palavra))))
        for nome, expressao in (config.get("regex") or {}).items():
            termos.append(Termo("Regex", nome, re.compile(expressao, re.IGNORECASE), no_original=True))
        return cls(termos=termos, contexto=int(config.get("contexto_caracteres") or 250))

    def processos_citados(self, texto: str) -> list[str]:
        vistos: dict[str, None] = {}
        for m in REGEX_PROCESSO.finditer(texto):
            vistos.setdefault(formatar_processo("".join(m.groups())), None)
        return list(vistos)

    def buscar(self, texto: str) -> list[Ocorrencia]:
        normalizado, mapa = normalizar_com_mapa(texto)
        brutas: list[tuple[Termo, int, int]] = []  # (termo, início, fim) no texto original

        processos = [(m.start(), m.end(), "".join(m.groups())) for m in REGEX_PROCESSO.finditer(texto)]
        for termo in self.termos:
            if termo.categoria == "Processo":
                for inicio, fim, digitos in processos:
                    if termo.digitos == digitos or (len(termo.digitos) < 17 and termo.digitos in digitos):
                        brutas.append((termo, inicio, fim))
                continue
            alvo = texto if termo.no_original else normalizado
            for m in termo.padrao.finditer(alvo):
                if m.end() <= m.start():
                    continue
                if termo.no_original:
                    brutas.append((termo, m.start(), m.end()))
                else:
                    brutas.append((termo, mapa[m.start()], mapa[m.end() - 1] + 1))

        brutas = self._remover_sobreposicoes(brutas)
        linhas_item = self._inicios_de_item(texto)
        ocorrencias = []
        for termo, inicio, fim in sorted(brutas, key=lambda b: b[1]):
            ocorrencias.append(Ocorrencia(
                categoria=termo.categoria,
                termo=termo.rotulo,
                encontrado=texto[inicio:fim],
                trecho=self._trecho(texto, inicio, fim),
                item=self._item_mais_proximo(linhas_item, inicio),
                posicao=inicio,
            ))
        return ocorrencias

    @staticmethod
    def _remover_sobreposicoes(brutas: list[tuple[Termo, int, int]]) -> list[tuple[Termo, int, int]]:
        """Entre empresas, a ocorrência mais específica prevalece no mesmo trecho:
        "Energisa Mato Grosso do Sul" não conta também como "Energisa Mato Grosso", e
        "Energisa Sergipe" não conta também como "Energisa" genérico.
        Ocorrências repetidas do mesmo termo no mesmo trecho são unificadas."""
        empresas = [(t, i, f) for t, i, f in brutas if t.categoria == "Empresa"]

        def coberta(termo: Termo, inicio: int, fim: int) -> bool:
            for t, i, f in empresas:
                if t.rotulo == termo.rotulo or not (i <= inicio and fim <= f):
                    continue
                if (f - i) > (fim - inicio) or (termo.generico and not t.generico):
                    return True
            return False

        resultado: list[tuple[Termo, int, int]] = []
        for termo, inicio, fim in sorted(brutas, key=lambda b: (b[1], -(b[2] - b[1]))):
            if termo.categoria == "Empresa" and coberta(termo, inicio, fim):
                continue
            if any((termo.categoria, termo.rotulo) == (t.categoria, t.rotulo) and i <= inicio < f
                   for t, i, f in resultado):
                continue
            resultado.append((termo, inicio, fim))
        return resultado

    def _trecho(self, texto: str, inicio: int, fim: int) -> str:
        a = max(0, inicio - self.contexto)
        b = min(len(texto), fim + self.contexto)
        trecho = texto[a:inicio] + "[[" + texto[inicio:fim] + "]]" + texto[fim:b]
        trecho = re.sub(r"\s+", " ", trecho).strip()
        return ("…" if a > 0 else "") + trecho + ("…" if b < len(texto) else "")

    @staticmethod
    def _inicios_de_item(texto: str) -> list[tuple[int, str]]:
        """Linhas que parecem início de item de pauta (ex.: "Item 3", "3.", "3 -", "Processo:")."""
        padrao = re.compile(
            r"^[ \t]*(?:(?:item|ITEM|Item)\s*n?[ºo°.]?\s*\d{1,3}\b|\d{1,3}\s*[.\-–)]\s+\S|(?:Processo|PROCESSO)s?\s*(?:n[ºo°.]?)?\s*:)[^\n]*",
            re.MULTILINE,
        )
        return [(m.start(), re.sub(r"\s+", " ", m.group(0)).strip()[:200]) for m in padrao.finditer(texto)]

    @staticmethod
    def _item_mais_proximo(linhas: list[tuple[int, str]], posicao: int) -> str:
        anterior = ""
        for inicio, linha in linhas:
            if inicio > posicao:
                break
            anterior = linha
        return anterior
