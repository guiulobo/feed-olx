# -*- coding: utf-8 -*-
"""
Robô de correção do feed VRSync (Lobo Imóveis -> Grupo OLX) — v3
Uso: python3 corrige_feed.py <entrada.xml> <saida.xml> [destaques.csv]

O que ele faz, em ordem:
1. Lê o XML exportado pelo Vista.
2. Aplica as correções que elevam a nota no Grupo OLX
   (endereço completo, unidades de área, moeda, Iptu, UsageType etc.).
3. Aplica o tipo de destaque (PublicationType) de cada anúncio conforme o
   CSV indicado (por padrão destaques.csv) versionado no repositório.
   Valores aceitos: STANDARD, PREMIUM, SUPER_PREMIUM, PREMIERE_1, PREMIERE_2, TRIPLE.
4. Valida o resultado (XML bem-formado, quantidade mínima de anúncios).
5. Só grava a saída se estiver tudo certo. Se algo falhar, sai com erro
   e o arquivo publicado anterior fica intacto.
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

MIN_ANUNCIOS = 5          # abaixo disso, considera feed quebrado e aborta
QUEDA_MAXIMA = 0.5        # queda acima de 50% vs publicado anterior aborta

NS = {'v': 'http://www.vivareal.com/schemas/1.0/VRSync'}
TIERS_VALIDOS = {'STANDARD', 'PREMIUM', 'SUPER_PREMIUM',
                 'PREMIERE_1', 'PREMIERE_2', 'TRIPLE'}

stats = {
    'displayAddress_All': 0, 'area_unit': 0, 'fee_currency': 0,
    'yearlytax_iptu': 0, 'usagetype': 0, 'country': 0,
    'titulo_aparado': 0, 'barbecue': 0, 'yearbuilt0_removido': 0,
    'fotos_aparadas': 0, 'publicationtype_aplicado': 0,
}
DESTAQUES = {}
aplicados_por_tier = {}
ids_no_feed = set()


def carrega_destaques(nome_csv='destaques.csv'):
    """Lê o CSV de destaques (por padrão destaques.csv) ao lado deste script."""
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), nome_csv)
    if not os.path.exists(caminho):
        print(f'Aviso: {nome_csv} não encontrado — os destaques do XML '
              'não serão alterados nesta rodada.')
        return
    invalidos = 0
    with open(caminho, encoding='utf-8') as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith('#') or linha.lower().startswith('codigo'):
                continue
            # Formato compacto: TIER: cod1 cod2 cod3 ...  (uma linha por tipo)
            mcomp = re.match(r'^([A-Z_0-9]+)\s*:\s*(.+)$', linha)
            if mcomp and mcomp.group(1).upper() in TIERS_VALIDOS:
                tier = mcomp.group(1).upper()
                for cod in re.split(r'[\s,;]+', mcomp.group(2).strip()):
                    if cod:
                        DESTAQUES[cod] = tier
                continue
            # Formato linha a linha: codigo,tier
            partes = [p.strip() for p in linha.replace(';', ',').split(',')]
            if len(partes) < 2:
                continue
            cod, tier = partes[0], partes[1].upper()
            if tier in TIERS_VALIDOS and cod:
                DESTAQUES[cod] = tier
            else:
                invalidos += 1
    print(f'{nome_csv} carregado: {len(DESTAQUES)} códigos'
          + (f' ({invalidos} linhas inválidas ignoradas)' if invalidos else ''))


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

    # 11) Tipo de destaque (PublicationType) conforme o CSV
    mid = re.search(r'<ListingID>(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?</ListingID>',
                    blk, re.S)
    if mid:
        cod = mid.group(1).strip()
        ids_no_feed.add(cod)
        tier = DESTAQUES.get(cod, 'STANDARD')   # quem não está no CSV vira STANDARD
        novo = f'<PublicationType>{tier}</PublicationType>'
        if re.search(r'<PublicationType\b', blk):
            blk2 = re.sub(r'<PublicationType>.*?</PublicationType>', novo, blk, flags=re.S)
            blk2 = re.sub(r'<PublicationType\s*/>', novo, blk2)
        else:
            blk2 = re.sub(r'(</TransactionType>)([\r\n]+[ \t]*)',
                          r'\1\2' + novo + r'\2', blk, count=1)
        if blk2 != blk:
            blk = blk2
            if tier != 'STANDARD':
                stats['publicationtype_aplicado'] += 1
                aplicados_por_tier[tier] = aplicados_por_tier.get(tier, 0) + 1
    return blk


def conta_listings_arquivo(path: str) -> int:
    try:
        r = ET.parse(path).getroot()
        return len(r.findall('.//v:Listing', NS))
    except Exception:
        return -1


def main():
    if len(sys.argv) not in (3, 4):
        print('Uso: python3 corrige_feed.py <entrada.xml> <saida.xml> [destaques.csv]')
        sys.exit(2)
    entrada, saida = sys.argv[1], sys.argv[2]
    nome_csv = sys.argv[3] if len(sys.argv) == 4 else 'destaques.csv'

    carrega_destaques(nome_csv)

    try:
        xml = open(entrada, encoding='utf-8').read()
    except UnicodeDecodeError:
        xml = open(entrada, encoding='latin-1').read()

    if '<ListingDataFeed' not in xml:
        print('ERRO: o arquivo baixado não parece ser um feed VRSync '
              '(<ListingDataFeed> não encontrado). Nada foi publicado.')
        sys.exit(1)

    xml = xml.lstrip('\ufeff\r\n \t')
    if not xml.startswith('<?xml'):
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml
        print('Aviso: declaração <?xml ...?> ausente na origem; foi adicionada.')

    n_in = len(re.findall(r'<Listing>', xml))
    if n_in < MIN_ANUNCIOS:
        print(f'ERRO: o feed baixado tem só {n_in} anúncios '
              f'(mínimo de segurança: {MIN_ANUNCIOS}). Nada foi publicado.')
        sys.exit(1)

    n_atual = conta_listings_arquivo(saida)
    if n_atual > 0 and n_in < n_atual * QUEDA_MAXIMA:
        print(f'ERRO: o novo feed tem {n_in} anúncios, mas o publicado tem '
              f'{n_atual}. Queda acima de {int(QUEDA_MAXIMA*100)}% — pode ser '
              'export parcial do Vista. Nada foi publicado.')
        sys.exit(1)

    out = re.sub(r'<Listing>.*?</Listing>',
                 lambda m: fix_block(m.group(0)), xml, flags=re.S)

    tmp = saida + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(out)
    n_out = conta_listings_arquivo(tmp)
    if n_out != n_in:
        print(f'ERRO: validação falhou (entrada {n_in} x saída {n_out} '
              'anúncios, ou XML malformado). Nada foi publicado.')
        sys.exit(1)

    os.replace(tmp, saida)

    print(f'OK: {n_out} anúncios corrigidos e validados.')
    print('Correções aplicadas nesta rodada:')
    for k, v in stats.items():
        print(f'  - {k}: {v}')
    if aplicados_por_tier:
        print('Destaques aplicados por tipo:')
        for t in ['PREMIERE_1', 'PREMIERE_2', 'SUPER_PREMIUM', 'TRIPLE',
                  'PREMIUM', 'STANDARD']:
            if t in aplicados_por_tier:
                print(f'  - {t}: {aplicados_por_tier[t]}')


if __name__ == '__main__':
    main()
