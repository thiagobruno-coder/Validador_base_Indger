# Busca nas Reuniões Públicas da ANEEL

Varre as **pautas e atas das Reuniões Públicas / Circuitos Deliberativos da Diretoria** e as
**listas de distribuição de processos** da ANEEL, e procura:

- **números de processo** que você acompanha (ex.: `48500.001234/2025-11`, com ou sem pontuação);
- **empresas do Grupo Energisa** (EMS, EMT, EPB, ESE, ETO, EAC, ESS, EMR, ERO, nomes antigos e o termo genérico "Energisa");
- **palavras-chave** livres (ex.: "revisão tarifária").

O resultado é uma planilha Excel com a data, a reunião, o documento, o item da pauta (aproximado),
o trecho onde o termo aparece e os links. Também são gerados CSVs.

## Como funciona

1. Lê as listagens `idAreaNoticia=425` (Pautas e Atas) e `424` (Distribuição de Processos), página por página, até o período configurado.
2. Abre a página de cada reunião e baixa os anexos (PDF, DOCX, XLSX, ZIP, HTML).
3. Extrai o texto e faz a busca sem diferenciar maiúsculas/minúsculas e acentos. As siglas (EMS, EMT…) só contam em MAIÚSCULAS e como palavra inteira, para evitar falsos positivos.
4. Gera `resultados/busca_aneel_AAAAMMDD_HHMMSS.xlsx` com as abas **Resumo**, **Ocorrências** e **Documentos analisados** (esta inclui os erros de leitura e todos os processos citados em cada documento).

> **Cloudflare:** o site `www2.aneel.gov.br` é protegido pelo Cloudflare, que exibe a tela "Um momento…".
> No modo padrão (`auto`), o programa tenta o acesso direto e, se for bloqueado, **abre o Chrome**,
> espera a verificação terminar (se aparecer a caixa "Confirme que você é humano", clique nela) e faz
> os downloads por dentro dessa janela. A sessão fica salva em `.perfil_navegador/`, então nas execuções
> seguintes a verificação normalmente não aparece de novo.

## Jeito mais fácil: o painel (Windows)

1. Instale o Python 3.10 ou superior em <https://www.python.org/downloads/> e marque **"Add python.exe to PATH"**.
2. Dê **dois cliques em `iniciar_painel.bat`**, dentro desta pasta.
   - Na primeira vez, ele prepara tudo sozinho, o que leva alguns minutos.
   - Depois, abre o painel no navegador (`http://127.0.0.1:8765`).
3. No painel:
   - **2. O que procurar:** cole os números de processo e as palavras-chave, revise as empresas e clique em **Salvar**.
   - **1. Buscar:** escolha o período e clique em **Iniciar busca**. Se o Chrome abrir pedindo verificação, aguarde ou clique na caixa e não feche a janela.
   - **3. Resultados:** veja as ocorrências com o trecho destacado, filtre por termo e clique em **Baixar Excel**.
   - Para buscar em PDFs que você já tem, escolha **"Em arquivos do meu computador"** e arraste os arquivos.
4. Para fechar, clique em **Encerrar painel** ou feche a janela preta.

O painel funciona só no seu computador e não fica acessível a outras pessoas da rede.
No Linux ou macOS, use `./iniciar_painel.sh`.

## Instalação manual (linha de comando)

1. Instale o Python 3.10 ou superior em <https://www.python.org/downloads/> e marque **"Add Python to PATH"**.
2. Abra o **Prompt de Comando** na pasta `busca_reunioes_aneel` e rode:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

O último comando só é necessário se você não tiver o Google Chrome instalado.

No Linux/macOS, use `source .venv/bin/activate` no lugar de `.venv\Scripts\activate`.

## Configuração

Edite o `config.yaml`:

```yaml
desde: "2026-01-01"
processos:
  - "48500.001234/2025-11"
  - "48500.005678/2026-02"
palavras_chave:
  - "revisão tarifária"
```

A lista de empresas da Energisa já vem preenchida. Você pode incluir ou remover nomes e siglas.

## Uso

```bat
:: Busca no site da ANEEL (período e termos do config.yaml)
python -m aneel_busca online

:: Só as pautas/atas, a partir de junho
python -m aneel_busca online --areas 425 --desde 2026-06-01

:: Período fechado e mais páginas
python -m aneel_busca online --desde 2025-01-01 --ate 2025-12-31 --max-paginas 30

:: Busca em arquivos já baixados (PDF, HTML salvo com Ctrl+S, DOCX…), incluindo subpastas
python -m aneel_busca local C:\caminho\da\pasta

:: Guardar também o texto extraído de cada documento, para conferência
python -m aneel_busca --salvar-textos online
```

Opções úteis do modo `online`:

| Opção | Para que serve |
|---|---|
| `--modo auto` | (padrão) acesso direto; abre o navegador se o Cloudflare bloquear |
| `--modo navegador` | usa sempre o navegador |
| `--modo requests` | nunca abre o navegador (falha se houver bloqueio) |
| `--sem-cache` | baixa tudo de novo |
| `-v` | mostra mensagens detalhadas |

Os documentos baixados ficam em cache (`.cache_aneel/`), então rodar de novo é rápido. A exceção são as
pautas **(PRÉVIA)** e as listagens, que sempre são baixadas de novo porque mudam.

### Variáveis de ambiente (opcionais)

- `HTTPS_PROXY`: proxy da rede corporativa (também é repassado ao navegador).
- `ANEEL_NAVEGADOR`: caminho de um Chrome/Chromium específico.

## Limitações conhecidas

- **PDF digitalizado** (imagem, sem texto) não é lido; aparece como erro na aba "Documentos analisados". Seria preciso OCR.
- **Arquivos `.doc` antigos** não são lidos; converta para `.docx` ou PDF.
- O **item da pauta** é identificado de forma aproximada, pela linha anterior que parece um item ("1.", "Item 2", "Processo:").
- As páginas de detalhe das reuniões foram modeladas sem um exemplo real. Se algum anexo não for encontrado, salve a página da reunião (Ctrl+S) e ajuste `site_aneel.py` (função `_parece_documento`).

## Testes

```bat
pip install pytest
python -m pytest
```

Os testes usam páginas reais de listagem salvas do site (`tests/fixtures/`) e simulam as páginas de detalhe e os anexos.

## Estrutura

```
busca_reunioes_aneel/
├── iniciar_painel.bat       ← dois cliques para abrir o painel (Windows)
├── iniciar_painel.sh        ← idem para Linux/macOS
├── config.yaml              ← termos de busca e período
├── requirements.txt
├── aneel_busca/
│   ├── __main__.py          ← linha de comando
│   ├── painel.py            ← servidor local do painel
│   ├── web/painel.html      ← página do painel
│   ├── executor.py          ← execução de uma busca completa (usada pelo painel e pela linha de comando)
│   ├── site_aneel.py        ← leitura das listagens e páginas das reuniões
│   ├── cliente.py           ← downloads (cache, novas tentativas, Cloudflare/navegador)
│   ├── extracao.py          ← texto de PDF, DOCX, XLSX, HTML, RTF, ZIP
│   ├── busca.py             ← identificação de processos, empresas e palavras-chave
│   ├── processamento.py     ← orquestra a varredura
│   └── relatorio.py         ← Excel e CSV
└── tests/
```
