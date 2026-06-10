# -*- coding: utf-8 -*-
"""
Robô de correção do feed VRSync (Lobo Imóveis -> Grupo OLX)
Uso: python3 corrige_feed.py <entrada.xml> <saida.xml>

O que ele faz, em ordem:
1. Lê o XML exportado pelo Vista.
2. Aplica as correções que elevam a nota no Grupo OLX
   (endereço completo, unidades de área, moeda, Iptu, UsageType etc.).
3. Valida o resultado (XML bem-formado, quantidade mínima de anúncios).
4. Só grava a saída se estiver tudo certo. Se algo falhar, sai com erro
   e o arquivo publicado anterior fica intacto.
"""
import re
import sys
import xml.etree.ElementTree as ET

MIN_ANUNCIOS = 5      # abaixo disso, considera feed quebrado e aborta
QUEDA_MAXIMA = 0.5    # se o novo feed tiver menos da metade dos anúncios
                      # do que o publicado anteriormente, aborta por segurança

NS = {'v': 'http://www.vivareal.com/schemas/1.0/VRSync'}

stats = {
    'displayAddress_All': 0, 'area_unit': 0, 'fee_currency': 0,
    'yearlytax_iptu': 0, 'usagetype': 0, 'country': 0,
    'titulo_aparado': 0, 'barbecue': 0, 'yearbuilt0_removido': 0,
    'fotos_aparadas': 0,
}

def fix_block(blk: str) -> str:
    g = lambda pat: (re.search(pat, blk, re.S).group(1).strip()
                     if re.search(pat, blk, re.S) else '')
    pt = g(r'<PropertyType>(.*?)</PropertyType>')
    usage = 'Commercial' if pt.startswith('Commercial') else 'Residential'

    # 1) Endereço completo (maior alavanca de nota: 35 pontos)
    if re.search(r'displayAddress="(?!All")[^"]*"', blk):
        stats['displayAddress_All'] += 1
        blk = re.sub(r'displayAddress="[^"]*"', 'displayAddress="All"', blk)

    # 2) Áreas com unidade obrigatória
    n = len(re.findall(r'<LivingArea>', blk)) + len(re.findall(r'<LotArea>', blk))
    stats['area_unit'] += n
    blk = re.sub(r'<LivingArea>', '<LivingArea unit="square metres">', blk)
    blk = re.sub(r'<LotArea>', '<LotArea unit="square metres">', blk)

    # 3) Condomínio com moeda
    if '<PropertyAdministrationFee>' in blk:
        stats['fee_currency'] += 1
        blk = blk.replace('<PropertyAdministrationFee>',
                          '<PropertyAdministrationFee currency="BRL">')

    # 4) Campo de IPTU atualizado (YearlyTax foi descontinuado)
    if '<YearlyTax>' in blk:
        stats['yearlytax_iptu'] += 1
        blk = re.sub(r'<YearlyTax>(.*?)</YearlyTax>',
                     r'<Iptu currency="BRL" period="Yearly">\1</Iptu>', blk, flags=re.S)

    # 5) Feature malformada
    if '<Feature>BarbecueBalcony</Feature>' in blk:
        stats['barbecue'] += 1
        blk = blk.replace('<Feature>BarbecueBalcony</Feature>',
                          '<Feature>Barbecue Balcony</Feature>')

    # 6) YearBuilt=0 gera aviso no portal: remove
    if re.search(r'<YearBuilt>0</YearBuilt>', blk):
        stats['yearbuilt0_removido'] += 1
        blk = re.sub(r'\n[ \t]*<YearBuilt>0</YearBuilt>', '', blk)

    # 7) UsageType correto, derivado do PropertyType
    if re.search(r'<UsageType>(?!(?:Residential|Commercial)</UsageType>).*?</UsageType>'
                 r'|<UsageType\s*/>', blk, re.S):
        stats['usagetype'] += 1
        blk = re.sub(r'<UsageType>.*?</UsageType>',
                     f'<UsageType>{usage}</UsageType>', blk, flags=re.S)
        blk = re.sub(r'<UsageType\s*/>', f'<UsageType>{usage}</UsageType>', blk)

    # 8) Country: insere se faltar, preenche se vazio
    if '<Country' not in blk:
        m = re.search(r'(<Location[^>]*>)([\r\n]+)([ \t]*)', blk)
        if m:
            blk = (blk[:m.end()]
                   + f'<Country abbreviation="BR">Brasil</Country>{m.group(2)}{m.group(3)}'
                   + blk[m.end():])
            stats['country'] += 1
    else:
        nb = re.sub(r'<Country([^>]*)></Country>', r'<Country\1>Brasil</Country>', blk)
        nb = re.sub(r'<Country([^>]*?)\s*/>', r'<Country\1>Brasil</Country>', nb)
        if nb != blk:
            stats['country'] += 1
            blk = nb

    # 9) Títulos com mais de 100 caracteres: apara no último espaço
    def trim(inner):
        if len(inner) <= 100:
            return None
        cut = inner[:100]
        if ' ' in cut:
            cut = cut[:cut.rfind(' ')]
        return cut.rstrip(' ,;.-')

    def t_cdata(mt):
        new = trim(mt.group(1))
        if new is None:
            return mt.group(0)
        stats['titulo_aparado'] += 1
        return f'<Title><![CDATA[{new}]]></Title>'
    blk = re.sub(r'<Title><!\[CDATA\[(.*?)\]\]></Title>', t_cdata, blk, flags=re.S)

    def t_plain(mt):
        new = trim(mt.group(1))
        if new is None:
            return mt.group(0)
        stats['titulo_aparado'] += 1
        return f'<Title>{new}</Title>'
    blk = re.sub(r'<Title>(?!<!\[CDATA)(.*?)</Title>', t_plain, blk, flags=re.S)

    # 10) Máximo de 50 fotos por anúncio
    its = list(re.finditer(r'[\r\n]+[ \t]*<Item\b[^>]*>.*?</Item>', blk, re.S))
    if len(its) > 50:
        stats['fotos_aparadas'] += 1
        for m in reversed(its[50:]):
            blk = blk[:m.start()] + blk[m.end():]
    return blk

def conta_listings_arquivo(path: str) -> int:
    try:
        r = ET.parse(path).getroot()
        return len(r.findall('.//v:Listing', NS))
    except Exception:
        return -1

def main():
    if len(sys.argv) != 3:
        print('Uso: python3 corrige_feed.py <entrada.xml> <saida.xml>')
        sys.exit(2)
    entrada, saida = sys.argv[1], sys.argv[2]

    # --- Lê a entrada ---
    try:
        xml = open(entrada, encoding='utf-8').read()
    except UnicodeDecodeError:
        xml = open(entrada, encoding='latin-1').read()

    if '<ListingDataFeed' not in xml:
        print('ERRO: o arquivo baixado não parece ser um feed VRSync '
              '(<ListingDataFeed> não encontrado). Nada foi publicado.')
        sys.exit(1)

    # --- Garante a declaração XML na primeira linha (exigência do Grupo OLX) ---
    xml = xml.lstrip('\ufeff\r\n \t')
    if not xml.startswith('<?xml'):
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml
        print('Aviso: declaração <?xml ...?> ausente na origem; foi adicionada.')

    # --- Valida a ENTRADA antes de mexer ---
    n_in = len(re.findall(r'<Listing>', xml))
    if n_in < MIN_ANUNCIOS:
        print(f'ERRO: o feed baixado tem só {n_in} anúncios '
              f'(mínimo de segurança: {MIN_ANUNCIOS}). Nada foi publicado.')
        sys.exit(1)

    # --- Proteção contra queda brusca em relação ao publicado ---
    n_atual = conta_listings_arquivo(saida)
    if n_atual > 0 and n_in < n_atual * QUEDA_MAXIMA:
        print(f'ERRO: o novo feed tem {n_in} anúncios, mas o publicado tem '
              f'{n_atual}. Queda acima de {int(QUEDA_MAXIMA*100)}% — pode ser '
              'export parcial do Vista. Nada foi publicado. '
              '(Se a redução for proposital, ajuste QUEDA_MAXIMA no script.)')
        sys.exit(1)

    # --- Aplica as correções ---
    out = re.sub(r'<Listing>.*?</Listing>',
                 lambda m: fix_block(m.group(0)), xml, flags=re.S)

    # --- Valida a SAÍDA (bem-formada e com a mesma quantidade) ---
    tmp = saida + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(out)
    n_out = conta_listings_arquivo(tmp)
    if n_out != n_in:
        print(f'ERRO: validação falhou (entrada {n_in} x saída {n_out} '
              'anúncios, ou XML malformado). Nada foi publicado.')
        sys.exit(1)

    import os
    os.replace(tmp, saida)

    print(f'OK: {n_out} anúncios corrigidos e validados.')
    print('Correções aplicadas nesta rodada:')
    for k, v in stats.items():
        print(f'  - {k}: {v}')

if __name__ == '__main__':
    main()
