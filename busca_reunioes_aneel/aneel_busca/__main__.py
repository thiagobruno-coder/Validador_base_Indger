"""Linha de comando.

Exemplos:
    python -m aneel_busca online
    python -m aneel_busca online --desde 2026-06-01 --areas 425
    python -m aneel_busca local pasta_com_pdfs
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from .busca import Buscador
from .cliente import BloqueioCloudflare, Cache, Cliente
from .processamento import Varredura
from .relatorio import COLUNAS_DOCUMENTOS, COLUNAS_OCORRENCIAS, gravar_csv, gravar_excel

log = logging.getLogger("aneel_busca")


def _data(valor: str | None) -> date | None:
    if not valor:
        return None
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(valor), formato).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Data inválida: {valor} (use AAAA-MM-DD ou DD/MM/AAAA)")


def carregar_config(caminho: Path) -> dict:
    if not caminho.exists():
        sys.exit(f"Arquivo de configuração não encontrado: {caminho}")
    with caminho.open(encoding="utf-8") as arquivo:
        return yaml.safe_load(arquivo) or {}


def montar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aneel_busca",
        description="Procura processos, empresas do Grupo Energisa e palavras-chave nas pautas/atas da ANEEL.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="arquivo de configuração")
    parser.add_argument("--saida", type=Path, default=Path("resultados"), help="pasta dos relatórios")
    parser.add_argument("--salvar-textos", action="store_true",
                        help="salva o texto extraído de cada documento (útil para conferência)")
    parser.add_argument("-v", "--verbose", action="store_true", help="mostra mensagens detalhadas")
    sub = parser.add_subparsers(dest="comando", required=True)

    online = sub.add_parser("online", help="baixa as reuniões do site da ANEEL e faz a busca")
    online.add_argument("--areas", nargs="+", help="idAreaNoticia a varrer (padrão: os do config)")
    online.add_argument("--desde", type=_data, help="data inicial (AAAA-MM-DD)")
    online.add_argument("--ate", type=_data, help="data final (AAAA-MM-DD)")
    online.add_argument("--max-paginas", type=int, help="máximo de páginas por área (15 reuniões cada)")
    online.add_argument("--modo", choices=["auto", "requests", "navegador"], default="auto",
                        help="auto: tenta acesso direto e abre o navegador se o Cloudflare bloquear")
    online.add_argument("--headless", action="store_true", help="navegador invisível (o Cloudflare pode bloquear)")
    online.add_argument("--sem-cache", action="store_true", help="baixa tudo de novo, ignorando o cache")

    local = sub.add_parser("local", help="faz a busca em arquivos já baixados (PDF, HTML, DOCX…)")
    local.add_argument("pasta", type=Path, help="pasta com os arquivos")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = montar_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for ruidoso in ("pypdf", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.ERROR)

    config = carregar_config(args.config)
    buscador = Buscador.de_config(config)
    if not buscador.termos:
        sys.exit("Nenhum termo de busca configurado (processos, empresas ou palavras_chave) no config.")
    log.info("%d termo(s) de busca carregado(s)", len(buscador.termos))

    args.saida.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    varredura = Varredura(buscador, args.saida / f"textos_{carimbo}" if args.salvar_textos else None)

    try:
        if args.comando == "online":
            areas = config.get("areas") or {425: "Pautas e Atas", 424: "Distribuição de Processos"}
            if args.areas:
                areas = {int(a): areas.get(int(a), f"Área {a}") for a in args.areas}
            cache = Cache(None if args.sem_cache else Path(".cache_aneel"))
            with Cliente(modo=args.modo, cache=cache, headless=args.headless,
                         intervalo=float(config.get("intervalo_requisicoes") or 1.5)) as cliente:
                varredura.varrer_online(
                    cliente, areas,
                    desde=args.desde or _data(config.get("desde")),
                    ate=args.ate or _data(config.get("ate")),
                    max_paginas=args.max_paginas or int(config.get("max_paginas") or 10),
                )
        else:
            if not args.pasta.is_dir():
                sys.exit(f"Pasta não encontrada: {args.pasta}")
            varredura.varrer_pasta(args.pasta)
    except BloqueioCloudflare as erro:
        log.error("%s", erro)
        if not varredura.resultado.documentos:
            return 2
        log.warning("Gerando relatório parcial com o que foi lido até aqui.")
    except KeyboardInterrupt:
        log.warning("Interrompido pelo usuário; gerando relatório parcial.")

    resultado = varredura.resultado
    xlsx = args.saida / f"busca_aneel_{carimbo}.xlsx"
    gravar_excel(xlsx, resultado.ocorrencias, resultado.documentos)
    gravar_csv(args.saida / f"ocorrencias_{carimbo}.csv", resultado.ocorrencias, COLUNAS_OCORRENCIAS)
    gravar_csv(args.saida / f"documentos_{carimbo}.csv", resultado.documentos, COLUNAS_DOCUMENTOS)

    erros = sum(1 for d in resultado.documentos if d.situacao != "OK")
    print()
    print(f"Documentos analisados: {len(resultado.documentos)} (com erro: {erros})")
    print(f"Ocorrências encontradas: {len(resultado.ocorrencias)}")
    for termo in sorted({o.termo for o in resultado.ocorrencias}):
        reunioes = sorted({f"{o.data} {o.reuniao}" for o in resultado.ocorrencias if o.termo == termo})
        print(f"  • {termo}: {len(reunioes)} reunião(ões)")
        for r in reunioes[:5]:
            print(f"      - {r}")
        if len(reunioes) > 5:
            print(f"      … e mais {len(reunioes) - 5}")
    print(f"\nRelatório: {xlsx.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
