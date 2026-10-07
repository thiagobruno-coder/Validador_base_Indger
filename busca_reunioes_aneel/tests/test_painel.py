import shutil
import threading
import time
from pathlib import Path

import pytest
import requests

from aneel_busca.executor import carregar_config
from aneel_busca.painel import criar_servidor

from .test_aneel_busca import CONFIG, pdf_minimo


@pytest.fixture
def painel(tmp_path):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG, config)
    servidor, estado = criar_servidor(config, tmp_path / "resultados", porta=18765)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{servidor.server_address[1]}"
    sessao = requests.Session()
    sessao.trust_env = False  # não passa pelo proxy do ambiente
    yield base, sessao, estado, config
    servidor.shutdown()
    servidor.server_close()


def post(sessao, url, **kw):
    return sessao.post(url, headers={"X-Painel": "1", **kw.pop("headers", {})}, **kw)


def esperar_fim(sessao, base, limite=30):
    fim = time.time() + limite
    while time.time() < fim:
        status = sessao.get(f"{base}/api/status").json()
        if not status["rodando"] and status["ultima"]:
            return status
        time.sleep(0.2)
    raise AssertionError("a busca não terminou")


def test_pagina_e_config(painel):
    base, sessao, _, _ = painel
    pagina = sessao.get(base + "/")
    assert pagina.status_code == 200 and "Busca nas Reuniões Públicas da ANEEL" in pagina.text
    config = sessao.get(base + "/api/config").json()
    assert any(e["nome"] == "Energisa Sergipe (ESE)" for e in config["empresas"])
    assert {a["id"] for a in config["areas"]} == {424, 425}


def test_salvar_config(painel):
    base, sessao, _, caminho = painel
    config = sessao.get(base + "/api/config").json()
    for empresa in config["empresas"]:
        empresa["ativa"] = empresa["nome"] != "Grupo Energisa (genérico)"
    config["processos"] = ["48500.001234/2025-11", " ", "48500.001234/2025-11"]
    config["palavras_chave"] = ["revisão tarifária"]
    resposta = post(sessao, base + "/api/config", json=config)
    assert resposta.status_code == 200, resposta.text
    salvo = carregar_config(caminho)
    assert salvo["processos"] == ["48500.001234/2025-11"]  # vazios e repetidos removidos
    assert salvo["palavras_chave"] == ["revisão tarifária"]
    assert salvo["empresas_ignoradas"] == ["Grupo Energisa (genérico)"]
    assert salvo["empresas"]["Energisa Acre (EAC)"]["siglas"] == ["EAC"]


def test_bloqueia_post_sem_cabecalho_e_host_externo(painel):
    base, sessao, _, _ = painel
    assert sessao.post(base + "/api/iniciar", json={}).status_code == 403
    assert sessao.get(base + "/api/config", headers={"Host": "exemplo.com"}).status_code == 403


def test_busca_com_arquivos_enviados(painel):
    base, sessao, estado, _ = painel
    pdf = pdf_minimo("22/09/2026\nProcesso 48500.001234/2025-11\nInteressada: Energisa Tocantins")
    r = post(sessao, base + "/api/enviar?nome=pauta%2019.pdf", data=pdf)
    assert r.json() == {"ok": True, "nome": "pauta 19.pdf"}
    assert post(sessao, base + "/api/enviar?nome=virus.exe", data=b"x").status_code == 400
    assert sessao.get(base + "/api/relatorios").json()["enviados"] == ["pauta 19.pdf"]

    r = post(sessao, base + "/api/iniciar", json={"comando": "local", "usar_enviados": True})
    assert r.status_code == 200, r.text
    status = esperar_fim(sessao, base)
    assert status["ultima"]["situacao"] == "concluida"
    assert status["ultima"]["ocorrencias"] == 1
    assert any("Busca finalizada" in l["texto"] for l in status["logs"])

    resultados = sessao.get(base + "/api/resultados").json()
    assert [o["termo"] for o in resultados["ocorrencias"]] == ["Energisa Tocantins (ETO)"]

    arquivo = status["ultima"]["arquivo"]
    download = sessao.get(base + "/baixar", params={"arquivo": arquivo})
    assert download.status_code == 200 and download.content[:2] == b"PK"
    assert sessao.get(base + "/baixar", params={"arquivo": "../config.yaml"}).status_code == 400
    assert sessao.get(base + "/api/relatorios").json()["relatorios"][0]["nome"] == arquivo


def test_pasta_local_vazia_da_erro(painel):
    base, sessao, _, _ = painel
    r = post(sessao, base + "/api/iniciar", json={"comando": "local", "pasta": ""})
    assert r.status_code == 400 and "pasta" in r.json()["erro"].lower()


def test_parar_busca(painel, tmp_path):
    base, sessao, estado, _ = painel
    pasta = tmp_path / "muitos"
    pasta.mkdir()
    for i in range(300):
        (pasta / f"doc{i:03d}.pdf").write_bytes(pdf_minimo(f"Documento {i} Energisa Acre"))
    post(sessao, base + "/api/iniciar", json={"comando": "local", "pasta": str(pasta)})
    post(sessao, base + "/api/parar", json={})
    status = esperar_fim(sessao, base)
    assert status["ultima"]["situacao"] == "cancelada"
    assert status["ultima"]["documentos"] < 300
    assert Path(estado.saida / status["ultima"]["arquivo"]).exists()  # relatório parcial
