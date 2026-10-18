# -*- coding: utf-8 -*-
"""T5 e T6 do cartão 132. Códigos fictícios. Nenhum valor de segredo.

T5: a lista não está em arquivo do robô; a leitura é a variável.
T6: o registro da rodada mostra só a contagem.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import corrige_feed as robo

RAIZ = Path(__file__).resolve().parent
IM_SIGILO = 'IM-TESTE-001'
IM_COMUM = 'IM-TESTE-002'
DUMPS = ('lista_sigilo.csv', 'lista_sigilo.json', 'lista_sigilo.txt')


def test_t5_leitura_e_a_variavel():
    fonte = (RAIZ / 'corrige_feed.py').read_text(encoding='utf-8')
    assert "VARIAVEL_LISTA_SIGILO = 'PORTAIS_LISTA_SIGILO'" in fonte
    assert 'os.environ' in fonte
    assert 'lista_sigilo.csv' not in fonte
    assert 'open(' not in fonte.split('def carrega_lista_sigilo')[1].split('def mensagem_contagem')[0]
    assert 'print(codigos' not in fonte
    assert 'print(LISTA_SIGILO' not in fonte
    assert 'print(texto' not in fonte

    workflow = (RAIZ / '.github' / 'workflows' / 'atualiza-feed.yml').read_text(encoding='utf-8')
    assert workflow.count('PORTAIS_LISTA_SIGILO: ${{ secrets.PORTAIS_LISTA_SIGILO }}') == 2
    assert IM_SIGILO not in workflow
    assert IM_SIGILO not in fonte

    gitignore = (RAIZ / '.gitignore').read_text(encoding='utf-8')
    for nome in DUMPS:
        assert nome in gitignore
        assert not (RAIZ / nome).exists()


def test_t5_rejeita_nome_e_cpf():
    texto = f'{IM_SIGILO}\nBeltrano\n000.000.000-00\n00000000000\nnao vale'
    assert robo.ler_lista_sigilo(texto) == {IM_SIGILO}


def test_t6_so_a_contagem():
    texto = f'{IM_SIGILO}, {IM_COMUM}\n# nota\n'
    codigos, erro = robo.carrega_lista_sigilo({'PORTAIS_LISTA_SIGILO': texto})
    assert erro is None
    linha = robo.mensagem_contagem(codigos)
    assert linha == '2 imóveis em sigilo'
    assert IM_SIGILO not in linha
    assert IM_COMUM not in linha


def test_t6_comentario_e_zero():
    codigos, erro = robo.carrega_lista_sigilo(
        {'PORTAIS_LISTA_SIGILO': '# nenhum\n'}
    )
    assert erro is None
    assert robo.mensagem_contagem(codigos) == '0 imóveis em sigilo'


def test_fail_closed_ausente_e_vazio():
    codigos, erro = robo.carrega_lista_sigilo({})
    assert codigos is None
    assert 'não está configurado' in erro
    assert 'IM-' not in erro
    codigos, erro = robo.carrega_lista_sigilo({'PORTAIS_LISTA_SIGILO': '   \n'})
    assert codigos is None
    assert 'vazio' in erro


def test_rodada_imprime_so_a_contagem():
    """O script para no segredo vazio antes de publicar, e a linha da rodada não leva código."""
    env = os.environ.copy()
    env.pop('PORTAIS_LISTA_SIGILO', None)
    env['PORTAIS_LISTA_SIGILO'] = f'{IM_SIGILO}\n{IM_COMUM}'
    saida = subprocess.run(
        [sys.executable, str(RAIZ / 'corrige_feed.py'), 'entrada-ficticia.xml', 'saida-ficticia.xml'],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    bruto = saida.stdout + saida.stderr
    assert '2 imóveis em sigilo' in bruto
    assert IM_SIGILO not in bruto
    assert IM_COMUM not in bruto
    assert saida.returncode != 0  # faltam os arquivos do feed; a lista já foi só contada


def test_feed_ficticio_grava_sem_vazar_codigo():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListingDataFeed xmlns="http://www.vivareal.com/schemas/1.0/VRSync">
<Listings>
""" + ''.join(
        f'<Listing><ListingID>{IM_SIGILO if n == 0 else "IM-TESTE-00" + str(n)}</ListingID>'
        f'<Details><PropertyType>Residential / Apartment</PropertyType>'
        f'<LivingArea>40</LivingArea></Details></Listing>'
        for n in range(5)
    ) + '</Listings></ListingDataFeed>\n'
    with tempfile.TemporaryDirectory() as pasta:
        entrada = Path(pasta) / 'entrada.xml'
        destino = Path(pasta) / 'saida.xml'
        entrada.write_text(xml, encoding='utf-8')
        env = os.environ.copy()
        env['PORTAIS_LISTA_SIGILO'] = f'# ficticio\n{IM_SIGILO}'
        proc = subprocess.run(
            [sys.executable, str(RAIZ / 'corrige_feed.py'), str(entrada), str(destino)],
            cwd=RAIZ,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        bruto = proc.stdout + proc.stderr
        assert proc.returncode == 0, bruto
        assert '1 imóveis em sigilo' in proc.stdout
        assert IM_SIGILO not in proc.stdout
        assert IM_SIGILO not in proc.stderr
        assert destino.is_file()


def test_codigo_numerico_curto_entra():
    assert robo.codigo_de_sigilo('1001')
    assert not robo.codigo_de_sigilo('00000000000')
    assert not robo.codigo_de_sigilo('Beltrano')


def _anuncio(codigo, com_endereco):
    """Anúncio fictício. O endereço só entra quando o teste pede."""
    if com_endereco:
        local = """
    <Location displayAddress="Street">
      <Country abbreviation="BR">Brasil</Country>
      <State abbreviation="GO">Goiás</State>
      <City>Goiânia</City>
      <Neighborhood>Bairro Teste</Neighborhood>
      <Address>Rua Exemplo</Address>
      <StreetNumber>123</StreetNumber>
      <Complement>Apto 10</Complement>
      <PostalCode>74823-150</PostalCode>
      <Latitude>-16.6869</Latitude>
      <Longitude>-49.2648</Longitude>
      <GeoLocation>
        <Lat>-16.6869</Lat>
        <Long>-49.2648</Long>
      </GeoLocation>
    </Location>"""
        texto = (
            'Visite a Rua Exemplo, 123, Apto 10. '
            'Ponto -16.6869, -49.2648. CEP 74823-150. Bairro Teste.'
        )
    else:
        local = """
    <Location displayAddress="Neighborhood">
      <Country abbreviation="BR">Brasil</Country>
      <State abbreviation="GO">Goiás</State>
      <City>Goiânia</City>
      <Neighborhood>Bairro Teste</Neighborhood>
      <PostalCode>74823-150</PostalCode>
    </Location>"""
        texto = 'Imóvel de enchimento no Bairro Teste.'
    return (
        f'<Listing><ListingID>{codigo}</ListingID>'
        f'<Title>Apartamento no Bairro Teste</Title>'
        f'<TransactionType>For Sale</TransactionType>'
        f'<Description>{texto}</Description>'
        f'{local}'
        f'<Details><PropertyType>Residential / Apartment</PropertyType>'
        f'<LivingArea>80</LivingArea></Details></Listing>'
    )


def _bloco_do(xml, codigo):
    achado = re.search(
        rf'<Listing>\s*<ListingID>{re.escape(codigo)}</ListingID>.*?</Listing>',
        xml,
        re.S,
    )
    assert achado, codigo
    return achado.group(0)


def test_feed_em_sigilo_sai_sem_rua_e_o_comum_fica():
    """Imóvel fictício em sigilo sai sem rua. O de fora segue completo."""
    fonte = (RAIZ / 'corrige_feed.py').read_text(encoding='utf-8')
    assert 'cod in LISTA_SIGILO' in fonte
    assert 'aplicar_sigilo_no_anuncio' in fonte

    miolo = ''.join([
        _anuncio(IM_SIGILO, True),
        _anuncio(IM_COMUM, True),
        _anuncio('IM-TESTE-003', False),
        _anuncio('IM-TESTE-004', False),
        _anuncio('IM-TESTE-005', False),
    ])
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ListingDataFeed xmlns="http://www.vivareal.com/schemas/1.0/VRSync">\n'
        '<Listings>\n' + miolo + '\n</Listings></ListingDataFeed>\n'
    )
    with tempfile.TemporaryDirectory() as pasta:
        entrada = Path(pasta) / 'entrada.xml'
        destino = Path(pasta) / 'saida.xml'
        entrada.write_text(xml, encoding='utf-8')
        env = os.environ.copy()
        env['PORTAIS_LISTA_SIGILO'] = f'# ficticio\n{IM_SIGILO}\n'
        proc = subprocess.run(
            [sys.executable, str(RAIZ / 'corrige_feed.py'), str(entrada), str(destino)],
            cwd=RAIZ,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        bruto = proc.stdout + proc.stderr
        assert proc.returncode == 0, bruto
        assert '1 imóveis em sigilo' in proc.stdout
        assert IM_SIGILO not in proc.stdout
        assert IM_COMUM not in proc.stdout
        saida = destino.read_text(encoding='utf-8')

    sigilo = _bloco_do(saida, IM_SIGILO)
    assert 'displayAddress="Neighborhood"' in sigilo
    assert 'displayAddress="All"' not in sigilo
    for campo in ('Address', 'StreetNumber', 'Complement', 'Latitude', 'Longitude', 'GeoLocation', 'Lat', 'Long'):
        assert f'<{campo}' not in sigilo, campo
    assert 'Rua Exemplo' not in sigilo
    assert 'Apto 10' not in sigilo
    assert '123' not in sigilo
    assert '-16.6869' not in sigilo
    assert '-49.2648' not in sigilo
    assert 'Brasil' in sigilo
    assert 'Goiás' in sigilo
    assert 'Goiânia' in sigilo
    assert 'Bairro Teste' in sigilo
    assert '74823-150' in sigilo

    comum = _bloco_do(saida, IM_COMUM)
    assert 'displayAddress="All"' in comum
    assert '<Address>Rua Exemplo</Address>' in comum
    assert '<StreetNumber>123</StreetNumber>' in comum
    assert '<Complement>Apto 10</Complement>' in comum
    assert '<Latitude>-16.6869</Latitude>' in comum
    assert '<Longitude>-49.2648</Longitude>' in comum
    assert '<GeoLocation>' in comum
    assert 'Rua Exemplo, 123, Apto 10' in comum
    assert '74823-150' in comum
    assert 'Bairro Teste' in comum


if __name__ == '__main__':
    testes = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in testes:
        fn()
        print('ok', fn.__name__)
    print(f'{len(testes)} testes passaram')
