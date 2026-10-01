import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from lxml import etree
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, black, red, white
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 21.0, 29.7
CONSULTA_URL = "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave="
IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios/{}"
CINZA = Color(0.95, 0.95, 0.95)
FONTE = "Helvetica"
FONTE_B = "Helvetica-Bold"
COL = (0.30, 5.41, 10.51, 15.62)
W = 5.09
FULL = 20.40
H = 0.65
BOTTOM = 29.40

UF_POR_IBGE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL", "28": "SE", "29": "BA",
    "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS",
    "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}
AMB_GERADOR = {"1": "Prefeitura", "2": "Sefin Nacional"}
TP_AMB = {"1": "Produção", "2": "Homologação"}
TP_EMIT = {"1": "Prestador", "2": "Tomador", "3": "Intermediário"}
SITUACAO = {"100": "NFS-e Gerada", "101": "NFS-e de Substituição", "102": "NFS-e de Decisão Judicial", "103": "NFS-e Avulsa"}
FINALIDADE = {"0": "NFS-e regular"}
OP_SIMP_NAC = {"1": "Não Optante", "2": "Optante - Microempreendedor Individual (MEI)", "3": "Optante - Microempresa ou Empresa de Pequeno Porte (ME/EPP)"}
REG_AP_TRIB_SN = {
    "1": "Regime de apuração dos tributos federais e municipal pelo Simples Nacional",
    "2": "Regime de apuração dos tributos federais pelo SN e ISSQN por fora do SN",
    "3": "Regime de apuração dos tributos federais e municipal por fora do SN",
}
TRIB_ISSQN = {"1": "Operação Tributável", "2": "Imunidade", "3": "Exportação de Serviço", "4": "Não Incidência"}
TP_RET_ISSQN = {"1": "Não Retido", "2": "Retido pelo Tomador", "3": "Retido pelo Intermediário"}
REG_ESP_TRIB = {
    "0": "Nenhum", "1": "Ato Cooperado (Cooperativa)", "2": "Estimativa", "3": "Microempresa Municipal",
    "4": "Notário ou Registrador", "5": "Profissional Autônomo", "6": "Sociedade de Profissionais", "9": "Outros",
}
TP_RET_PIS_COFINS = {"0": "PIS/COFINS/CSLL Não Retidos"}


def _path(p):
    return "/".join(f"*[local-name()='{s}']" for s in p.split("/"))


def _no(base, p):
    if base is None:
        return None
    r = base.xpath(_path(p))
    return r[0] if r else None


def _v(base, p):
    n = _no(base, p)
    return (n.text or "").strip() if n is not None else ""


def _qualquer(base, tag):
    if base is None:
        return ""
    r = base.xpath(f".//*[local-name()='{tag}']")
    return (r[0].text or "").strip() if r else ""


def _dec(v):
    try:
        return Decimal(v)
    except (InvalidOperation, TypeError):
        return None


def _num(x, casas=2):
    s = f"{x:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _money(v):
    d = _dec(v)
    return f"R$ {_num(d)}" if d is not None else ""


def _pct(v):
    d = _dec(v)
    return f"{_num(d)}%" if d is not None else ""


def _doc(v):
    d = re.sub(r"[^0-9A-Za-z]", "", v or "").upper()
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11 and d.isdigit():
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return v or ""


def _cep(v):
    d = re.sub(r"\D", "", v or "")
    return f"{d[:2]}.{d[2:5]}-{d[5:]}" if len(d) == 8 else v or ""


def _fone(v):
    d = re.sub(r"\D", "", v or "")
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    return v or ""


def _data(v):
    try:
        return datetime.fromisoformat(v).strftime("%d/%m/%Y")
    except ValueError:
        return v or ""


def _data_hora(v):
    try:
        return datetime.fromisoformat(v).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return v or ""


def _c_trib_nac(v):
    d = re.sub(r"\D", "", v or "")
    return f"{d[:2]}.{d[2:4]}.{d[4:]}" if len(d) == 6 else v or ""


def _nbs(v):
    d = re.sub(r"\D", "", v or "")
    return f"{d[0]}.{d[1:5]}.{d[5:7]}.{d[7:]}" if len(d) == 9 else v or ""


def _traco(v):
    return v if v else "-"


def _ibge(cod, cache_path):
    cache = {}
    if cache_path and Path(cache_path).exists():
        cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    if cod in cache:
        return cache[cod]
    try:
        r = requests.get(IBGE_URL.format(cod), timeout=8)
        r.raise_for_status()
        dados = r.json()
        nome = (dados[0] if isinstance(dados, list) else dados)["nome"]
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        return ""
    if cache_path:
        cache[cod] = nome
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return nome


def _mun_uf(cod, cache, nome="", uf=""):
    if not cod and not nome:
        return ""
    uf = uf or UF_POR_IBGE.get((cod or "")[:2], "")
    nome = nome or (_ibge(cod, cache) if cod else "") or cod
    return f"{nome} / {uf}" if uf else nome


def _soma(*valores):
    ds = [_dec(v) for v in valores if v]
    ds = [d for d in ds if d is not None]
    return str(sum(ds)) if ds else ""


def _pessoa(no, cache):
    if no is None:
        return None
    doc = _v(no, "CNPJ") or _v(no, "CPF") or _v(no, "NIF")
    nac = _no(no, "end/endNac")
    ext = _no(no, "end/endExt")
    if nac is not None:
        cod = _v(nac, "cMun")
        mun = _mun_uf(cod, cache)
        ibge_cep = f"{cod} / {_cep(_v(nac, 'CEP'))}"
    elif ext is not None:
        mun = " / ".join(x for x in (_v(ext, "xCidade"), _v(ext, "xEstProvReg")) if x)
        ibge_cep = f"- / {_v(ext, 'cEndPost')}"
    else:
        mun = ibge_cep = ""
    return {
        "doc": _doc(doc),
        "im": _v(no, "IM"),
        "fone": _fone(_v(no, "fone")),
        "nome": _v(no, "xNome"),
        "mun": mun,
        "ibge_cep": ibge_cep,
        "end": ", ".join(x for x in (_v(no, "end/xLgr"), _v(no, "end/nro"), _v(no, "end/xCpl"), _v(no, "end/xBairro")) if x),
        "email": _v(no, "email"),
    }


def _prestador(inf, dps, cache):
    emit = _no(inf, "emit")
    prest = _no(dps, "prest")
    cod = _v(emit, "enderNac/cMun")
    nome_conhecido = _v(inf, "xLocEmi") if cod and cod == _v(dps, "cLocEmi") else ""
    uf = _v(emit, "enderNac/UF")
    reg = _no(prest, "regTrib")
    return {
        "doc": _doc(_v(emit, "CNPJ") or _v(emit, "CPF") or _v(emit, "NIF") or _v(prest, "CNPJ") or _v(prest, "CPF")),
        "im": _v(emit, "IM") or _v(prest, "IM"),
        "fone": _fone(_v(emit, "fone") or _v(prest, "fone")),
        "nome": _v(emit, "xNome") or _v(prest, "xNome"),
        "mun": _mun_uf(cod, cache, nome_conhecido, uf),
        "ibge_cep": f"{cod} / {_cep(_v(emit, 'enderNac/CEP'))}" if cod else "",
        "end": ", ".join(x for x in (_v(emit, "enderNac/xLgr"), _v(emit, "enderNac/nro"), _v(emit, "enderNac/xCpl"), _v(emit, "enderNac/xBairro")) if x),
        "email": _v(emit, "email") or _v(prest, "email"),
        "op_simp_nac": OP_SIMP_NAC.get(_v(reg, "opSimpNac"), _v(reg, "opSimpNac")),
        "reg_ap_trib_sn": REG_AP_TRIB_SN.get(_v(reg, "regApTribSN"), _v(reg, "regApTribSN")),
        "reg_esp_trib": _v(reg, "regEspTrib"),
    }


def _totais_aprox(dps):
    tot = _no(dps, "valores/trib/totTrib")
    prefixo = "Totais Aproximados dos Tributos cfe. Lei nº 12.741/2012: "
    if _no(tot, "vTotTrib") is not None:
        f = [_traco(_money(_v(tot, f"vTotTrib/{k}"))) for k in ("vTotTribFed", "vTotTribEst", "vTotTribMun")]
    elif _no(tot, "pTotTrib") is not None:
        f = [_traco(_pct(_v(tot, f"pTotTrib/{k}"))) for k in ("pTotTribFed", "pTotTribEst", "pTotTribMun")]
    elif _v(tot, "pTotTribSN"):
        return prefixo + f"Simples Nacional: {_pct(_v(tot, 'pTotTribSN'))}"
    else:
        f = ["-", "-", "-"]
    return prefixo + f"Federais: {f[0]} ; Estaduais: {f[1]} ; Municipais: {f[2]}"


def _info_complementar(dps, inf):
    itens = [
        ("Inf. Cont.: ", _qualquer(dps, "xInfComp")),
        ("NFS-e Subst.: ", _qualquer(dps, "chSubstda")),
        ("Doc. Ref.: ", _qualquer(dps, "docRef")),
        ("Cod. Obra: ", _qualquer(dps, "cObra")),
        ("Insc. Imob.: ", _qualquer(dps, "inscImobFisc")),
        ("Cod. Evt.: ", _qualquer(dps, "idAtvEvt")),
        ("Doc. Tec.: ", _qualquer(dps, "idDocTec")),
        ("Núm. Ped.: ", _qualquer(dps, "xPed")),
        ("Item Ped.: ", _qualquer(dps, "xItemPed")),
        ("Inf. A. T. Mun.: ", _qualquer(inf, "xOutInf") or _qualquer(dps, "xOutInf")),
    ]
    partes = [rot + val for rot, val in itens if val]
    return " | ".join(partes + [_totais_aprox(dps)])


def _extrair(xml, cache):
    root = etree.fromstring(xml)
    infs = root.xpath("//*[local-name()='infNFSe']")
    if not infs:
        raise ValueError("XML sem infNFSe")
    inf = infs[0]
    dps = _no(inf, "DPS/infDPS")
    if dps is None:
        raise ValueError("XML sem DPS")
    ibs_dps = _no(dps, "IBSCBS")
    ibs_nfse = _no(inf, "IBSCBS")
    chave = re.sub(r"^NFS", "", inf.get("Id", ""))

    cod_prest = _v(dps, "serv/locPrest/cLocPrestacao")
    cod_incid = _v(inf, "cLocIncid")
    pais = _v(dps, "serv/locPrest/cPaisPrestacao") or "BR"
    local_prest = _mun_uf(cod_prest, cache, _v(inf, "xLocPrestacao"))
    local_incid = _mun_uf(cod_incid, cache, _v(inf, "xLocIncid"))

    dest = _pessoa(_no(ibs_dps, "dest"), cache)
    ind_dest = _v(ibs_dps, "indDest")
    if dest is not None:
        dest_aviso = None
    elif ind_dest == "0":
        dest_aviso = "O DESTINATÁRIO É O PRÓPRIO TOMADOR/ADQUIRENTE DA OPERAÇÃO"
    else:
        dest_aviso = "DESTINATÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e"

    g = lambda p: _v(ibs_nfse, p)
    red_aliq = [g("valores/uf/pRedAliqUF"), g("valores/mun/pRedAliqMun"), g("valores/fed/pRedAliqCBS")]
    exclusoes = _soma(
        _v(dps, "valores/vDescCondIncond/vDescIncond"), g("valores/vCalcReeRepRes"), _v(inf, "valores/vISSQN"),
        _v(dps, "valores/trib/tribFed/piscofins/vPis"), _v(dps, "valores/trib/tribFed/piscofins/vCofins"),
    )
    ibs_cbs_total = _soma(g("totCIBS/gIBS/vIBSTot"), g("totCIBS/gCBS/vCBS"))
    cst = _v(ibs_dps, "valores/trib/gIBSCBS/CST")
    class_trib = _v(ibs_dps, "valores/trib/gIBSCBS/cClassTrib")
    c_ind_op = _v(ibs_dps, "cIndOp")
    local_ibs = " / ".join(x for x in (c_ind_op, g("cLocalidadeIncid"), g("xLocalidadeIncid"), UF_POR_IBGE.get(g("cLocalidadeIncid")[:2], "")) if x)
    federal = _no(dps, "valores/trib/tribFed")
    tp_ret_pc = _v(federal, "piscofins/tpRetPisCofins")
    ret_csll = _v(federal, "vRetCSLL")

    return {
        "chave": chave,
        "homolog": _v(dps, "tpAmb") == "2",
        "municipio_header": _mun_uf(_v(inf, "emit/enderNac/cMun"), cache, _v(inf, "xLocEmi"), _v(inf, "emit/enderNac/UF")),
        "amb_gerador": AMB_GERADOR.get(_v(inf, "ambGer"), _v(inf, "ambGer")),
        "tp_amb": TP_AMB.get(_v(dps, "tpAmb"), _v(dps, "tpAmb")),
        "n_nfse": _v(inf, "nNFSe"),
        "competencia": _data(_v(dps, "dCompet")),
        "dh_proc": _data_hora(_v(inf, "dhProc")),
        "n_dps": _v(dps, "nDPS"),
        "serie": _v(dps, "serie"),
        "dh_emi": _data_hora(_v(dps, "dhEmi")),
        "emitente": TP_EMIT.get(_v(dps, "tpEmit"), _v(dps, "tpEmit")),
        "situacao": SITUACAO.get(_v(inf, "cStat"), _v(inf, "cStat")),
        "finalidade": FINALIDADE.get(_v(ibs_dps, "finNFSe"), _v(ibs_dps, "finNFSe")),
        "prestador": _prestador(inf, dps, cache),
        "tomador": _pessoa(_no(dps, "toma"), cache),
        "destinatario": dest,
        "destinatario_aviso": dest_aviso,
        "intermediario": _pessoa(_no(dps, "interm"), cache),
        "c_trib": f"{_c_trib_nac(_v(dps, 'serv/cServ/cTribNac'))} / {_v(dps, 'serv/cServ/cTribMun') or '-'}",
        "nbs": _nbs(_v(dps, "serv/cServ/cNBS")),
        "local_prestacao": f"{local_prest} / {pais}" if local_prest else "",
        "desc_trib": _v(inf, "xTribMun") or _v(inf, "xTribNac"),
        "desc_servico": _v(dps, "serv/cServ/xDescServ"),
        "trib_issqn_cod": _v(dps, "valores/trib/tribMun/tribISSQN"),
        "trib_issqn": TRIB_ISSQN.get(_v(dps, "valores/trib/tribMun/tribISSQN"), _v(dps, "valores/trib/tribMun/tribISSQN")),
        "local_incidencia": f"{local_incid} / {pais}" if local_incid else "",
        "reg_esp_trib": REG_ESP_TRIB.get(_v(_no(dps, "prest/regTrib"), "regEspTrib"), _v(_no(dps, "prest/regTrib"), "regEspTrib")),
        "tp_imunidade": _v(dps, "valores/trib/tribMun/tpImunidade"),
        "tp_susp": _v(dps, "valores/trib/tribMun/exigSusp/tpSusp"),
        "n_processo": _v(dps, "valores/trib/tribMun/exigSusp/nProcesso"),
        "tp_bm": _v(inf, "valores/tpBM"),
        "calc_bm": _v(inf, "valores/vCalcBM") or _v(inf, "valores/vRedBCBM"),
        "total_ded": _v(dps, "valores/vDedRed/vDR") or _v(inf, "valores/vCalcDR"),
        "desc_incond": _v(dps, "valores/vDescCondIncond/vDescIncond"),
        "desc_cond": _v(dps, "valores/vDescCondIncond/vDescCond"),
        "bc_issqn": _v(inf, "valores/vBC"),
        "aliq_aplic": _v(inf, "valores/pAliqAplic"),
        "ret_issqn": TP_RET_ISSQN.get(_v(dps, "valores/trib/tribMun/tpRetISSQN"), _v(dps, "valores/trib/tribMun/tpRetISSQN")),
        "v_issqn": _v(inf, "valores/vISSQN"),
        "irrf": _v(federal, "vRetIRRF"),
        "contrib_prev": _v(federal, "vRetCP"),
        "contrib_sociais": _soma(ret_csll, _v(federal, "piscofins/vPis"), _v(federal, "piscofins/vCofins")) if tp_ret_pc == "1" else ret_csll,
        "pis": "0.00" if tp_ret_pc == "1" else _v(federal, "piscofins/vPis"),
        "cofins": "0.00" if tp_ret_pc == "1" else _v(federal, "piscofins/vCofins"),
        "desc_contrib": TP_RET_PIS_COFINS.get(tp_ret_pc, f"Código {tp_ret_pc}" if tp_ret_pc else ""),
        "cst_class": f"{cst} / {class_trib}" if cst or class_trib else "",
        "local_ibs": local_ibs,
        "exclusoes": exclusoes,
        "bc_ibs": g("valores/vBC"),
        "red_aliq": " / ".join(_pct(x) or "-" for x in red_aliq) if any(red_aliq) else "",
        "aliq_ibs": f"{_pct(g('valores/uf/pIBSUF')) or '-'} / {_pct(g('valores/mun/pIBSMun')) or '-'}" if g("valores/uf/pIBSUF") or g("valores/mun/pIBSMun") else "",
        "aliq_ef_mun": _pct(g("valores/mun/pAliqEfetMun")),
        "v_ibs_mun": g("totCIBS/gIBS/gIBSMunTot/vIBSMun"),
        "aliq_ef_uf": _pct(g("valores/uf/pAliqEfetUF")),
        "v_ibs_uf": g("totCIBS/gIBS/gIBSUFTot/vIBSUF"),
        "v_ibs_tot": g("totCIBS/gIBS/vIBSTot"),
        "p_cbs": _pct(g("valores/fed/pCBS")),
        "aliq_ef_cbs": _pct(g("valores/fed/pAliqEfetCBS")),
        "v_cbs": g("totCIBS/gCBS/vCBS"),
        "v_serv": _v(dps, "valores/vServPrest/vServ"),
        "total_ret": _v(inf, "valores/vTotalRet"),
        "v_liq": _v(inf, "valores/vLiq"),
        "total_ibs_cbs": ibs_cbs_total,
        "v_tot_nf": g("totCIBS/vTotNF"),
        "info": _info_complementar(dps, inf),
    }


def _elipse(texto, fonte, tam, largura):
    if stringWidth(texto, fonte, tam) <= largura:
        return texto
    while texto and stringWidth(texto + "...", fonte, tam) > largura:
        texto = texto[:-1]
    return texto + "..."


def _quebrar(texto, fonte, tam, largura, max_linhas):
    if not texto:
        return []
    linhas = []
    for paragrafo in str(texto).split("\n"):
        linhas += simpleSplit(paragrafo, fonte, tam, largura) or [""]
    if len(linhas) > max_linhas:
        linhas = linhas[:max_linhas]
        linhas[-1] = _elipse(linhas[-1] + "...", fonte, tam, largura) if not linhas[-1].endswith("...") else linhas[-1]
    return [_elipse(l, fonte, tam, largura) if stringWidth(l, fonte, tam) > largura else l for l in linhas]


def _rect(c, x, top, w, h, shade=False):
    c.setLineWidth(0.5)
    c.setStrokeColor(black)
    c.setFillColor(CINZA if shade else white)
    c.rect(x * cm, (PAGE_H - top - h) * cm, w * cm, h * cm, stroke=1, fill=1)
    c.setFillColor(black)


def _y(top, off):
    return (PAGE_H - top - off) * cm


def _cell(c, x, top, w, h, label, value, shade=False, ls=6, vs=7, max_linhas=1):
    _rect(c, x, top, w, h, shade)
    c.setFillColor(black)
    if label:
        c.setFont(FONTE_B, ls)
        c.drawString((x + 0.08) * cm, _y(top, 0.26), label)
    c.setFont(FONTE, vs)
    largura = (w - 0.16) * cm
    linhas = _quebrar(value if value else "-", FONTE, vs, largura, max_linhas)
    if max_linhas == 1:
        c.drawString((x + 0.08) * cm, _y(top, h - 0.15), linhas[0] if linhas else "-")
        return
    for i, linha in enumerate(linhas):
        c.drawString((x + 0.08) * cm, _y(top, 0.56 + i * 0.30), linha)


def _titulo(c, x, top, w, h, texto):
    _rect(c, x, top, w, h, shade=True)
    c.setFillColor(black)
    c.setFont(FONTE_B, 7)
    c.drawString((x + 0.08) * cm, _y(top, h / 2 + 0.09), texto)


def _aviso(c, top, texto):
    _rect(c, COL[0], top, FULL, 0.40, shade=True)
    c.setFillColor(black)
    c.setFont(FONTE_B, 7)
    c.drawCentredString((COL[0] + FULL / 2) * cm, _y(top, 0.28), texto)
    return top + 0.40


def _span(i, n=1):
    return COL[i], COL[i + n - 1] + W - COL[i]


def _linha(c, top, celulas, h=H):
    for i, n, label, valor, *shade in celulas:
        x, w = _span(i, n)
        _cell(c, x, top, w, h, label, valor, shade=bool(shade and shade[0]))
    return top + h


def _linha_com_titulo(c, top, titulo, celulas, h=H):
    _titulo(c, COL[0], top, W, h, titulo)
    return _linha(c, top, celulas, h)


def _bloco_pessoa(c, top, titulo, p, aviso, com_im=True):
    if p is None:
        return _aviso(c, top, aviso)
    if com_im:
        top = _linha_com_titulo(c, top, titulo, [(1, 1, "CNPJ / CPF / NIF", p["doc"]), (2, 1, "Inscrição Municipal", p["im"]), (3, 1, "Telefone", p["fone"])])
    else:
        top = _linha_com_titulo(c, top, titulo, [(1, 2, "CNPJ / CPF / NIF", p["doc"]), (3, 1, "Telefone", p["fone"])])
    top = _linha(c, top, [(0, 2, "Nome / Nome Empresarial", p["nome"]), (2, 1, "Município / Sigla UF", p["mun"]), (3, 1, "Código IBGE / CEP", p["ibge_cep"])])
    return _linha(c, top, [(0, 2, "Endereço", p["end"]), (2, 2, "E-mail", p["email"])])


def _linha_opcional(c, top, celulas):
    if not any(valor for _, _, _, valor in celulas):
        return top
    return _linha(c, top, [(i, n, label, valor) for i, n, label, valor in celulas])


def _cabecalho(c, d, logo):
    _rect(c, 0.30, 0.30, FULL, 1.16, shade=True)
    if logo and Path(logo).exists():
        c.drawImage(str(logo), 0.49 * cm, _y(0.44, 0.85), 4.0 * cm, 0.85 * cm, preserveAspectRatio=True, anchor="w", mask="auto")
    else:
        c.setFont(FONTE_B, 14)
        c.setFillColor(black)
        c.drawString(0.49 * cm, _y(0.44, 0.62), "NFS-e")
    centro = (5.41 + 10.19 / 2) * cm
    c.setFillColor(black)
    c.setFont(FONTE_B, 9)
    bases = (0.58, 0.90, 1.22) if d["homolog"] else (0.72, 1.04)
    c.drawCentredString(centro, _y(0.30, bases[0] - 0.30), "DANFSe v2.0")
    c.drawCentredString(centro, _y(0.30, bases[1] - 0.30), "Documento Auxiliar da NFS-e")
    if d["homolog"]:
        c.setFillColor(red)
        c.drawCentredString(centro, _y(0.30, bases[2] - 0.30), "NFS-e SEM VALIDADE JURÍDICA")
        c.setFillColor(black)
    largura = (W - 0.16) * cm
    c.setFont(FONTE, 8)
    if d["municipio_header"]:
        c.drawString(15.70 * cm, _y(0.30, 0.45), _elipse(f"Município: {d['municipio_header']}", FONTE, 8, largura))
    c.setFont(FONTE, 6)
    c.drawString(15.70 * cm, _y(0.30, 0.84), f"Ambiente Gerador: {d['amb_gerador']}")
    c.drawString(15.70 * cm, _y(0.30, 1.09), f"Tipo de Ambiente: {d['tp_amb']}")


def _identificacao(c, d):
    _cell(c, 0.30, 1.48, 15.30, 0.77, "CHAVE DE ACESSO DA NFS-E", d["chave"], ls=7)
    h = 0.67
    linhas = [
        (2.27, [("NÚMERO DA NFS-E", d["n_nfse"], False), ("COMPETÊNCIA DA NFS-E", d["competencia"], False), ("DATA E HORA DA EMISSÃO DA NFS-E", d["dh_proc"], False)]),
        (2.96, [("NÚMERO DA DPS", d["n_dps"], False), ("SÉRIE DA DPS", d["serie"], False), ("DATA E HORA DA EMISSÃO DA DPS", d["dh_emi"], False)]),
        (3.65, [("EMITENTE DA NFS-E", d["emitente"], True), ("SITUAÇÃO DA NFS-E", d["situacao"], False), ("FINALIDADE", d["finalidade"], False)]),
    ]
    for top, campos in linhas:
        for i, (label, valor, shade) in enumerate(campos):
            _cell(c, COL[i], top, W, h, label, valor, shade=shade, ls=7)
    _rect(c, COL[3], 1.48, W, 2.84)
    url = CONSULTA_URL + d["chave"]
    qr = QrCodeWidget(url, barLevel="L", barBorder=0)
    b = qr.getBounds()
    lado = 1.60 * cm
    desenho = Drawing(lado, lado, transform=[lado / (b[2] - b[0]), 0, 0, lado / (b[3] - b[1]), 0, 0])
    desenho.add(qr)
    renderPDF.draw(desenho, c, 17.48 * cm, _y(1.67, 1.60))
    c.setFillColor(black)
    c.setFont(FONTE, 6)
    texto = "A autenticidade desta NFS-e pode ser verificada pela leitura deste código QR ou pela consulta da chave de acesso no portal nacional da NFS-e"
    for i, linha in enumerate(simpleSplit(texto, FONTE, 6, 4.72 * cm)[:3]):
        c.drawCentredString((15.80 + 4.72 / 2) * cm, _y(3.36, 0.20 + i * 0.22), linha)
    return 4.32


def _bloco_servico(c, top, d):
    top = _linha_com_titulo(c, top, "SERVIÇO PRESTADO", [
        (1, 1, "Cód. Tributação Nacional / Municipal", d["c_trib"]),
        (2, 1, "Código da NBS", d["nbs"]),
        (3, 1, "Local da Prestação / UF / País", d["local_prestacao"]),
    ])
    _rect(c, COL[0], top, FULL, 0.40)
    c.setFillColor(black)
    c.setFont(FONTE, 7)
    c.drawString((COL[0] + 0.08) * cm, _y(top, 0.28), _elipse(d["desc_trib"] or "-", FONTE, 7, (FULL - 0.16) * cm))
    top += 0.40
    texto = d["desc_servico"] or "-"
    n_linhas = max(2, len(simpleSplit(texto, FONTE, 7, (FULL - 0.16) * cm)))
    n_linhas = min(n_linhas, 24)
    h = 0.56 + 0.30 * n_linhas - 0.10
    _cell(c, COL[0], top, FULL, h, "Descrição do Serviço", texto, max_linhas=n_linhas)
    return top + h


def _bloco_issqn(c, top, d):
    if d["trib_issqn_cod"] == "4":
        return _aviso(c, top, "TRIBUTAÇÃO MUNICIPAL (ISSQN) - OPERAÇÃO NÃO SUJEITA AO ISSQN")
    top = _linha_com_titulo(c, top, "TRIBUTAÇÃO MUNICIPAL (ISSQN)", [
        (1, 1, "Tipo de Tributação do ISSQN", d["trib_issqn"]),
        (2, 2, "Município / UF / País da Incidência do ISSQN", d["local_incidencia"]),
    ])
    top = _linha_opcional(c, top, [
        (0, 1, "Regime Especial de Tributação do ISSQN", d["reg_esp_trib"]),
        (1, 1, "Tipo de Imunidade do ISSQN", d["tp_imunidade"]),
        (2, 1, "Suspensão da Exigibilidade do ISSQN", d["tp_susp"]),
        (3, 1, "Número Processo Suspensão", d["n_processo"]),
    ])
    top = _linha_opcional(c, top, [
        (0, 1, "Benefício Municipal", d["tp_bm"]),
        (1, 1, "Cálculo do BM", _money(d["calc_bm"])),
        (2, 1, "Total Deduções/Reduções", _money(d["total_ded"])),
        (3, 1, "Desconto Incondicionado", _money(d["desc_incond"])),
    ])
    return _linha(c, top, [
        (0, 1, "BC ISSQN", _money(d["bc_issqn"])),
        (1, 1, "Alíquota Aplicada", _pct(d["aliq_aplic"])),
        (2, 1, "Retenção do ISSQN", d["ret_issqn"]),
        (3, 1, "ISSQN Apurado", _money(d["v_issqn"])),
    ])


def _bloco_federal(c, top, d):
    top = _linha_com_titulo(c, top, "TRIBUTAÇÃO FEDERAL (EXCETO CBS)", [
        (1, 1, "IRRF", _money(d["irrf"])),
        (2, 1, "Contribuição Previdenciária - Retida", _money(d["contrib_prev"])),
        (3, 1, "Contribuições Sociais - Retidas", _money(d["contrib_sociais"])),
    ])
    return _linha(c, top, [
        (0, 1, "PIS - Débito Apuração Própria", _money(d["pis"])),
        (1, 1, "COFINS - Débito Apuração Própria", _money(d["cofins"])),
        (2, 2, "Descrição Contrib. Sociais - Retidas", d["desc_contrib"]),
    ])


def _bloco_ibs_cbs(c, top, d):
    top = _linha_com_titulo(c, top, "TRIBUTAÇÃO IBS / CBS", [
        (1, 1, "CST / cClassTrib", d["cst_class"]),
        (2, 2, "Indicador de Operação / Cód. IBGE Incidência / Município / UF", d["local_ibs"]),
    ])
    top = _linha(c, top, [
        (0, 1, "Exclusões e Reduções da BC", _money(d["exclusoes"])),
        (1, 1, "BC Após Exclusões e Reduções", _money(d["bc_ibs"])),
        (2, 1, "Red. Alíquota IBS / Red. Alíquota CBS", d["red_aliq"]),
        (3, 1, "Alíquota - IBS UF / IBS Mun", d["aliq_ibs"]),
    ])
    top = _linha(c, top, [
        (0, 1, "Alíq. Efetiva Municipal - IBS", d["aliq_ef_mun"]),
        (1, 1, "Valor Apurado Municipal - IBS", _money(d["v_ibs_mun"])),
        (2, 1, "Alíq. Efetiva Estadual - IBS", d["aliq_ef_uf"]),
        (3, 1, "Valor Apurado Estadual - IBS", _money(d["v_ibs_uf"])),
    ])
    return _linha(c, top, [
        (0, 1, "Valor Total Apurado - IBS", _money(d["v_ibs_tot"])),
        (1, 1, "Alíquota - CBS", d["p_cbs"]),
        (2, 1, "Alíquota Efetiva - CBS", d["aliq_ef_cbs"]),
        (3, 1, "Valor Total Apurado - CBS", _money(d["v_cbs"])),
    ])


def _bloco_total(c, top, d):
    top = _linha_com_titulo(c, top, "VALOR TOTAL DA NFS-E", [
        (1, 1, "Valor da Operação / Serviço", _money(d["v_serv"])),
        (2, 1, "Desconto Incondicionado", _money(d["desc_incond"])),
        (3, 1, "Desconto Condicionado", _money(d["desc_cond"])),
    ], h=0.67)
    return _linha(c, top, [
        (0, 1, "Total das Retenções (ISSQN / Federais)", _money(d["total_ret"])),
        (1, 1, "Valor Líquido da NFS-e", _money(d["v_liq"])),
        (2, 1, "Total do IBS/CBS", _money(d["total_ibs_cbs"])),
        (3, 1, "Valor Líquido da NFS-e + IBS/CBS", _money(d["v_tot_nf"]), True),
    ], h=0.67)


def _bloco_info(c, top, d):
    _rect(c, COL[0], top, FULL, 0.39, shade=True)
    c.setFillColor(black)
    c.setFont(FONTE_B, 7)
    c.drawString((COL[0] + 0.08) * cm, _y(top, 0.27), "INFORMAÇÕES COMPLEMENTARES")
    top += 0.39
    h = BOTTOM - top
    _rect(c, COL[0], top, FULL, h)
    c.setFillColor(black)
    c.setFont(FONTE, 7)
    max_linhas = int((h - 0.2) / 0.30)
    for i, linha in enumerate(_quebrar(d["info"], FONTE, 7, (FULL - 0.16) * cm, max_linhas)):
        c.drawString((COL[0] + 0.08) * cm, _y(top, 0.30 + i * 0.30), linha)


def _renderizar(d, destino, logo):
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(destino), pagesize=(PAGE_W * cm, PAGE_H * cm))
    c.setTitle(f"DANFSe {d['chave']}")
    c.setLineWidth(1)
    c.rect(0.20 * cm, 0.20 * cm, (PAGE_W - 0.40) * cm, (PAGE_H - 0.40) * cm, stroke=1, fill=0)
    _cabecalho(c, d, logo)
    top = _identificacao(c, d)

    p = d["prestador"]
    top = _bloco_pessoa(c, top, "PRESTADOR / FORNECEDOR", p, "")
    top = _linha(c, top, [
        (0, 2, "Simples Nacional na Data de Competência", p["op_simp_nac"]),
        (2, 2, "Regime de Apuração Tributária pelo SN", p["reg_ap_trib_sn"]),
    ])
    top = _bloco_pessoa(c, top, "TOMADOR / ADQUIRENTE", d["tomador"], "TOMADOR/ADQUIRENTE DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    top = _bloco_pessoa(c, top, "DESTINATÁRIO DA OPERAÇÃO", d["destinatario"], d["destinatario_aviso"], com_im=False)
    top = _bloco_pessoa(c, top, "INTERMEDIÁRIO DA OPERAÇÃO", d["intermediario"], "INTERMEDIÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
    top = _bloco_servico(c, top, d)
    top = _bloco_issqn(c, top, d)
    top = _bloco_federal(c, top, d)
    top = _bloco_ibs_cbs(c, top, d)
    top = _bloco_total(c, top, d)
    _bloco_info(c, top, d)
    c.showPage()
    c.save()


def gerar_danfse(xml, destino, cache_municipios=None, logo=None):
    _renderizar(_extrair(xml, cache_municipios), destino, logo)
    return str(destino)