import shutil
import threading

import pytest
import requests

from aneel_busca.coleta import Coleta
from aneel_busca.executor import carregar_config
from aneel_busca.painel import criar_servidor

from .test_aneel_busca import CONFIG, DETALHE, FIXTURES, config_teste, pdf_minimo

ANEEL = "https://www2.aneel.gov.br"
LISTA = (FIXTURES / "lista_425_pagina1.html").read_bytes()
URL_REUNIAO = f"{ANEEL}/aplicacoes_liferay/noticias_area/dsp_detalheNoticia.cfm?idNoticia=14810&idAreaNoticia=425"


def test_coleta_fluxo_completo(tmp_path):
    coleta = Coleta(config_teste(), {"areas": ["425"], "desde": "2026-09-20", "ate": "2026-09-30"})
    plano = coleta.plano()
    assert plano["areas"][0]["id"] == 425 and "idAreaNoticia=425" in plano["areas"][0]["url"]
    assert plano["desde"] == "20/09/2026"

    lista = coleta.receber_listagem(425, plano["areas"][0]["url"], LISTA)
    assert [r["data"] for r in lista["reunioes"]] == ["29/09/2026", "22/09/2026"]
    assert lista["proxima"] is None  # a página já tem reuniões anteriores a "desde"
    assert lista["parar"] is False

    pagina = coleta.receber_pagina(425, "22/09/2026", "19ª REUNIÃO", URL_REUNIAO, DETALHE)
    assert [a["nome"] for a in pagina["anexos"]] == ["Pauta completa", "Ata (PDF)"]

    anexo = coleta.receber_anexo(425, "22/09/2026", "19ª REUNIÃO", URL_REUNIAO, "Pauta completa",
                                 pagina["anexos"][0]["url"], pdf_minimo("Energisa Sul-Sudeste"), "application/pdf")
    assert anexo["ocorrencias"] == 1
    coleta.receber_erro(425, "22/09/2026", "19ª REUNIÃO", "Ata (PDF)", pagina["anexos"][1]["url"], "HTTP 404")

    coleta.cancelar.set()
    assert coleta.receber_erro(425, "", "", "x", "y", "z")["parar"] is True

    execucao = coleta.finalizar(tmp_path)
    assert execucao.situacao == "cancelada" and execucao.xlsx.exists()
    termos = {o.termo for o in execucao.resultado.ocorrencias}
    assert {"48500.001234/2025-11", "Energisa Paraíba (EPB)", "Energisa Sul-Sudeste (ESS)"} <= termos
    assert sum(d.situacao.startswith("ERRO") for d in execucao.resultado.documentos) == 2


def test_coleta_respeita_max_paginas():
    coleta = Coleta(config_teste(), {"areas": ["425"], "desde": "2020-01-01", "max_paginas": 1})
    assert coleta.receber_listagem(425, "u", LISTA)["proxima"] is None
    coleta2 = Coleta(config_teste(), {"areas": ["425"], "desde": "2020-01-01", "max_paginas": 3})
    assert "page=2" in coleta2.receber_listagem(425, "u", LISTA)["proxima"]


@pytest.fixture
def painel(tmp_path):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG, config)
    servidor, estado = criar_servidor(config, tmp_path / "resultados", porta=18865)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{servidor.server_address[1]}"
    sessao = requests.Session()
    sessao.trust_env = False
    yield base, sessao, estado
    servidor.shutdown()
    servidor.server_close()


def test_cors_somente_para_aneel(painel):
    base, sessao, _ = painel
    pre = sessao.options(base + "/api/coleta/inicio", headers={
        "Origin": ANEEL, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-painel"})
    assert pre.status_code == 204
    assert pre.headers["Access-Control-Allow-Origin"] == ANEEL
    assert pre.headers["Access-Control-Allow-Private-Network"] == "true"
    # outra origem, ou rota que não é de coleta: recusado
    assert sessao.options(base + "/api/coleta/inicio", headers={"Origin": "https://malicioso.com"}).status_code == 403
    assert sessao.options(base + "/api/config", headers={"Origin": ANEEL}).status_code == 403
    r = sessao.get(base + "/coletor.js")
    assert r.status_code == 200 and "Coletor ANEEL" in r.text


def test_coleta_pelo_painel(painel):
    base, sessao, estado = painel
    cab = {"X-Painel": "1", "Origin": ANEEL}

    def post(acao, corpo=b"", **params):
        return sessao.post(f"{base}/api/coleta/{acao}", params=params, data=corpo, headers=cab)

    assert post("listagem").status_code == 400  # sem coleta iniciada
    sessao.post(base + "/api/preparar_coleta", json={"areas": ["425"], "desde": "2026-09-20", "ate": "2026-09-30"},
                headers={"X-Painel": "1"})
    plano = post("inicio", b"{}").json()
    assert plano["areas"][0]["id"] == 425
    assert post("inicio", b"{}").status_code == 400  # já existe coleta ativa

    status = sessao.get(base + "/api/status").json()
    assert status["rodando"] and status["coletando"]
    assert sessao.post(base + "/api/iniciar", json={"comando": "online"}, headers={"X-Painel": "1"}).status_code == 400

    lista = post("listagem", LISTA, area=425, url=plano["areas"][0]["url"]).json()
    assert lista["reunioes"][1]["data"] == "22/09/2026"
    assert lista.get("parar") is False
    r = post("pagina", DETALHE, area=425, data="22/09/2026", titulo="19ª REUNIÃO", url=URL_REUNIAO)
    assert r.headers["Access-Control-Allow-Origin"] == ANEEL
    anexos = r.json()["anexos"]
    post("anexo", pdf_minimo("Energisa Acre"), area=425, data="22/09/2026", titulo="19ª REUNIÃO",
         url_reuniao=URL_REUNIAO, nome=anexos[1]["nome"], url=anexos[1]["url"], tipo="application/pdf")

    sessao.post(base + "/api/parar", json={}, headers={"X-Painel": "1"})
    assert post("erro", area=425, nome="x", url="y", mensagem="z").json()["parar"] is True

    fim = post("fim", b"{}").json()
    assert fim["situacao"] == "cancelada" and fim["arquivo"]
    status = sessao.get(base + "/api/status").json()
    assert not status["rodando"]
    termos = {o["termo"] for o in sessao.get(base + "/api/resultados").json()["ocorrencias"]}
    assert {"Energisa Paraíba (EPB)", "Energisa Acre (EAC)"} <= termos


def test_coleta_abandonada_e_finalizada(painel):
    base, sessao, estado = painel
    sessao.post(f"{base}/api/coleta/inicio", data=b"{}", headers={"X-Painel": "1", "Origin": ANEEL})
    estado.coleta.cancelar.set()
    estado.coleta.ultima_atividade -= 60  # coletor não respondeu depois do "Parar"
    status = sessao.get(base + "/api/status").json()
    assert not status["rodando"] and status["ultima"]["situacao"] == "cancelada"


def test_config_do_painel_continua_valida(painel):
    _, _, estado = painel
    assert carregar_config(estado.caminho_config)["empresas"]
