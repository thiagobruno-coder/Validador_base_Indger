"""Coleta pelo navegador do usuário.

O site da ANEEL é protegido pelo Cloudflare, que bloqueia navegadores automatizados.
Na coleta pelo navegador, o próprio usuário abre o site no Chrome, passa pela verificação
normalmente e clica no favorito "Coletar ANEEL". O script do favorito (web/coletor.js)
baixa as listagens, as páginas das reuniões e os anexos *dentro da sessão do usuário*,
em ritmo moderado, e envia cada conteúdo para o painel local. Esta classe decide o que
deve ser baixado (listagem → reuniões do período → anexos) e faz a análise.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .busca import Buscador
from .executor import AREAS_PADRAO, Execucao, ler_data
from .extracao import extrair_texto
from .processamento import Varredura
from .relatorio import COLUNAS_DOCUMENTOS, COLUNAS_OCORRENCIAS, gravar_csv, gravar_excel
from .site_aneel import ler_detalhe, ler_listagem, url_listagem

log = logging.getLogger("aneel_busca")


class Coleta:
    def __init__(self, config: dict, pedido: dict | None = None):
        pedido = pedido or {}
        self.buscador = Buscador.de_config(config)
        if not self.buscador.termos:
            raise ValueError("Nenhum termo de busca configurado (processos, empresas ou palavras-chave).")
        self.varredura = Varredura(self.buscador)
        todas = {int(k): v for k, v in (config.get("areas") or AREAS_PADRAO).items()}
        escolhidas = [int(a) for a in pedido.get("areas") or []] or list(todas)
        self.areas = {a: todas.get(a, f"Área {a}") for a in escolhidas}
        self.desde: date | None = ler_data(pedido.get("desde")) or ler_data(config.get("desde"))
        self.ate: date | None = ler_data(pedido.get("ate")) or ler_data(config.get("ate"))
        self.max_paginas = int(pedido.get("max_paginas") or config.get("max_paginas") or 10)
        self.intervalo = float(config.get("intervalo_requisicoes") or 1.5)
        self.cancelar = threading.Event()
        self.ativa = True
        self.ultima_atividade = time.time()
        self.paginas_lidas: dict[int, int] = {}
        self.trava = threading.Lock()

    # ------------------------------------------------------------------ protocolo com o coletor
    def plano(self) -> dict:
        """O que o coletor precisa saber para começar."""
        return {
            "areas": [{"id": a, "nome": n, "url": url_listagem(a, 1)} for a, n in self.areas.items()],
            "max_paginas": self.max_paginas,
            "intervalo_ms": int(self.intervalo * 1000),
            "desde": self.desde.strftime("%d/%m/%Y") if self.desde else "",
            "ate": self.ate.strftime("%d/%m/%Y") if self.ate else "",
        }

    def _tocar(self) -> dict:
        self.ultima_atividade = time.time()
        return {"parar": self.cancelar.is_set()}

    def receber_listagem(self, id_area: int, url: str, conteudo: bytes) -> dict:
        with self.trava:
            resposta = self._tocar()
            nome_area = self.areas.get(id_area, f"Área {id_area}")
            pagina = self.paginas_lidas.get(id_area, 0) + 1
            self.paginas_lidas[id_area] = pagina
            listagem = ler_listagem(conteudo, url)
            if not listagem.reunioes:
                log.warning("[%s] página %d sem reuniões (a verificação do site expirou? recarregue a página)",
                            nome_area, pagina)
            reunioes, passou = [], False
            for r in listagem.reunioes:
                if r.data and self.desde and r.data < self.desde:
                    passou = True
                    continue
                if r.data and self.ate and r.data > self.ate:
                    continue
                reunioes.append({"data": r.data.strftime("%d/%m/%Y") if r.data else "", "titulo": r.titulo,
                                 "url": r.url})
            log.info("[%s] página %d: %d reunião(ões) no período", nome_area, pagina, len(reunioes))
            continuar = bool(listagem.proxima_pagina) and not passou and pagina < self.max_paginas
            resposta.update({"reunioes": reunioes, "proxima": listagem.proxima_pagina if continuar else None})
            return resposta

    def receber_pagina(self, id_area: int, data: str, titulo: str, url: str, conteudo: bytes) -> dict:
        with self.trava:
            resposta = self._tocar()
            base = dict(data=data, area=self.areas.get(id_area, f"Área {id_area}"), reuniao=titulo)
            log.info("%s  %s", data, titulo)
            try:
                detalhe = ler_detalhe(conteudo, url)
            except Exception as erro:  # noqa: BLE001
                self.varredura.registrar_erro(erro, documento="Página da reunião", url_documento=url, **base)
                resposta["anexos"] = []
                resposta["ocorrencias"] = 0
                return resposta
            n = self.varredura.analisar_texto(detalhe.texto, url_reuniao=url, documento="Página da reunião",
                                              url_documento=url, tipo="html", **base)
            log.info("    página da reunião: %d ocorrência(s); %d anexo(s)", n, len(detalhe.anexos))
            resposta["anexos"] = [{"nome": nome, "url": u} for nome, u in detalhe.anexos]
            resposta["ocorrencias"] = n
            return resposta

    def receber_anexo(self, id_area: int, data: str, titulo: str, url_reuniao: str, nome: str, url: str,
                      conteudo: bytes, content_type: str) -> dict:
        with self.trava:
            resposta = self._tocar()
            base = dict(data=data, area=self.areas.get(id_area, f"Área {id_area}"), reuniao=titulo)
            try:
                tipo, texto = extrair_texto(conteudo, content_type, url)
            except Exception as erro:  # noqa: BLE001
                self.varredura.registrar_erro(erro, documento=nome, url_documento=url, **base)
                resposta["ocorrencias"] = 0
                return resposta
            n = self.varredura.analisar_texto(texto, url_reuniao=url_reuniao, documento=nome, url_documento=url,
                                              tipo=tipo, **base)
            log.info("    %s: %d ocorrência(s)", nome[:70], n)
            resposta["ocorrencias"] = n
            return resposta

    def receber_erro(self, id_area: int, data: str, titulo: str, nome: str, url: str, mensagem: str) -> dict:
        with self.trava:
            resposta = self._tocar()
            self.varredura.registrar_erro(RuntimeError(mensagem), data=data,
                                          area=self.areas.get(id_area, f"Área {id_area}"), reuniao=titulo,
                                          documento=nome, url_documento=url)
            return resposta

    def finalizar(self, saida: Path) -> Execucao:
        with self.trava:
            self.ativa = False
            resultado = self.varredura.resultado
            saida.mkdir(parents=True, exist_ok=True)
            carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
            xlsx = saida / f"busca_aneel_{carimbo}.xlsx"
            gravar_excel(xlsx, resultado.ocorrencias, resultado.documentos)
            gravar_csv(saida / f"ocorrencias_{carimbo}.csv", resultado.ocorrencias, COLUNAS_OCORRENCIAS)
            gravar_csv(saida / f"documentos_{carimbo}.csv", resultado.documentos, COLUNAS_DOCUMENTOS)
            situacao = "cancelada" if self.cancelar.is_set() else "concluida"
            log.info("Relatório gravado: %s", xlsx.resolve())
            return Execucao(resultado, xlsx, situacao,
                            "Coleta interrompida (relatório parcial)" if situacao == "cancelada" else "")
