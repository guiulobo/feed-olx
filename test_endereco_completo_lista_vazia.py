# -*- coding: utf-8 -*-
"""Decisao de 18/10/2026: nenhum imovel em sigilo, todos com endereco completo.

O segredo PORTAIS_LISTA_SIGILO fica com so um comentario ("#"). O robo segue
com o mecanismo, mas a lista tem zero codigos: todo anuncio sai com
displayAddress="All" e com todos os campos de endereco que vieram do Vista.
Segredo ausente ou em branco continua parando a rodada (nada e publicado).
Dados 100% ficticios.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
CAMPOS = (
    '<Country abbreviation="BR">Brasil</Country>',
    '<State abbreviation="GO">Goias</State>',
    '<City>Cidade Teste</City>',
    '<Neighborhood>Bairro Ficticio</Neighborhood>',
    '<Address>Rua Ficticia de Teste</Address>',
    '<StreetNumber>123</StreetNumber>',
    '<Complement>Apto 45</Complement>',
    '<PostalCode>00000-123</PostalCode>',
    '<Latitude>-10.123456</Latitude>',
    '<Longitude>-40.654321</Longitude>',
)


def _xml(display):
    anuncios = ''.join(
        f'<Listing><ListingID>IM-TESTE-10{n}</ListingID>'
        f'<Title>Apartamento ficticio numero {n} para teste</Title>'
        f'<TransactionType>For Sale</TransactionType>'
        f'<Description>Imovel ficticio.</Description>'
        f'<Location displayAddress="{display}">' + ''.join(CAMPOS) + '</Location>'
        f'<Details><PropertyType>Residential / Apartment</PropertyType>'
        f'<LivingArea>80</LivingArea></Details></Listing>'
        for n in range(5)
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ListingDataFeed xmlns="http://www.vivareal.com/schemas/1.0/VRSync">'
            '<Listings>' + anuncios + '</Listings></ListingDataFeed>\n')


def _rodar(valor, display='Neighborhood'):
    with tempfile.TemporaryDirectory() as pasta:
        entrada = Path(pasta) / 'entrada.xml'
        destino = Path(pasta) / 'saida.xml'
        entrada.write_text(_xml(display), encoding='utf-8')
        env = os.environ.copy()
        env.pop('PORTAIS_LISTA_SIGILO', None)
        if valor is not None:
            env['PORTAIS_LISTA_SIGILO'] = valor
        proc = subprocess.run(
            [sys.executable, str(RAIZ / 'corrige_feed.py'), str(entrada), str(destino)],
            cwd=RAIZ, env=env, capture_output=True, text=True, check=False)
        saida = destino.read_text(encoding='utf-8') if destino.exists() else None
    return proc, saida


def test_comentario_publica_todos_com_endereco_completo():
    for display in ('Neighborhood', 'Street', 'All'):
        proc, saida = _rodar('#', display)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert '0 imóveis em sigilo' in proc.stdout
        assert saida.count('displayAddress="All"') == 5
        assert 'displayAddress="Neighborhood"' not in saida
        assert 'displayAddress="Street"' not in saida
        for campo in CAMPOS:
            assert saida.count(campo) == 5, campo


def test_segredo_ausente_ou_em_branco_nao_publica():
    for valor in (None, '', '   ', '\n\n'):
        proc, saida = _rodar(valor)
        assert proc.returncode != 0
        assert saida is None
        assert 'Nada foi publicado' in proc.stdout


if __name__ == '__main__':
    test_comentario_publica_todos_com_endereco_completo()
    print('ok test_comentario_publica_todos_com_endereco_completo')
    test_segredo_ausente_ou_em_branco_nao_publica()
    print('ok test_segredo_ausente_ou_em_branco_nao_publica')
    print('2 testes passaram')
