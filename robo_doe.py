"""
Robô de prospecção de obras — XYZ Tecnologia em Concreto

Baixa o Diário Oficial do Estado do Pará (IOEPA), procura publicações de
licença ambiental ("torna público ... requereu/recebeu ... Licença") nas cidades
de Belém, Ananindeua, Marituba, Santa Isabel do Pará e Castanhal, dá uma nota
de "parece obra" e grava os leads na coleção `prospeccao_obras` do Firestore,
a mesma que o app usa.

Uso:
  python robo_doe.py                     # últimos 3 dias (padrão)
  python robo_doe.py --data 2026-09-30   # um dia específico
  python robo_doe.py --dias 10           # últimos 10 dias
  python robo_doe.py --pdf arquivo.pdf   # testar com um PDF local
  python robo_doe.py --dry-run           # não grava nada, só mostra

Variáveis de ambiente:
  FIREBASE_SERVICE_ACCOUNT  JSON da conta de serviço (obrigatório para gravar)
  NOTA_MINIMA               nota mínima para gravar (padrão 3)
  CONTATO_NOMINATIM         e-mail de contato exigido pela política do Nominatim (opcional)
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from pypdf import PdfReader
from urllib.parse import urljoin

COLECAO = "prospeccao_obras"
COLECAO_LOG = "robo_execucoes"
URL_DOE = "https://www.ioepa.com.br/arquivos/{a}/{a}.{m}.{d}.DOE.pdf"
FUSO_BELEM = timezone(timedelta(hours=-3))
NOTA_MINIMA = int(os.getenv("NOTA_MINIMA", "5"))

CIDADES = {
    "Belém": {"c": (-1.4558, -48.4902), "k": ["belem"]},
    "Ananindeua": {"c": (-1.3656, -48.3720), "k": ["ananindeua"]},
    "Marituba": {"c": (-1.3550, -48.3420), "k": ["marituba"]},
    "Santa Isabel do Pará": {"c": (-1.2986, -48.1606), "k": ["santa isabel"]},
    "Castanhal": {"c": (-1.2964, -47.9258), "k": ["castanhal"]},
}

PALAVRAS_OBRA = [
    # obra civil e engenharia — vocabulário dos extratos de contrato municipal
    ("execucao de obra", 5), ("obras civis", 5), ("servicos de engenharia", 4),
    ("construcao de edificios", 5), ("edificio", 4), ("condominio", 4), ("loteamento", 4),
    ("incorporacao", 4), ("residencial", 3), ("empreendimento", 3), ("galpao", 3),
    ("construcao", 3), ("construir", 4), ("reforma", 3), ("reformar", 4),
    ("conservacao e manutencao", 3), ("manutencao predial", 3), ("engenharia", 2),
    ("projeto executivo", 3), ("infraestrutura", 3), ("pavimentacao", 3),
    ("saneamento", 3), ("drenagem", 2), ("obra", 2), ("ampliacao", 2),
    ("implantacao", 2), ("shopping", 3), ("hospital", 2), ("supermercado", 2),
    ("armazem", 2), ("torre", 1), ("conjunto habitacional", 4), ("escola", 2),
    ("posto de saude", 3), ("unidade basica de saude", 3), ("sede administrativa", 2),
    ("galpao", 3), ("pontilhao", 3), ("guia e calcada", 3), ("terraplenagem", 3),
]
NAO_OBRA = [
    "comercio varejista", "comercio atacadista", "lava jato", "bovinocultura",
    "agrossilvipastoril", "telefonia", "estacao radio base", "desdobro de madeira",
    "oficina", "restaurante", "posto de combustiveis", "farmacia", "distribuicao",
    # compra de consumo não põe tijolo: saía "AQUISIÇÃO DE MEDICAMENTOS GERAIS"
    # como lead de nota 5 só por causa de "armazém" e "distribuição".
    "medicamento", "farmaceutic", "material de consumo", "material de expediente",
    "combate a dengue", "uniforme", "escritorio",
]

# Mero texto publicado no Diário, sem pedido nem obra: aviso de circulação e
# carga de servidor. Casavam com "torna público" e entravam como lead de nota 3,
# com nome vindo de texto corrido.
#
# "Extrato de contrato" NÃO entra aqui: nas prefeituras é exatamente o extrato
# que publica a contratação de obra — tirar daqui matava o lead bom.
BOILERPLATE = [
    "publicado no diario oficial", "publica no diario oficial", "edicao do diario",
    "aviso ao conselho", "notificacao", "edital de convocacao",
    "praca de publicacao", "via de publicacao",
    "nome de servidor", "matricula de servidor", "inscricao de 01",
    # "designacao" ficou de fora de propósito: no DOM de Belém toda tabela de
    # contrato traz "DESIGNAÇÃO DE GESTOR E FISCAL DO CONTRATO", e a penalidade
    # de -6 derrubava o próprio contrato bom.
    "exonera", "nomeacao", "posse de servidor",
]

SUFIXO_EMPRESA = r"\b(LTDA|EIRELI|S\/A|S\.A\.|SPE|EPP|ME|EMPRESA|CONSORCIO|COOPERATIVA)\b"
RE_ENDERECO = re.compile(
    r"\b(rodovia|rod\.|avenida|av\.?|rua|r\.|travessa|tv\.?|passagem|psg\.?|"
    r"alameda|estrada|conjunto|cj\.?|quadra|lote|br[- ]?\d{3})\b", re.I)


def a_endereco_valido(endereco):
    """Endereço de verdade: pelo menos uma via, não só a palavra 'Pará'."""
    return bool(endereco) and len(endereco) >= 8 and bool(RE_ENDERECO.search(endereco))


def eh_lead_qualificado(a):
    """Portão de qualidade.

    Um lead precisa de identidade de empresa: CNPJ ou sufixo societário
    (LTDA, SPE, EPP...). O que passava sem isso era texto corrido —
    "ublicado no Diário Oficial nº 36.582" virava `nome` e o bloco entrava
    com nota 3.

    Endereço deixou de ser obrigatório: extrato de contrato de prefeitura
    costuma publicar objeto e empresa sem o endereço do canteiro. Passa a
    ser bônus de nota, cobrado em `analisar_bloco`.
    """
    if not a["cidade"]:
        return False
    if not a["cnpj"] and not re.search(SUFIXO_EMPRESA, a["nome"] or "", re.I):
        return False
    return True


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------
def sem_acento(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def limpar_texto(t):
    t = t.replace("\r", "")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)  # junta palavra hifenizada na quebra
    t = re.sub(r"\n+", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


RE_TORNA = re.compile(r"torna(?:-se)?\s+p[uú]blic[oa]", re.I)


def separar_publicacoes(texto):
    t = limpar_texto(texto)
    partes = [p.strip() for p in re.split(r"Protocolo:\s*\d+", t, flags=re.I)]
    partes = [p for p in partes if len(p) > 40]
    out = []
    for p in partes:
        idx = [m.start() for m in RE_TORNA.finditer(p)]
        if len(idx) <= 1:
            out.append(p)
            continue
        for i in range(len(idx)):
            ini = 0 if i == 0 else max(idx[i - 1] + 80, idx[i] - 260)
            fim = len(p) if i == len(idx) - 1 else max(idx[i] + 60, idx[i + 1] - 260)
            out.append(p[ini:fim].strip())
    return out


def detectar_cidade(bloco):
    s = sem_acento(bloco)
    for nome, info in CIDADES.items():  # cidade junto do órgão ambiental tem preferência
        for k in info["k"]:
            if re.search(r"(sem{1,2}a|meio ambiente)[^.]{0,60}" + k, s):
                return nome
    melhor, pos = None, 10**9
    for nome, info in CIDADES.items():
        for k in info["k"]:
            i = s.find(k)
            if 0 <= i < pos:
                melhor, pos = nome, i
    return melhor


def _m(rx, txt, g=1, flags=re.I):
    m = re.search(rx, txt, flags)
    return (m.group(g) or "") if m else ""


def analisar_bloco(bloco, cidade=None, exigir_torna=True):
    if exigir_torna and not RE_TORNA.search(bloco):
        return None
    if not cidade:
        cidade = detectar_cidade(bloco)   # a fonte municipal já sabe a cidade
    if not cidade:
        return None
    s = sem_acento(bloco)
    sem_cpf = re.sub(r"\d{3}\.\d{3}\.\d{3}-\d{2}", "[CPF omitido]", bloco)

    cnpj = _m(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", bloco)
    nome = _m(r"([A-ZÀ-Ü0-9][A-ZÀ-Ü0-9&.,/\- ]{3,90}?\b(?:LTDA|S/A|S\.A\.?|EIRELI|EPP|ME|SPE)\b\.?)", bloco, flags=0)
    if not nome:  # extrato de prefeitura solta o nome em caixa mista
        nome = _m(r"([^.;\n]{3,90}?\b(?:Ltda|LTDA|S/A|S\.A\.?|Eireli|EPP|SPE)\b\.?)", bloco, flags=re.I)
    if not nome and cnpj:
        # A empresa é o que vem logo antes do CNPJ. Pego daí em vez de uma
        # janela solta do bloco, que acabava trazendo "DE LONA LTDA" no meio
        # de uma lista de itens.
        pos = bloco.find(cnpj)
        antes = bloco[max(0, pos - 140):pos]
        nome = (_m(r"([A-ZÀ-Ü0-9][A-ZÀ-Ü0-9&.,/\- ]{3,90}?\b(?:LTDA|S/A|S\.A\.?|EIRELI|EPP|ME|SPE)\b\.?)\s*[,:]?\s*$",
                   antes, flags=0)
                or _m(r"([^.;]{3,90}?\b(?:Ltda|LTDA|S/A|S\.A\.?|Eireli|EPP|SPE)\b\.?)\s*[,:]?\s*$",
                      antes, flags=re.I))
    if not nome and cnpj:
        pre = re.split(r",?\s*(?:CNPJ|CPF|torna)", bloco, flags=re.I)[0]
        nome = pre[-80:].strip()
    nome = re.sub(r"^[\s,.\-]+", "", nome)[:90]

    licenca = ""
    tipo = _m(r"Licen[çc]a\s+(?:Ambiental\s+)?(?:de\s+)?(Pr[ée]via|Instala[çc][ãa]o|Opera[çc][ãa]o)", bloco)
    if tipo:
        t = sem_acento(tipo)
        licenca = "LP" if t.startswith("pr") else "LI" if t.startswith("i") else "LO"
    if not licenca:
        licenca = _m(r"\b(LP|LI|LO|LAR)\b", bloco, flags=0)
    if re.search(r"\bLP\b.*\bLI\b|Pr[ée]via\s+e\s+(?:a\s+)?(?:Licen[çc]a\s+)?(?:de\s+)?Instala", bloco, re.I):
        licenca = "LP+LI"
    situacao = "Recebeu" if re.search(r"recebeu|obteve", bloco, re.I) else (
        "Requereu" if re.search(r"requer|solicit", bloco, re.I) else "")

    atividade = (_m(r"atividades?\s+(?:de\s*:?\s*)?(?:\d{2}\.\d{2}-\d-\d{2}\s*-\s*)?([^.;]{5,150})", bloco)
                 or _m(r"que tem por objeto\s+([^.;]{10,220})", bloco)
                 or _m(r"objeto\s*(?:do\s+contrato\s*)?[:\-\s]+\s*(?:o\s+presente\s+|o\s+objeto\s+do\s+presente\s+)?([^.;]{10,220})", bloco)
                 or _m(r"para\s+(?:a\s+)?((?:implanta[çc][ãa]o|constru[çc][ãa]o|instala[çc][ãa]o|amplia[çc][ãa]o)[^.;]{3,150})", bloco))
    atividade = re.sub(r",?\s*(através|atraves|sob|por meio|mediante|com validade|no munic).*$", "", atividade, flags=re.I).strip()

    endereco = _m(r"((?:Rodovia|Rod\.|Avenida|Av\.?|Rua|R\.|Travessa|Tv\.?|Passagem|Psg\.?|Alameda|Estrada|Conjunto|Cj\.?|BR[- ]?\d{3})\s[^;]{4,140}?)(?=,?\s*(?:CEP|torna|Bairro|\d{5}-?\d{3}|CNPJ)|\.\s)", bloco).strip()
    bairro = _m(r"Bairro:?\s*([A-ZÀ-Üa-zà-ü ]{3,40}?)(?=[,.;]|\s+CEP|\s+\d|$)", bloco).strip()
    cep = _m(r"\b(\d{2}\.?\d{3}-\d{3})\b", bloco)

    nota = 0
    for p, v in PALAVRAS_OBRA:
        if p in s:
            nota += v
    for p in NAO_OBRA:
        if p in s:
            nota -= 4
    if any(b in s for b in BOILERPLATE):
        nota -= 6          # texto do próprio Diário, não um pedido de obra
    if licenca in ("LP", "LI", "LP+LI"):
        nota += 3  # antes/durante a obra
    if licenca == "LO":
        nota -= 2  # já está operando
    if re.search(r"renova", bloco, re.I):
        nota -= 2
    if a_endereco_valido(endereco):
        nota += 2          # dizer onde é dá confiança ao lead
    prioridade = "Quente" if nota >= 7 else "Morno" if nota >= 3 else "Frio"

    a = dict(cidade=cidade, nome=nome, cnpj=cnpj, licenca=licenca, situacao=situacao,
             atividade=atividade, endereco=endereco, bairro=bairro, cep=cep,
             score=nota, prioridade=prioridade, trecho=sem_cpf[:900])
    if not eh_lead_qualificado(a):
        return None
    return a


def analisar_texto(texto):
    achados = [a for a in (analisar_bloco(b) for b in separar_publicacoes(texto)) if a]
    return sorted(achados, key=lambda a: -a["score"])


# ----------------------------------------------------------------------------
# Fontes municipais — o DOM de Belém e o de Ananindeua não usam "torna público",
# então em vez de partir por Protocolo o texto é cortado em janelas ancoradas
# nos títulos que trazem contrato e objeto. Janela larga de propósito: objeto e
# empresa costumam dividir bloco com o endereço.
# ----------------------------------------------------------------------------
ANCORAS_OBRA = re.compile(
    r"extrato de contrato|contrata[çc][ãa]o de empresa|contrato administrativo|"
    r"termo de contrato|objeto\s*:|processo licitat[óo]rio|preg[ãa]o eletr[ôo]nico|"
    r"extrato da ata|edital de licita[çc][ãa]o|aditivo|servi[çc]os? comuns de engenharia",
    re.I)


def janelas_municipais(texto, antes=450, depois=1700):
    """Corta em janelas ancoradas nos títulos de contrato.

    `antes` existe porque o nome da empresa vem antes do título: no DOM de
    Belém o texto é "...e a seguinte empresa CONSTRUTORA SOBERANA LTDA do
    Contrato Administrativo nº 006/2026" — começando no "Contrato" o nome
    ficava fora e o lead reprovava no portão de identidade.
    """
    idx = [m.start() for m in ANCORAS_OBRA.finditer(texto)]
    if not idx:
        return []
    trechos, ini, fim = [], max(0, idx[0] - antes), idx[0] + depois
    for i in idx[1:]:
        if i - antes <= fim:                 # sobrepõe: junta em uma só
            fim = max(fim, i + depois)
        else:
            trechos.append((ini, fim))
            ini, fim = max(0, i - antes), i + depois
    trechos.append((ini, fim))
    return [texto[a:min(b, len(texto))] for a, b in trechos if b - a > 60]


def analisar_municipal(texto, cidade_fonte):
    achados = []
    for trecho in janelas_municipais(texto):
        # limpar_texto junta "ADE-\nQUAÇÃO" em "ADEQUAÇÃO" e tira as quebras:
        # sem isso o nome saía com \n no meio e o regex de atividade não casava.
        a = analisar_bloco(limpar_texto(trecho), cidade=cidade_fonte, exigir_torna=False)
        if a:
            achados.append(a)
    # janelas vizinhas se sobrepõem e podem repetir o mesmo contrato
    vistos, unicos = set(), []
    for a in sorted(achados, key=lambda x: -x["score"]):
        chave = a["cnpj"] or sem_acento(re.sub(r"[^a-z0-9]", "", (a["nome"] or "")[:40]))
        if chave and chave not in vistos:
            vistos.add(chave)
            unicos.append(a)
    return unicos


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------
def baixar_pdf(data):
    url = URL_DOE.format(a=data.strftime("%Y"), m=data.strftime("%m"), d=data.strftime("%d"))
    r = requests.get(url, timeout=120, headers={"User-Agent": "XYZ-Prospeccao/1.0"})
    if r.status_code == 404:
        return None, url
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        return None, url  # dia sem edição costuma devolver página HTML
    return r.content, url


def texto_do_pdf(conteudo):
    """Extrai só as páginas que têm publicações de terceiros ('torna público')."""
    leitor = PdfReader(io.BytesIO(conteudo))
    paginas, total = [], len(leitor.pages)
    for pg in leitor.pages:
        try:
            t = pg.extract_text() or ""
        except Exception:
            continue
        if RE_TORNA.search(t):
            paginas.append(t)
    return "\n".join(paginas), total, len(paginas)


def texto_pdf_bruto(conteudo, max_paginas=60):
    """Todo o texto do PDF: municipal não tem 'torna público' que sirva de filtro."""
    leitor = PdfReader(io.BytesIO(conteudo))
    paginas, total = [], len(leitor.pages)
    for i, pg in enumerate(leitor.pages):
        if i >= max_paginas:
            break
        try:
            paginas.append(pg.extract_text() or "")
        except Exception:
            continue
    return "\n".join(paginas), total, len(paginas)


# ----------------------------------------------------------------------------
# Fontes: DOM municipal de Belém e de Ananindeua
# ----------------------------------------------------------------------------
API_BELEM = "https://sistemas.belem.pa.gov.br/diario-consulta-api/diarios"
URL_ANANINDEUA = "https://ananindeua.pa.gov.br/diario_oficial"
FONTE_BELEM = "DOM Belém (PMB)"
FONTE_ANANINDEUA = "DOM Ananindeua (PMA)"
HEADERS = {"User-Agent": "XYZ-Prospeccao/1.0"}


def datas_janela(datas):
    return {d.date() for d in datas}


def fonte_belem(datas):
    """(data, id) das edições de Belém que caem na janela pedida."""
    alvo = datas_janela(datas)
    if not alvo:
        return []
    params = {"dataRecebidoInicio": min(alvo).isoformat(),
              "dataRecebidoFim": (max(alvo) + timedelta(days=3)).isoformat()}
    try:
        r = requests.get(API_BELEM, params=params, timeout=60, headers=HEADERS)
        r.raise_for_status()
        docs = r.json()["response"]["docs"]
    except Exception as e:
        print(f"  {FONTE_BELEM}: lista indisponível ({e})")
        return []
    fora = []
    for d in docs:
        try:
            dt = datetime.strptime(d["data_publicacao"][:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if dt in alvo:
            fora.append((dt, d["id"]))
    return fora


def baixar_belem(ed_id):
    """A rota do PDF é a mesma do detalhe, só que com Accept: octet-stream."""
    r = requests.get(f"{API_BELEM}/{ed_id}", timeout=120,
                     headers={"Accept": "application/octet-stream", **HEADERS})
    r.raise_for_status()
    return r.content if r.content.startswith(b"%PDF") else None


def fonte_ananindeua(datas):
    """(data, url) dos PDFs de Ananindeua na janela pedida."""
    alvo = datas_janela(datas)
    if not alvo:
        return []
    try:
        r = requests.get(URL_ANANINDEUA, timeout=60, headers=HEADERS)
        r.raise_for_status()
        # O servidor não declara charset; requests então lê como latin-1 e
        # "Publicação" vira "PublicaÃ§Ã£o", derrubando o regex da data.
        html = r.content.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  {FONTE_ANANINDEUA}: lista indisponível ({e})")
        return []
    fora = []
    for pedaco in re.split(r'class="item_lic(?:\s|")', html):
        link = re.search(r'href="([^"]+\.pdf)"', pedaco)
        data = re.search(r"Data da Publica[çc][ãa]o:\s*(\d{2}/\d{2}/\d{4})", pedaco, re.I)
        if not (link and data):
            continue
        dt = datetime.strptime(data.group(1), "%d/%m/%Y").date()
        if dt in alvo:
            fora.append((dt, urljoin(URL_ANANINDEUA, link.group(1))))
    return fora


def baixar_ananindeua(url):
    r = requests.get(url, timeout=120, headers=HEADERS)
    r.raise_for_status()
    return r.content if r.content.startswith(b"%PDF") else None


# ----------------------------------------------------------------------------
# Geocodificação (Nominatim / OpenStreetMap — máx. 1 consulta por segundo)
# ----------------------------------------------------------------------------
def geocodificar(endereco, bairro, cidade):
    ua = "XYZ-Prospeccao-Robo/1.0"
    contato = os.getenv("CONTATO_NOMINATIM")
    if contato:
        ua += f" ({contato})"
    tentativas = [", ".join(x for x in (endereco, bairro, cidade, "Pará", "Brasil") if x)]
    if bairro:
        tentativas.append(", ".join((bairro, cidade, "Pará", "Brasil")))
    for i, q in enumerate(tentativas):
        if q.startswith(cidade):
            continue
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"format": "json", "limit": 1, "countrycodes": "br", "q": q},
                             headers={"User-Agent": ua, "Accept-Language": "pt-BR"}, timeout=30)
            j = r.json() if r.ok else []
            time.sleep(1.1)
            if j:
                return {"lat": float(j[0]["lat"]), "lng": float(j[0]["lon"]), "posAprox": i > 0}
        except Exception:
            time.sleep(1.1)
    lat, lng = CIDADES[cidade]["c"]
    return {"lat": lat, "lng": lng, "posAprox": True}


# ----------------------------------------------------------------------------
# Firestore
# ----------------------------------------------------------------------------
def conectar_firestore():
    import firebase_admin
    from firebase_admin import credentials, firestore
    bruto = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if not bruto:
        sys.exit("ERRO: defina FIREBASE_SERVICE_ACCOUNT com o JSON da conta de serviço (ou use --dry-run).")
    cred = credentials.Certificate(json.loads(bruto))
    firebase_admin.initialize_app(cred)
    return firestore.client()


def id_do_lead(a):
    """ID fixo por publicação: o mesmo lead nunca entra duas vezes nem sobrescreve o que a equipe editou."""
    chave = "|".join([a["cnpj"] or a["nome"], sem_acento(a["atividade"])[:80], a["cidade"], a["licenca"]])
    return "doe_" + hashlib.sha1(chave.encode()).hexdigest()[:20]


def gravar(db, a, data, url, fonte="DOE-PA (IOEPA)"):
    from google.api_core.exceptions import AlreadyExists
    ref = db.collection(COLECAO).document(id_do_lead(a))
    if ref.get().exists:
        return False
    doc = {
        "nome": a["nome"], "cnpj": a["cnpj"], "endereco": a["endereco"], "bairro": a["bairro"],
        "cidade": a["cidade"], "atividade": a["atividade"], "licenca": a["licenca"],
        "situacao": a["situacao"], "prioridade": a["prioridade"], "status": "Novo",
        "origem": fonte, "fonte": f"{fonte} — robô", "urlFonte": url,
        "dataPublicacao": data.strftime("%Y-%m-%d"), "trecho": a["trecho"],
        "fase": "Ainda não iniciada", "porte": "Grande" if a["score"] >= 7 else "Média",
        "nota": a["score"], "criadoEm": datetime.now(timezone.utc).isoformat(), "criadoPor": "robo",
    }
    doc.update(geocodificar(a["endereco"], a["bairro"], a["cidade"]))
    try:
        ref.create(doc)
        return True
    except AlreadyExists:
        return False


# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------
def processar(texto, data, url, db, dry, fonte="DOE-PA (IOEPA)", modo="doe", cidade_fonte=None):
    if modo == "municipal":
        base = analisar_municipal(texto, cidade_fonte)
    else:
        base = analisar_texto(texto)

    achados, vistos = [], set()  # a mesma publicação pode sair repetida no Diário
    for a in base:
        k = id_do_lead(a)
        if k not in vistos:
            vistos.add(k)
            achados.append(a)
    bons = [a for a in achados if a["score"] >= NOTA_MINIMA]
    novos = 0
    for a in bons:
        marca = f"[{a['prioridade']:6} {a['score']:>3}] {a['cidade']:<20} {a['licenca']:<5} {a['nome'][:45]} — {a['atividade'][:50]}"
        if dry:
            print("  (teste)", marca)
            continue
        if gravar(db, a, data, url, fonte):
            novos += 1
            print("  NOVO   ", marca)
        else:
            print("  já tem ", marca)
    return len(achados), len(bons), novos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AAAA-MM-DD (padrão: hoje no horário de Belém)")
    ap.add_argument("--dias", type=int, default=3, help="quantos dias para trás verificar (padrão 3)")
    ap.add_argument("--pdf", help="processar um PDF local em vez de baixar")
    ap.add_argument("--dry-run", action="store_true", help="não grava no Firestore")
    args = ap.parse_args()

    db = None if args.dry_run else conectar_firestore()
    resumo = []

    if args.pdf:
        data = datetime.strptime(args.data, "%Y-%m-%d") if args.data else datetime.now(FUSO_BELEM)
        with open(args.pdf, "rb") as f:
            texto, tot, pgs = texto_do_pdf(f.read())
        print(f"{args.pdf}: {tot} páginas, {pgs} com publicações de terceiros")
        resumo.append((data, *processar(texto, data, args.pdf, db, args.dry_run)))
    else:
        if args.data:
            datas = [datetime.strptime(args.data, "%Y-%m-%d")]
        else:
            hoje = datetime.now(FUSO_BELEM)
            datas = [hoje - timedelta(days=i) for i in range(args.dias)]
        for data in datas:
            conteudo, url = baixar_pdf(data)
            if not conteudo:
                print(f"{data:%d/%m/%Y}: sem edição ({url})")
                continue
            texto, tot, pgs = texto_do_pdf(conteudo)
            print(f"{data:%d/%m/%Y}: {tot} páginas, {pgs} com publicações de terceiros")
            resumo.append((data, *processar(texto, data, url, db, args.dry_run)))

        # Fontes municipais: mesma janela, cidade já conhecida pela origem.
        municipais = ([(FONTE_BELEM, "Belém", dt, f"{API_BELEM}/{i}", baixar_belem, i)
                       for dt, i in fonte_belem(datas)]
                      + [(FONTE_ANANINDEUA, "Ananindeua", dt, u, baixar_ananindeua, u)
                         for dt, u in fonte_ananindeua(datas)])
        if not municipais:
            print("  (nenhuma edição municipal na janela)")
        for fonte, cidade, dt, url, baixa, chave in sorted(municipais, key=lambda x: x[2]):
            try:
                conteudo = baixa(chave)
            except Exception as e:
                print(f"  {fonte} {dt:%d/%m/%Y}: falhou ao baixar ({e})")
                continue
            if not conteudo:
                print(f"  {fonte} {dt:%d/%m/%Y}: sem PDF")
                continue
            texto, tot, pgs = texto_pdf_bruto(conteudo)
            print(f"  {fonte} {dt:%d/%m/%Y}: {tot} páginas")
            resumo.append((dt, *processar(texto, dt, url, db, args.dry_run,
                                          fonte=fonte, modo="municipal", cidade_fonte=cidade)))

    for data, tot, bons, novos in resumo:
        print(f"Resumo {data:%d/%m/%Y}: {tot} publicações das 5 cidades, {bons} parecem obra, {novos} novas gravadas")

    if db and resumo:
        db.collection(COLECAO_LOG).add({
            "quando": datetime.now(timezone.utc).isoformat(),
            "dias": [{"data": d.strftime("%Y-%m-%d"), "publicacoes": t, "obras": b, "novas": n} for d, t, b, n in resumo],
        })


if __name__ == "__main__":
    main()
