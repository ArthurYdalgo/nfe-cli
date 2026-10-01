#!/usr/bin/env python3
import argparse
import base64
import getpass
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

NS_NFSE = "http://www.sped.fazenda.gov.br/nfse"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
TZ = ZoneInfo("America/Sao_Paulo")
VER_APLIC = "emissor-py-1.0"
BASE_DIR = Path(__file__).resolve().parent

AMBIENTES = {
    "producao": {
        "tp_amb": "1",
        "sefin": "https://sefin.nfse.gov.br/SefinNacional",
        "adn": "https://adn.nfse.gov.br",
    },
    "homologacao": {
        "tp_amb": "2",
        "sefin": "https://sefin.producaorestrita.nfse.gov.br/SefinNacional",
        "adn": "https://adn.producaorestrita.nfse.gov.br",
    },
}


class ErroSefin(Exception):
    pass


class Certificado:
    def __init__(self, path, senha):
        key, cert, extras = pkcs12.load_key_and_certificates(Path(path).read_bytes(), senha.encode())
        if key is None or cert is None:
            raise SystemExit("PFX sem chave privada ou certificado.")
        self.key = key
        self.cert = cert
        self.extras = extras or []
        self.cert_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()

    @contextmanager
    def pem_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path = Path(tmp) / "cert.pem"
            key_path = Path(tmp) / "key.pem"
            chain = [self.cert, *self.extras]
            cert_path.write_bytes(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain))
            key_path.touch(mode=0o600)
            key_path.write_bytes(self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            yield str(cert_path), str(key_path)


def validar_config(cfg):
    for campo in ("documento", "cod_municipio"):
        valor = limpar_doc(cfg["prestador"].get(campo))
        if not valor or len(set(valor)) == 1:
            raise SystemExit(f"config.json: prestador.{campo} não preenchido (valor placeholder).")


def doc_do_certificado(cert):
    m = re.search(r":(\d{14}|\d{11})\b", cert.cert.subject.rfc4514_string())
    return m.group(1) if m else None


def ci(data, chave):
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if k.lower() == chave.lower():
            return v
    return None


def limpar_doc(valor):
    return re.sub(r"[^0-9A-Za-z]", "", valor or "").upper()


def so_digitos(valor):
    return re.sub(r"\D", "", valor or "")


def dec2(valor):
    return str(Decimal(str(valor)).quantize(Decimal("0.01")))


def doc_tag(doc):
    return "CNPJ" if len(doc) == 14 else "CPF"


def ler_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def gravar_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def c14n(node):
    return ET.canonicalize(etree.tostring(node, encoding="unicode", with_tail=False)).encode("utf-8")


def el(parent, tag, text=None, **attrs):
    node = etree.SubElement(parent, f"{{{NS_NFSE}}}{tag}", **attrs)
    if text is not None:
        node.text = str(text)
    return node


def opt(parent, tag, text):
    if text not in (None, ""):
        el(parent, tag, text)


def xpath_ln(path):
    return "/".join(f"*[local-name()='{p}']" for p in path.split("/"))


def xt(node, path):
    found = node.xpath(xpath_ln(path))
    return (found[0].text or "").strip() if found else ""


def gerar_id(cfg, n_dps):
    doc = limpar_doc(cfg["prestador"]["documento"])
    tp_insc = "2" if len(doc) == 14 else "1"
    return (
        "DPS"
        + cfg["prestador"]["cod_municipio"]
        + tp_insc
        + doc.zfill(14)
        + str(int(cfg["serie"])).zfill(5)
        + str(n_dps).zfill(15)
    )


def montar_dps(cfg, dados, n_dps, tp_amb):
    prest_cfg = cfg["prestador"]
    doc_prest = limpar_doc(prest_cfg["documento"])
    agora = datetime.now(TZ).replace(microsecond=0) - timedelta(seconds=30)

    dps = etree.Element(f"{{{NS_NFSE}}}DPS", nsmap={None: NS_NFSE}, versao="1.01")
    inf = el(dps, "infDPS", Id=gerar_id(cfg, n_dps))
    el(inf, "tpAmb", tp_amb)
    el(inf, "dhEmi", agora.isoformat())
    el(inf, "verAplic", VER_APLIC)
    el(inf, "serie", int(cfg["serie"]))
    el(inf, "nDPS", n_dps)
    el(inf, "dCompet", dados["competencia"])
    el(inf, "tpEmit", "1")
    el(inf, "cLocEmi", prest_cfg["cod_municipio"])

    prest = el(inf, "prest")
    el(prest, doc_tag(doc_prest), doc_prest)
    opt(prest, "fone", so_digitos(prest_cfg.get("fone")))
    opt(prest, "email", prest_cfg.get("email"))
    reg_trib = el(prest, "regTrib")
    el(reg_trib, "opSimpNac", prest_cfg["op_simp_nac"])
    if str(prest_cfg["op_simp_nac"]) == "3":
        el(reg_trib, "regApTribSN", prest_cfg.get("reg_ap_trib_sn", 1))
    el(reg_trib, "regEspTrib", prest_cfg.get("reg_esp_trib", 0))

    tomador = dados["tomador"]
    if tomador.get("documento"):
        toma = el(inf, "toma")
        el(toma, doc_tag(tomador["documento"]), tomador["documento"])
        el(toma, "xNome", tomador["nome"])
        if tomador.get("cep"):
            end = el(toma, "end")
            end_nac = el(end, "endNac")
            el(end_nac, "cMun", tomador["cod_municipio"])
            el(end_nac, "CEP", so_digitos(tomador["cep"]))
            el(end, "xLgr", tomador["logradouro"])
            el(end, "nro", tomador["numero"])
            opt(end, "xCpl", tomador.get("complemento"))
            el(end, "xBairro", tomador["bairro"])
        opt(toma, "fone", so_digitos(tomador.get("fone")))
        opt(toma, "email", tomador.get("email"))

    servico = dados["servico"]
    serv = el(inf, "serv")
    el(el(serv, "locPrest"), "cLocPrestacao", servico["cod_municipio_prestacao"])
    c_serv = el(serv, "cServ")
    el(c_serv, "cTribNac", so_digitos(servico["c_trib_nac"]))
    opt(c_serv, "cTribMun", servico.get("c_trib_mun"))
    el(c_serv, "xDescServ", servico["descricao"])
    opt(c_serv, "cNBS", so_digitos(servico.get("c_nbs")))

    valores_dados = dados["valores"]
    valores = el(inf, "valores")
    el(el(valores, "vServPrest"), "vServ", dec2(valores_dados["valor_servico"]))
    trib = el(valores, "trib")
    trib_mun = el(trib, "tribMun")
    el(trib_mun, "tribISSQN", valores_dados["trib_issqn"])
    el(trib_mun, "tpRetISSQN", valores_dados["tp_ret_issqn"])
    if valores_dados.get("aliquota"):
        el(trib_mun, "pAliq", dec2(valores_dados["aliquota"]))
    trib_fed_cfg = cfg.get("trib_fed")
    if trib_fed_cfg:
        pis_cofins = el(el(trib, "tribFed"), "piscofins")
        el(pis_cofins, "CST", trib_fed_cfg["cst"])
        el(pis_cofins, "tpRetPisCofins", trib_fed_cfg["tp_ret_pis_cofins"])
    tot_trib = el(trib, "totTrib")
    for tag, valor in cfg["tot_trib"].items():
        el(tot_trib, tag, valor)

    ibscbs_cfg = cfg.get("ibscbs")
    if ibscbs_cfg:
        ibscbs = el(inf, "IBSCBS")
        el(ibscbs, "finNFSe", ibscbs_cfg.get("fin_nfse", "0"))
        el(ibscbs, "indFinal", ibscbs_cfg.get("ind_final", "0"))
        el(ibscbs, "cIndOp", ibscbs_cfg["c_ind_op"])
        el(ibscbs, "indDest", ibscbs_cfg.get("ind_dest", "0"))
        g_ibscbs = el(el(el(ibscbs, "valores"), "trib"), "gIBSCBS")
        el(g_ibscbs, "CST", ibscbs_cfg["cst"])
        el(g_ibscbs, "cClassTrib", ibscbs_cfg["c_class_trib"])

    return dps


def assinar(dps, cert):
    inf = dps.find(f"{{{NS_NFSE}}}infDPS")
    digest = base64.b64encode(hashlib.sha1(c14n(inf)).digest()).decode()

    def ds(parent, tag, text=None, **attrs):
        node = etree.SubElement(parent, f"{{{NS_DS}}}{tag}", **attrs)
        if text is not None:
            node.text = text
        return node

    signature = etree.SubElement(dps, f"{{{NS_DS}}}Signature", nsmap={None: NS_DS})
    signed_info = ds(signature, "SignedInfo")
    ds(signed_info, "CanonicalizationMethod", Algorithm=C14N)
    ds(signed_info, "SignatureMethod", Algorithm=NS_DS + "rsa-sha1")
    reference = ds(signed_info, "Reference", URI="#" + inf.get("Id"))
    transforms = ds(reference, "Transforms")
    ds(transforms, "Transform", Algorithm=NS_DS + "enveloped-signature")
    ds(transforms, "Transform", Algorithm=C14N)
    ds(reference, "DigestMethod", Algorithm=NS_DS + "sha1")
    ds(reference, "DigestValue", digest)

    signature_value = cert.key.sign(c14n(signed_info), padding.PKCS1v15(), hashes.SHA1())
    ds(signature, "SignatureValue", base64.b64encode(signature_value).decode())
    ds(ds(ds(signature, "KeyInfo"), "X509Data"), "X509Certificate", cert.cert_b64)

    return b'<?xml version="1.0" encoding="UTF-8"?>' + etree.tostring(dps, encoding="UTF-8")


def formatar_erros(corpo):
    erros = ci(corpo, "erros") or ci(corpo, "erro")
    if isinstance(erros, dict):
        erros = [erros]
    if not erros:
        return json.dumps(corpo, ensure_ascii=False)
    linhas = []
    for e in erros:
        partes = [ci(e, "codigo"), ci(e, "descricao"), ci(e, "complemento")]
        linhas.append(" | ".join(str(p) for p in partes if p))
    return "\n".join(linhas)


def decodificar_xml(b64):
    return gzip.decompress(base64.b64decode(b64))


def consultar_nfse(session, sefin, chave):
    r = session.get(f"{sefin}/nfse/{chave}", timeout=60)
    r.raise_for_status()
    return r.json()


def proximo_ndps(session, sefin, cfg, inicial):
    n_dps = inicial
    for _ in range(100):
        try:
            r = session.head(f"{sefin}/dps/{gerar_id(cfg, n_dps)}", timeout=30)
        except requests.RequestException:
            return n_dps
        if r.status_code != 200:
            return n_dps
        n_dps += 1
    raise SystemExit("100 nDPS seguidos já usados. Verifique a série configurada.")


def enviar(session, sefin, dps_id, xml_assinado):
    payload = {"dpsXmlGZipB64": base64.b64encode(gzip.compress(xml_assinado)).decode()}
    try:
        r = session.post(f"{sefin}/nfse", json=payload, timeout=90)
    except requests.Timeout:
        r_dps = session.get(f"{sefin}/dps/{dps_id}", timeout=30)
        chave = ci(r_dps.json(), "chaveAcesso") if r_dps.status_code == 200 else None
        if not chave:
            raise ErroSefin("Timeout no envio e DPS não localizada. Rode novamente; o nDPS será verificado.")
        return consultar_nfse(session, sefin, chave)
    try:
        corpo = r.json()
    except ValueError:
        raise ErroSefin(f"HTTP {r.status_code}: {r.text[:2000]}")
    if r.status_code not in (200, 201) or not ci(corpo, "chaveAcesso"):
        raise ErroSefin(f"HTTP {r.status_code}\n{formatar_erros(corpo)}")
    return corpo


def gerar_pdf(xml, chave, pasta):
    try:
        from danfse import gerar_danfse
    except ImportError as e:
        return f"falhou (dependência ausente: {e.name}; rode: pip install reportlab)"
    destino = BASE_DIR / f"{chave}.pdf"
    try:
        return gerar_danfse(xml, destino, cache_municipios=pasta.parent / "ibge.json", logo=BASE_DIR / "logo-nfse.png")
    except Exception as e:
        return f"falhou ({e.__class__.__name__}: {e})"


def extrair_dados(xml):
    root = etree.fromstring(xml)
    infs = root.xpath("//*[local-name()='infDPS']")
    if not infs:
        raise ValueError("XML sem infDPS")
    inf = infs[0]
    id_nfse = root.xpath("string(//*[local-name()='infNFSe']/@Id)")
    return {
        "chave": re.sub(r"^NFS", "", id_nfse),
        "dh_emi": xt(inf, "dhEmi"),
        "prestador": xt(inf, "prest/CNPJ") or xt(inf, "prest/CPF"),
        "n_nfse": xt(root, "infNFSe/nNFSe") if root.xpath(xpath_ln("infNFSe")) else "",
        "dados": {
            "tomador": {
                "documento": xt(inf, "toma/CNPJ") or xt(inf, "toma/CPF"),
                "nome": xt(inf, "toma/xNome"),
                "email": xt(inf, "toma/email"),
                "fone": xt(inf, "toma/fone"),
                "cep": xt(inf, "toma/end/endNac/CEP"),
                "cod_municipio": xt(inf, "toma/end/endNac/cMun"),
                "logradouro": xt(inf, "toma/end/xLgr"),
                "numero": xt(inf, "toma/end/nro"),
                "complemento": xt(inf, "toma/end/xCpl"),
                "bairro": xt(inf, "toma/end/xBairro"),
            },
            "servico": {
                "c_trib_nac": xt(inf, "serv/cServ/cTribNac"),
                "c_trib_mun": xt(inf, "serv/cServ/cTribMun"),
                "c_nbs": xt(inf, "serv/cServ/cNBS"),
                "descricao": xt(inf, "serv/cServ/xDescServ"),
                "cod_municipio_prestacao": xt(inf, "serv/locPrest/cLocPrestacao"),
            },
            "valores": {
                "valor_servico": xt(inf, "valores/vServPrest/vServ"),
                "aliquota": xt(inf, "valores/trib/tribMun/pAliq"),
                "trib_issqn": xt(inf, "valores/trib/tribMun/tribISSQN"),
                "tp_ret_issqn": xt(inf, "valores/trib/tribMun/tpRetISSQN"),
            },
        },
    }


def atualizar_recentes(recentes, novas, limite=3):
    por_nf = {}
    for r in [*recentes, *novas]:
        r = {**r, "chave": re.sub(r"^import-(?=\d{50}$)", "", r["chave"])}
        chave_nf = (r.get("n_nfse"), r["dh_emi"]) if r.get("n_nfse") else r["chave"]
        existente = por_nf.get(chave_nf)
        if existente and not existente["chave"].startswith(("import-", "nsu-")) and r["chave"].startswith(("import-", "nsu-")):
            continue
        por_nf[chave_nf] = r
    ordenadas = sorted(por_nf.values(), key=lambda r: datetime.fromisoformat(r["dh_emi"]), reverse=True)
    return ordenadas[:limite]


def sincronizar(session, adn, cfg, pasta, estado):
    doc = limpar_doc(cfg["prestador"]["documento"])
    params = {"cnpjConsulta": doc} if len(doc) == 14 else {}
    ultimo_nsu = int(estado.get("ultimo_nsu", 0))
    encontradas = []
    for _ in range(1000):
        r = session.get(f"{adn}/contribuintes/DFe/{ultimo_nsu}", params={**params, "lote": "true"}, timeout=60)
        if r.status_code in (204, 404):
            break
        if r.status_code >= 400:
            raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:500]}", response=r)
        lote = ci(r.json(), "LoteDFe") or []
        itens = [i for i in lote if int(ci(i, "NSU") or 0) > ultimo_nsu]
        if not itens:
            break
        for item in itens:
            ultimo_nsu = max(ultimo_nsu, int(ci(item, "NSU")))
            arquivo = ci(item, "ArquivoXml")
            if not arquivo:
                continue
            xml = decodificar_xml(arquivo)
            if etree.QName(etree.fromstring(xml)).localname != "NFSe":
                continue
            try:
                extraido = extrair_dados(xml)
            except ValueError:
                continue
            if limpar_doc(extraido["prestador"]) != doc:
                continue
            chave = ci(item, "ChaveAcesso") or extraido["chave"] or f"nsu-{ultimo_nsu}"
            (pasta / "xml").mkdir(parents=True, exist_ok=True)
            (pasta / "xml" / f"{chave}.xml").write_bytes(xml)
            extraido["chave"] = chave
            encontradas.append(extraido)
    estado["ultimo_nsu"] = ultimo_nsu
    print(f"Sincronização ADN: {len(encontradas)} NFS-e nova(s), NSU atual {ultimo_nsu}.")
    return encontradas


def perguntar(label, default="", obrigatorio=True):
    sufixo = f" [{default}]" if default else ""
    while True:
        valor = input(f"{label}{sufixo}: ").strip()
        if valor == "-":
            if obrigatorio:
                print("  campo obrigatório")
                continue
            return ""
        valor = valor or str(default or "")
        if valor or not obrigatorio:
            return valor
        print("  campo obrigatório")


def perguntar_decimal(label, default="", obrigatorio=True):
    while True:
        valor = perguntar(label, default, obrigatorio)
        if not valor:
            return ""
        normalizado = valor.replace(".", "").replace(",", ".") if "," in valor else valor
        try:
            if Decimal(normalizado) < 0:
                raise InvalidOperation
            return dec2(normalizado)
        except InvalidOperation:
            print("  valor inválido")


def perguntar_regex(label, default, padrao, obrigatorio=True):
    while True:
        valor = perguntar(label, default, obrigatorio)
        if not valor or re.fullmatch(padrao, valor):
            return valor
        print("  formato inválido")


def confirmar(label, padrao=True):
    resposta = input(f"{label} [{'S/n' if padrao else 's/N'}]: ").strip().lower()
    return padrao if not resposta else resposta in ("s", "sim", "y")


def coletar(defaults, cfg):
    t = defaults.get("tomador", {})
    s = defaults.get("servico", {})
    v = defaults.get("valores", {})

    print("\n-- Tomador ('-' limpa campo opcional) --")
    tomador = {"documento": limpar_doc(perguntar("CPF/CNPJ (vazio = sem tomador)", t.get("documento"), False))}
    if tomador["documento"]:
        if len(tomador["documento"]) not in (11, 14):
            raise SystemExit("CPF/CNPJ do tomador inválido.")
        tomador["nome"] = perguntar("Nome/razão social", t.get("nome"))
        tomador["email"] = perguntar("E-mail", t.get("email"), False)
        tomador["fone"] = perguntar("Telefone", t.get("fone"), False)
        tomador["cep"] = perguntar_regex("CEP (vazio = sem endereço)", t.get("cep"), r"\d{5}-?\d{3}", False)
        if tomador["cep"]:
            tomador["cod_municipio"] = perguntar_regex("Cód. IBGE do município", t.get("cod_municipio"), r"\d{7}")
            tomador["logradouro"] = perguntar("Logradouro", t.get("logradouro"))
            tomador["numero"] = perguntar("Número", t.get("numero"))
            tomador["complemento"] = perguntar("Complemento", t.get("complemento"), False)
            tomador["bairro"] = perguntar("Bairro", t.get("bairro"))

    print("\n-- Serviço --")
    servico = {
        "c_trib_nac": perguntar_regex("cTribNac (6 dígitos)", s.get("c_trib_nac"), r"\d{2}\.?\d{2}\.?\d{2}"),
        "c_trib_mun": perguntar_regex("cTribMun (3 dígitos)", s.get("c_trib_mun"), r"\d{3}", False),
        "c_nbs": perguntar_regex("cNBS (9 dígitos)", s.get("c_nbs"), r"[\d.]{9,12}", bool(cfg.get("ibscbs"))),
        "descricao": perguntar("Descrição", s.get("descricao")),
        "cod_municipio_prestacao": perguntar_regex(
            "Cód. IBGE local da prestação",
            s.get("cod_municipio_prestacao") or cfg["prestador"]["cod_municipio"],
            r"\d{7}",
        ),
    }

    print("\n-- Valores --")
    valores = {
        "valor_servico": perguntar_decimal("Valor do serviço", v.get("valor_servico")),
        "aliquota": perguntar_decimal("Alíquota ISS %", v.get("aliquota"), False),
        "trib_issqn": perguntar_regex(
            "tribISSQN (1 tributável, 2 imunidade, 3 exportação, 4 não incidência)", v.get("trib_issqn") or "1", r"[1-4]"
        ),
        "tp_ret_issqn": perguntar_regex(
            "tpRetISSQN (1 não retido, 2 retido tomador, 3 retido intermediário)", v.get("tp_ret_issqn") or "1", r"[1-3]"
        ),
    }

    competencia = perguntar_regex("Competência (AAAA-MM-DD)", date.today().isoformat(), r"\d{4}-\d{2}-\d{2}")
    date.fromisoformat(competencia)

    return {"tomador": tomador, "servico": servico, "valores": valores, "competencia": competencia}


def dados_completos(dados, cfg):
    t, s, v = dados["tomador"], dados["servico"], dados["valores"]
    obrigatorios = [
        s.get("c_trib_nac"), s.get("descricao"), s.get("cod_municipio_prestacao"),
        v.get("valor_servico"), v.get("trib_issqn"), v.get("tp_ret_issqn"),
    ]
    if t.get("documento"):
        obrigatorios.append(t.get("nome"))
        if t.get("cep"):
            obrigatorios += [t.get("cod_municipio"), t.get("logradouro"), t.get("numero"), t.get("bairro")]
    if cfg.get("ibscbs"):
        obrigatorios.append(s.get("c_nbs"))
    return all(obrigatorios)


def linha_nf(indice, nf):
    d = nf["dados"]
    tomador = d["tomador"].get("nome") or "(sem tomador)"
    return (
        f" [{indice}] {nf['dh_emi'][:10]}  nº {nf.get('n_nfse') or '?':<6} {tomador[:30]:<30} "
        f"R$ {d['valores']['valor_servico']:>10}  {d['servico']['descricao'][:40]}"
    )


def escolher_nf(recentes, cfg, defaults):
    print("\nÚltimas NFS-e emitidas:")
    for i, nf in enumerate(recentes, 1):
        print(linha_nf(i, nf))
    print(" [n] nova, preencher manualmente\n [q] sair")
    while True:
        opcao = input("Replicar qual? [1]: ").strip().lower() or "1"
        if opcao == "q":
            print("Cancelado.")
            sys.exit(0)
        if opcao == "n":
            return coletar(defaults, cfg)
        if opcao.isdigit() and 1 <= int(opcao) <= len(recentes):
            break
    base = mesclar(defaults, recentes[int(opcao) - 1]["dados"])
    for secao in ("tomador", "servico", "valores"):
        base.setdefault(secao, {})
    base["competencia"] = date.today().isoformat()
    if not dados_completos(base, cfg):
        print("Dados da NF escolhida incompletos, preencha manualmente.")
        return coletar(base, cfg)
    base["valores"]["valor_servico"] = perguntar_decimal("Valor do serviço", base["valores"]["valor_servico"])
    return base


def mesclar(base, extra):
    resultado = {k: dict(v) for k, v in base.items()}
    for secao, campos in (extra or {}).items():
        for k, valor in campos.items():
            if valor:
                resultado.setdefault(secao, {})[k] = valor
    return resultado


def resumo(dados):
    t, s, v = dados["tomador"], dados["servico"], dados["valores"]
    tomador = f"{t.get('nome')} ({t['documento']})" if t.get("documento") else "(sem tomador)"
    return (
        f"Tomador:     {tomador}\n"
        f"Serviço:     {s['c_trib_nac']} - {s['descricao'][:80]}\n"
        f"Valor:       R$ {v['valor_servico']}  alíquota: {v.get('aliquota') or '-'}  "
        f"tribISSQN: {v['trib_issqn']}  tpRet: {v['tp_ret_issqn']}"
        + (f"\nCompetência: {dados['competencia']}" if "competencia" in dados else "")
    )


@contextmanager
def abrir_sessao(cert):
    with cert.pem_files() as (cert_path, key_path):
        session = requests.Session()
        session.cert = (cert_path, key_path)
        session.headers["Accept"] = "application/json"
        yield session


def sugerir_cod_municipio(documento):
    for pasta_amb in (BASE_DIR / "dados").glob("*"):
        xmls = sorted((pasta_amb / "xml").glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
        for xml_path in xmls:
            try:
                nf = extrair_dados(xml_path.read_bytes())
            except (ValueError, etree.XMLSyntaxError):
                continue
            cod = nf["dados"]["servico"].get("cod_municipio_prestacao")
            if cod:
                return cod, f"da última NFS-e local ({xml_path.name})"

    if documento and len(documento) == 14:
        try:
            resp = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{documento}", timeout=8)
            resp.raise_for_status()
            cod = str(ci(resp.json(), "codigo_municipio_ibge") or "")
            if re.fullmatch(r"\d{7}", cod):
                return cod, "via BrasilAPI (dados do CNPJ)"
        except requests.RequestException:
            pass

    return "", ""


def localizar_certificado(cfg):
    valor = cfg.get("certificado")
    if valor:
        cert_path = Path(valor)
        return cert_path if cert_path.is_absolute() else BASE_DIR / cert_path
    encontrados = sorted(BASE_DIR.glob("*.pfx"))
    if not encontrados:
        raise SystemExit("Nenhum certificado .pfx encontrado na pasta e 'certificado' não foi definido no config.json.")
    if len(encontrados) > 1:
        nomes = ", ".join(p.name for p in encontrados)
        raise SystemExit(f"Mais de um .pfx encontrado na pasta ({nomes}). Defina 'certificado' no config.json com o caminho desejado.")
    return encontrados[0]


def abrir_certificado(cfg, config_path):
    senha = cfg.get("senha_certificado") or os.environ.get("NFSE_CERT_SENHA")
    if cfg.get("senha_certificado") and os.name == "posix" and config_path.stat().st_mode & 0o077:
        print(f"Aviso: {config_path} contém a senha e é legível por outros usuários. Rode: chmod 600 {config_path}", file=sys.stderr)
    senha = senha or getpass.getpass("Senha do certificado: ")
    cert_path = localizar_certificado(cfg)
    cert = Certificado(cert_path, senha)
    print(f"Certificado: {cert.cert.subject.rfc4514_string()} | válido até {cert.cert.not_valid_after_utc:%d/%m/%Y}")
    return cert


def listar_locais(pasta, limite=10):
    notas = []
    for arquivo in (pasta / "xml").glob("*.xml"):
        try:
            nf = extrair_dados(arquivo.read_bytes())
            nf["_dt"] = datetime.fromisoformat(nf["dh_emi"])
        except (ValueError, etree.XMLSyntaxError):
            continue
        nf["chave"] = nf["chave"] or arquivo.stem
        notas.append(nf)
    notas.sort(key=lambda nf: nf["_dt"], reverse=True)
    return notas[:limite]


def menu_principal():
    print("\n [1] Emitir nova NFS-e\n [2] Gerar PDF de uma NFS-e anterior\n [q] Sair")
    while True:
        opcao = input("O que deseja fazer? [1]: ").strip().lower() or "1"
        if opcao in ("1", "2", "q"):
            return opcao


def escolher_chaves_pdf(notas):
    padrao = "1"
    if notas:
        print("\nNFS-e disponíveis:")
        for i, nf in enumerate(notas, 1):
            print(linha_nf(i, nf))
    else:
        print("\nNenhuma NFS-e local.")
        padrao = "c"
    print(" [c] informar chave\n [q] voltar")
    while True:
        opcao = input(f"Gerar PDF de qual? (ex: 1,3) [{padrao}]: ").strip().lower() or padrao
        if opcao == "q":
            return []
        if opcao == "c":
            chave = re.sub(r"\D", "", input("Chave de acesso (50 dígitos): "))
            if len(chave) == 50:
                return [chave]
            print("  chave inválida")
            continue
        indices = [x.strip() for x in opcao.split(",")]
        if all(x.isdigit() and 1 <= int(x) <= len(notas) for x in indices):
            return [notas[int(x) - 1]["chave"] for x in indices]


def gerar_pdfs(chaves, pasta, ambiente, session=None):
    for chave in chaves:
        xml_path = pasta / "xml" / f"{chave}.xml"
        if not xml_path.exists():
            if session is None:
                raise SystemExit(f"XML local não encontrado: {xml_path}")
            try:
                resposta = consultar_nfse(session, ambiente["sefin"], chave)
            except requests.RequestException as e:
                print(f"{chave}: não foi possível obter o XML da NFS-e ({e})", file=sys.stderr)
                continue
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_bytes(decodificar_xml(ci(resposta, "nfseXmlGZipB64")))
        print(f"DANFSe: {gerar_pdf(xml_path.read_bytes(), chave, pasta)}")


def comando_pdf(args, cfg, ambiente, pasta):
    chaves = [args.pdf] if args.pdf else escolher_chaves_pdf(listar_locais(pasta))
    for chave in chaves:
        if not re.fullmatch(r"\d{50}", chave):
            raise SystemExit(f"Chave inválida: {chave}")
    if all((pasta / "xml" / f"{chave}.xml").exists() for chave in chaves):
        return gerar_pdfs(chaves, pasta, ambiente)
    cert = abrir_certificado(cfg, Path(args.config))
    with abrir_sessao(cert) as session:
        gerar_pdfs(chaves, pasta, ambiente, session)


def main():
    parser = argparse.ArgumentParser(description="Emissão de NFS-e pelo Sistema Nacional (Sefin Nacional).")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--ambiente", choices=AMBIENTES.keys())
    parser.add_argument("--sem-sync", action="store_true", help="não consulta o ADN; usa só a última NF local")
    parser.add_argument("--importar", metavar="XML", help="pré-preenche a partir de um XML de NFS-e/DPS")
    parser.add_argument("--dry-run", action="store_true", help="gera e assina o XML sem enviar")
    parser.add_argument("--sem-danfse", action="store_true", help="não gera o PDF após emitir")
    parser.add_argument("--pdf", nargs="?", const="", metavar="CHAVE", help="gera o DANFSe em PDF; sem CHAVE, lista as NFS-e locais")
    parser.add_argument("--dont-use-pfx-file", action="store_true", help="não detectar/usar automaticamente um .pfx da pasta ao criar o config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = ler_json(config_path)
    if not cfg:
        exemplo_path = BASE_DIR / "config.json.example"
        if not exemplo_path.exists():
            raise SystemExit(f"Config não encontrada: {config_path}")
        cfg = json.loads(exemplo_path.read_text(encoding="utf-8"))
        print(f"Config não encontrada. Criando {config_path} a partir de config.json.example.")
        if confirmar("Usar ambiente de produção em vez de homologação?", False):
            cfg["ambiente"] = "producao"
        print("Informe os dados do prestador (podem ser ajustados depois em config.json):")

        doc_detectado = None
        if not args.dont_use_pfx_file:
            pfx_encontrados = sorted(BASE_DIR.glob("*.pfx"))
            if len(pfx_encontrados) == 1:
                print(f"Certificado encontrado: {pfx_encontrados[0].name}")
                senha_pfx = getpass.getpass("Senha do certificado: ")
                try:
                    doc_detectado = doc_do_certificado(Certificado(pfx_encontrados[0], senha_pfx))
                except (SystemExit, ValueError) as e:
                    print(f"  não foi possível ler o certificado ({e}); informe o CNPJ/CPF manualmente.")

        cfg["prestador"]["documento"] = doc_detectado or perguntar_regex("CNPJ/CPF do prestador", "", r"\d{11}|\d{14}")

        municipio_sugerido, origem = sugerir_cod_municipio(cfg["prestador"]["documento"])
        if municipio_sugerido:
            print(f"  (sugestão obtida {origem}; pressione Enter para aceitar ou digite outro)")
        cfg["prestador"]["cod_municipio"] = perguntar_regex("Código IBGE do município", municipio_sugerido, r"\d{7}")
        gravar_json(config_path, cfg)
    nome_ambiente = args.ambiente or cfg.get("ambiente", "homologacao")
    ambiente = AMBIENTES[nome_ambiente]
    pasta = BASE_DIR / "dados" / nome_ambiente
    estado_path = pasta / "estado.json"
    recentes_path = pasta / "recentes.json"
    estado = ler_json(estado_path, {})
    recentes = atualizar_recentes(ler_json(recentes_path, []), [])

    validar_config(cfg)

    if args.pdf is not None:
        return comando_pdf(args, cfg, ambiente, pasta)

    cert = abrir_certificado(cfg, config_path)
    print(f"Ambiente:    {nome_ambiente}")
    doc_cert = doc_do_certificado(cert)
    doc_cfg = limpar_doc(cfg["prestador"]["documento"])
    if doc_cert and doc_cert != doc_cfg:
        print(f"Aviso: prestador.documento ({doc_cfg}) difere do documento no certificado ({doc_cert}).", file=sys.stderr)

    with abrir_sessao(cert) as session:
        if not args.sem_sync:
            try:
                recentes = atualizar_recentes(recentes, sincronizar(session, ambiente["adn"], cfg, pasta, estado))
                gravar_json(estado_path, estado)
                gravar_json(recentes_path, recentes)
            except requests.RequestException as e:
                print(f"Falha ao sincronizar com o ADN: {e}", file=sys.stderr)

        if args.importar:
            importada = extrair_dados(Path(args.importar).read_bytes())
            importada["chave"] = importada["chave"] or f"import-{Path(args.importar).stem}"
            recentes = atualizar_recentes(recentes, [importada])
            gravar_json(recentes_path, recentes)

        if not (args.importar or args.dry_run):
            acao = menu_principal()
            if acao == "q":
                return
            if acao == "2":
                return gerar_pdfs(escolher_chaves_pdf(listar_locais(pasta)), pasta, ambiente, session)

        defaults = cfg.get("padroes", {})
        dados = escolher_nf(recentes, cfg, defaults) if recentes else coletar(defaults, cfg)
        print("\n" + resumo(dados))

        n_dps = int(estado.get("ultimo_ndps", 0)) + 1
        if not args.dry_run:
            n_dps = proximo_ndps(session, ambiente["sefin"], cfg, n_dps)
        dps_id = gerar_id(cfg, n_dps)
        xml_assinado = assinar(montar_dps(cfg, dados, n_dps, ambiente["tp_amb"]), cert)

        if args.dry_run:
            destino = pasta / "dry-run" / f"{dps_id}.xml"
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(xml_assinado)
            print(f"\nDPS assinada gravada em {destino}")
            return

        if not confirmar(f"\nEmitir NFS-e (nDPS {n_dps}, {nome_ambiente})?", False):
            print("Cancelado.")
            return

        try:
            resposta = enviar(session, ambiente["sefin"], dps_id, xml_assinado)
        except ErroSefin as e:
            erro_path = pasta / "erros" / f"{dps_id}.xml"
            erro_path.parent.mkdir(parents=True, exist_ok=True)
            erro_path.write_bytes(xml_assinado)
            raise SystemExit(f"\nRejeitada:\n{e}\nDPS enviada: {erro_path}")

        estado["ultimo_ndps"] = n_dps
        gravar_json(estado_path, estado)

        chave = ci(resposta, "chaveAcesso")
        nfse_xml = decodificar_xml(ci(resposta, "nfseXmlGZipB64"))
        xml_path = pasta / "xml" / f"{chave}.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_bytes(nfse_xml)

        registro = extrair_dados(nfse_xml)
        registro["chave"] = chave
        gravar_json(recentes_path, atualizar_recentes(recentes, [registro]))

        for alerta in ci(resposta, "alertas") or []:
            print(f"Alerta: {ci(alerta, 'codigo')} {ci(alerta, 'descricao') or ci(alerta, 'mensagem')}")
        print(f"\nNFS-e emitida. Número: {registro['n_nfse'] or '?'}")
        print(f"Chave:  {chave}")
        print(f"XML:    {xml_path}")
        if not args.sem_danfse:
            print(f"DANFSe: {gerar_pdf(nfse_xml, chave, pasta)}")

main()