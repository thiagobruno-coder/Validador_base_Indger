/* Coletor da ANEEL — roda na página do site da ANEEL, aberta no Chrome do próprio usuário,
   depois que ele passou pela verificação do Cloudflare. Baixa as listagens, as páginas das
   reuniões e os anexos (mesmo site) em ritmo moderado e envia cada conteúdo para o painel
   local, que decide o que baixar em seguida e faz a análise. */
(function () {
  "use strict";
  if (window.__coletorAneel) { window.__coletorAneel.mostrar(); return; }
  const script = document.currentScript || document.querySelector('script[src*="coletor.js"]');
  const PAINEL = new URL(script.src).origin;

  // ------------------------------------------------------------------ caixa de progresso
  const caixa = document.createElement("div");
  caixa.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;width:380px;max-width:calc(100vw - 32px);" +
    "background:#172029;color:#e3e9ef;font:13px/1.45 Segoe UI,Arial,sans-serif;border-radius:10px;" +
    "box-shadow:0 8px 30px rgba(0,0,0,.35);padding:14px 16px";
  caixa.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
    '<b style="font-size:14px">Coletor ANEEL</b><span id="cx-fechar" style="cursor:pointer;opacity:.7">✕</span></div>' +
    '<div id="cx-status">Conectando ao painel…</div>' +
    '<div style="background:#2a3644;border-radius:4px;height:6px;margin:10px 0"><div id="cx-barra" style="background:#4a9cf0;height:6px;border-radius:4px;width:0"></div></div>' +
    '<div id="cx-numeros" style="opacity:.8"></div>' +
    '<div style="margin-top:10px;display:flex;gap:8px"><button id="cx-parar" style="flex:1;padding:6px;border-radius:6px;border:1px solid #ff7b72;background:none;color:#ff7b72;cursor:pointer">Parar</button>' +
    '<a href="' + PAINEL + '/" target="_blank" style="flex:1;text-align:center;padding:6px;border-radius:6px;background:#4a9cf0;color:#08121c;text-decoration:none;font-weight:600">Abrir painel</a></div>';
  document.body.appendChild(caixa);
  const $ = (id) => caixa.querySelector("#" + id);
  let parar = false, docs = 0, ocorr = 0, erros = 0;
  $("cx-fechar").onclick = () => (caixa.style.display = "none");
  $("cx-parar").onclick = () => { parar = true; status("Parando…"); };
  window.__coletorAneel = { mostrar: () => (caixa.style.display = "block") };
  const status = (t) => ($("cx-status").textContent = t);
  const numeros = () => ($("cx-numeros").textContent = `${docs} documento(s) · ${ocorr} ocorrência(s)` + (erros ? ` · ${erros} erro(s)` : ""));
  const barra = (p) => ($("cx-barra").style.width = Math.max(0, Math.min(100, p)) + "%");
  const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------------------------ comunicação com o painel
  async function painel(caminho, corpo, params, tipo) {
    const url = PAINEL + caminho + (params ? "?" + new URLSearchParams(params) : "");
    const r = await fetch(url, { method: "POST", headers: { "X-Painel": "1", "Content-Type": tipo || "application/octet-stream" }, body: corpo });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || "Erro " + r.status + " no painel");
    if (j.parar) parar = true;
    return j;
  }
  async function baixar(url) {
    const r = await fetch(url, { credentials: "include" });
    const bytes = await r.arrayBuffer();
    const tipo = r.headers.get("content-type") || "";
    if (!r.ok) {
      const inicio = new TextDecoder().decode(bytes.slice(0, 3000)).toLowerCase();
      if (r.status === 403 && (inicio.includes("momento") || inicio.includes("moment") || inicio.includes("cf_chl"))) {
        throw new Error("VERIFICACAO");
      }
      throw new Error("HTTP " + r.status);
    }
    return { bytes, tipo };
  }

  // ------------------------------------------------------------------ coleta
  (async function () {
    let plano;
    try { plano = await painel("/api/coleta/inicio", "{}", null, "application/json"); }
    catch (e) {
      status("Não foi possível falar com o painel: " + e.message + ". Verifique se o painel está aberto " +
        "(iniciar_painel.bat) e, se o Chrome perguntar, permita o acesso à rede local.");
      return;
    }
    const periodo = (plano.desde || "início") + " até " + (plano.ate || "hoje");
    let feitas = 0;
    const total = plano.areas.length;
    try {
      for (const area of plano.areas) {
        // Mesmo caminho da lista, mas na origem da página aberta (evita diferenças http/https).
        const ref = new URL(area.url);
        let url = new URL(ref.pathname + ref.search, location.origin).href, pagina = 0;
        while (url && !parar) {
          pagina++;
          status(`${area.nome}: lendo a página ${pagina} da lista (${periodo})…`);
          const lista = await baixar(url);
          const L = await painel("/api/coleta/listagem", lista.bytes, { area: area.id, url });
          for (let i = 0; i < L.reunioes.length && !parar; i++) {
            const reu = L.reunioes[i];
            barra(((feitas + (pagina - 1 + i / Math.max(1, L.reunioes.length)) / plano.max_paginas) / total) * 100);
            status(`${area.nome}: ${reu.data} — ${reu.titulo.slice(0, 70)}`);
            await esperar(plano.intervalo_ms);
            const meta = { area: area.id, data: reu.data, titulo: reu.titulo };
            let det;
            try {
              const pag = await baixar(reu.url);
              det = await painel("/api/coleta/pagina", pag.bytes, { ...meta, url: reu.url });
              docs++; ocorr += det.ocorrencias || 0;
            } catch (e) {
              if (e.message === "VERIFICACAO") throw e;
              erros++; await painel("/api/coleta/erro", "", { ...meta, nome: "Página da reunião", url: reu.url, mensagem: e.message });
              numeros(); continue;
            }
            for (const anexo of det.anexos) {
              if (parar) break;
              const ameta = { ...meta, url_reuniao: reu.url, nome: anexo.nome, url: anexo.url };
              if (new URL(anexo.url, location.href).origin !== location.origin) {
                erros++;
                await painel("/api/coleta/erro", "", { ...ameta, mensagem: "Anexo em outro site: baixe pelo link e use a busca em arquivos" });
                numeros(); continue;
              }
              await esperar(plano.intervalo_ms);
              try {
                const arq = await baixar(anexo.url);
                const r = await painel("/api/coleta/anexo", arq.bytes, { ...ameta, tipo: arq.tipo });
                docs++; ocorr += r.ocorrencias || 0;
              } catch (e) {
                if (e.message === "VERIFICACAO") throw e;
                erros++; await painel("/api/coleta/erro", "", { ...ameta, mensagem: e.message });
              }
              numeros();
            }
            numeros();
          }
          url = L.proxima;
          if (url) await esperar(plano.intervalo_ms);
        }
        feitas++;
      }
    } catch (e) {
      if (e.message === "VERIFICACAO") {
        status("O site pediu a verificação de novo. Recarregue esta página (F5), passe pela verificação e clique no favorito outra vez. O que já foi coletado será salvo.");
      } else {
        status("Erro: " + e.message + ". O que já foi coletado será salvo.");
      }
      parar = true;
    }
    try {
      const fim = await painel("/api/coleta/fim", "{}", null, "application/json");
      barra(100);
      docs = fim.documentos - (fim.erros || 0); ocorr = fim.ocorrencias; erros = fim.erros || 0; numeros();
      status((parar ? "Coleta interrompida. " : "Coleta concluída! ") + `${fim.ocorrencias} ocorrência(s) em ${fim.documentos} documento(s). Veja os resultados no painel.`);
    } catch (e) { status("Erro ao finalizar: " + e.message); }
    $("cx-parar").style.display = "none";
  })();
})();
