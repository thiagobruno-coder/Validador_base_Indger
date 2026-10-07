"""Linha de comando.

Exemplos:
    python -m aneel_busca painel
    python -m aneel_busca online
    python -m aneel_busca online --desde 2026-06-01 --areas 425
    python -m aneel_busca local pasta_com_pdfs
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .executor import ErroConfiguracao, carregar_config, executar, ler_data

log = logging.getLogger("aneel_busca")


def _data(valor: str | None):
    try:
        return ler_data(valor)
    except ErroConfiguracao as erro:
        raise argparse.ArgumentTypeError(str(erro)) from erro


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

    painel = sub.add_parser("painel", help="abre o painel no navegador (interface gráfica)")
    painel.add_argument("--porta", type=int, default=8765, help="porta local do painel")
    painel.add_argument("--nao-abrir", action="store_true", help="não abre o navegador automaticamente")

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

    if args.comando == "painel":
        from .painel import iniciar_painel

        return iniciar_painel(args.config, args.saida, porta=args.porta, abrir_navegador=not args.nao_abrir)

    try:
        config = carregar_config(args.config)
        if args.comando == "online":
            execucao = executar(config, comando="online", saida=args.saida, areas=args.areas, desde=args.desde,
                                ate=args.ate, max_paginas=args.max_paginas, modo=args.modo,
                                headless=args.headless, sem_cache=args.sem_cache,
                                salvar_textos=args.salvar_textos)
        else:
            execucao = executar(config, comando="local", saida=args.saida, pasta=args.pasta,
                                salvar_textos=args.salvar_textos)
    except ErroConfiguracao as erro:
        sys.exit(str(erro))

    if execucao.xlsx is None:
        return 2

    resultado = execucao.resultado
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
    print(f"\nRelatório: {execucao.xlsx.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
