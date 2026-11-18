"""Rodada das duas contas, sem rede e sem segredo real.

As URLs são fictícias. O marcador no parâmetro key não pode aparecer
em log, issue, commit ou exceção visível.
"""

import io
import os
import runpy
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest

import orquestra_feed as orq

RAIZ = Path(__file__).resolve().parent
MARCADOR = 'SEGREDO-FALSO'
URL_HUGO = f'https://exemplo.invalid/hugo?key={MARCADOR}'
URL_LOBO = f'https://exemplo.invalid/lobo?key={MARCADOR}'
CODIGO_SIGILO = 'ZZ-SIGILO-9'
RUN_URL = 'https://github.com/guiulobo/feed-olx/actions/runs/12345'
AGORA = datetime(2026, 11, 17, 23, 58, tzinfo=UTC)
AGORA_2 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
ANTERIOR_HUGO = b'ANTERIOR-HUGO\n'
ANTERIOR_LOBO = b'ANTERIOR-LOBO\n'


class Proc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class MemoriaIssues:
    """Issue em memória: a segunda falha comenta, não abre outra."""

    def __init__(self):
        self.abertas = []
        self.proximo = 1
        self.criadas = []
        self.comentarios = []
        self.fechadas = []
        self.corpos = {}

    def buscar_aberta(self):
        for item in self.abertas:
            if item['aberta']:
                return item['numero']
        return None

    def criar(self, corpo):
        numero = self.proximo
        self.proximo += 1
        self.abertas.append({'numero': numero, 'aberta': True, 'corpo': corpo})
        self.criadas.append(numero)
        self.corpos[numero] = corpo
        return numero

    def comentar(self, numero, corpo):
        self.comentarios.append((numero, corpo))

    def atualizar_corpo(self, numero, corpo):
        self.corpos[numero] = corpo
        for item in self.abertas:
            if item['numero'] == numero:
                item['corpo'] = corpo

    def fechar(self, numero):
        self.fechadas.append(numero)
        for item in self.abertas:
            if item['numero'] == numero:
                item['aberta'] = False


def ambiente():
    """Cópia sem os secrets do processo. A lista e as URLs são fictícias."""
    env = os.environ.copy()
    for chave in (
        'VISTA_FEED_URL',
        'VISTA_FEED_URL_LOBO',
        'PORTAIS_LISTA_SIGILO',
        'GITHUB_TOKEN',
        'GH_TOKEN',
        'FEED_RUN_URL',
    ):
        env.pop(chave, None)
    env['VISTA_FEED_URL'] = URL_HUGO
    env['VISTA_FEED_URL_LOBO'] = URL_LOBO
    env['PORTAIS_LISTA_SIGILO'] = f'#\n{CODIGO_SIGILO}\n'
    return env


def xml_vrsync(qtd=5):
    blocos = []
    for n in range(qtd):
        blocos.append(
            '<Listing>\n'
            f'  <ListingID>IM-TESTE-{n:03d}</ListingID>\n'
            '  <Title>Imovel ficticio de teste</Title>\n'
            '  <TransactionType>For Sale</TransactionType>\n'
            '  <Details>\n'
            '    <PropertyType>Residential / Apartment</PropertyType>\n'
            '    <LivingArea>40</LivingArea>\n'
            '  </Details>\n'
            '</Listing>\n'
        )
    texto = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ListingDataFeed xmlns="http://www.vivareal.com/schemas/1.0/VRSync">\n'
        '<Listings>\n' + ''.join(blocos) + '</Listings>\n</ListingDataFeed>\n'
    )
    ET.fromstring(texto)
    return texto.encode('utf-8')


def xml_invalido():
    itens = ''.join(f'<Listing><ListingID>IM-TESTE-{n:03d}</ListingID></Listing>' for n in range(5))
    return (
        '<?xml version="1.0"?>'
        '<ListingDataFeed xmlns="http://www.vivareal.com/schemas/1.0/VRSync">'
        f'<Listings>{itens}<quebrado></Listings></ListingDataFeed>'
    ).encode()


def baixar_mapa(mapa):
    def baixar(url, destino):
        conteudo = mapa[url]
        if isinstance(conteudo, Exception):
            raise conteudo
        destino.write_bytes(conteudo)

    return baixar


def juntar(resultado, memoria):
    partes = list(resultado.logs)
    for conta in resultado.contas:
        partes.append(conta.motivo or '')
    if memoria is None:
        return '\n'.join(partes)
    for item in memoria.abertas:
        partes.append(item.get('corpo') or '')
    for _numero, corpo in memoria.comentarios:
        partes.append(corpo)
    for corpo in memoria.corpos.values():
        partes.append(corpo)
    return '\n'.join(partes)


def assert_limpo(resultado, memoria):
    bruto = juntar(resultado, memoria)
    assert MARCADOR not in bruto, 'o marcador da URL apareceu na saída'
    assert CODIGO_SIGILO not in bruto, 'código de sigilo apareceu na saída'
    assert 'PORTAIS_LISTA_SIGILO' not in bruto
    assert 'Nada foi publicado' not in bruto
    assert 'imóveis em sigilo' not in bruto
    assert 'anúncios corrigidos' not in bruto
    assert '[redigido]' not in bruto


def preparar(tmp_path, hugo=ANTERIOR_HUGO, lobo=ANTERIOR_LOBO):
    (tmp_path / 'feed-hugo.xml').write_bytes(hugo)
    (tmp_path / 'feed-lobo.xml').write_bytes(lobo)


def rodar(
    tmp_path,
    mapa,
    *,
    agora=AGORA,
    cliente='auto',
    publicar=None,
    baixar=None,
    corrigir=None,
    environ=None,
    hugo=ANTERIOR_HUGO,
    lobo=ANTERIOR_LOBO,
    checar=True,
):
    preparar(tmp_path, hugo, lobo)
    memoria = MemoriaIssues() if cliente == 'auto' else cliente
    registros = []
    env = ambiente() if environ is None else environ

    def publicar_envelope(arquivos, diretorio, mensagem, proibidos):
        registros.append(
            {
                'arquivos': [Path(item).name for item in arquivos],
                'mensagem': mensagem,
            }
        )
        if publicar is None:
            return True
        return publicar(arquivos, diretorio, mensagem, proibidos)

    resultado = orq.executar_rodada(
        tmp_path,
        {'VISTA_FEED_URL': URL_HUGO, 'VISTA_FEED_URL_LOBO': URL_LOBO},
        baixar=baixar or baixar_mapa(mapa),
        corrigir=corrigir,
        cliente=memoria,
        publicar=publicar_envelope,
        agora=agora,
        run_url=RUN_URL,
        proibidos=orq.segredos_do_ambiente(env),
        environ=env,
    )
    if checar:
        assert_limpo(resultado, memoria)
    return resultado, memoria, registros


def test_sucesso_total_nao_abre_issue(tmp_path, monkeypatch):
    originais = []
    real = tempfile.mkstemp

    def vigia(*args, **kwargs):
        fd, nome = real(*args, **kwargs)
        originais.append(nome)
        return fd, nome

    monkeypatch.setattr(tempfile, 'mkstemp', vigia)
    dados = xml_vrsync()
    resultado, memoria, pubs = rodar(tmp_path, {URL_HUGO: dados, URL_LOBO: dados})
    assert resultado.codigo == 0
    assert resultado.acao_issue == 'nenhuma'
    assert memoria.criadas == []
    assert memoria.comentarios == []
    assert memoria.fechadas == []
    assert pubs[0]['arquivos'] == ['feed-hugo.xml', 'feed-lobo.xml']
    assert pubs[0]['mensagem'] == ('Atualiza feeds corrigidos — HUGO, LOBO (17/11/2026 23:58 UTC)')
    hugo = (tmp_path / 'feed-hugo.xml').read_bytes()
    lobo = (tmp_path / 'feed-lobo.xml').read_bytes()
    assert hugo != ANTERIOR_HUGO
    assert lobo != ANTERIOR_LOBO
    assert b'ListingDataFeed' in hugo
    assert b'PublicationType' in lobo
    assert originais
    assert all(not Path(nome).exists() for nome in originais)
    assert 'Rodada concluída com sucesso.' in resultado.logs


def test_sucesso_total_fecha_a_issue_aberta(tmp_path):
    memoria = MemoriaIssues()
    memoria.abertas.append({'numero': 4, 'aberta': True, 'corpo': 'falha antiga'})
    memoria.proximo = 5
    dados = xml_vrsync()
    resultado, memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        cliente=memoria,
    )
    assert resultado.codigo == 0
    assert resultado.acao_issue == 'fechada'
    assert memoria.criadas == []
    assert memoria.fechadas == [4]
    assert memoria.abertas[0]['aberta'] is False
    texto = memoria.comentarios[0][1]
    assert 'HUGO e LOBO passaram' in texto
    assert 'sendo fechada' in texto
    assert '17/11/2026 23:58 UTC' in texto
    assert '17/11/2026 20:58 Brasília' in texto
    assert RUN_URL in texto


def test_falha_isolada_da_hugo_publica_a_lobo(tmp_path):
    ordem = []
    memoria = MemoriaIssues()
    buscar = memoria.buscar_aberta
    criar = memoria.criar

    def buscar_wrap():
        ordem.append('buscar')
        return buscar()

    def criar_wrap(corpo):
        ordem.append('criar')
        return criar(corpo)

    memoria.buscar_aberta = buscar_wrap
    memoria.criar = criar_wrap
    vistos = []

    def baixar(url, destino):
        vistos.append(url)
        assert Path(destino).resolve().parent != tmp_path.resolve()
        destino.write_bytes(b'' if url == URL_HUGO else xml_vrsync())

    def publicar(*_args):
        ordem.append('publicar')
        return True

    resultado, memoria, pubs = rodar(
        tmp_path,
        {},
        cliente=memoria,
        baixar=baixar,
        publicar=publicar,
    )
    assert vistos == [URL_HUGO, URL_LOBO]
    assert ordem == ['publicar', 'buscar', 'criar']
    assert resultado.codigo == 1
    assert resultado.acao_issue == 'aberta'
    assert [conta.motivo for conta in resultado.contas] == [orq.MOTIVO_VAZIO, None]
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert (tmp_path / 'feed-hugo.xml').is_file()
    assert b'ListingDataFeed' in (tmp_path / 'feed-lobo.xml').read_bytes()
    assert pubs[0]['arquivos'] == ['feed-lobo.xml']
    assert 'HUGO' not in pubs[0]['mensagem']
    assert MARCADOR not in pubs[0]['mensagem']
    corpo = memoria.corpos[1]
    assert memoria.criadas == [1]
    assert 'HUGO: falhou (vazio)' in corpo
    assert 'último bom' in corpo
    assert 'LOBO: ok' in corpo
    assert 'LOBO: falhou' not in corpo
    assert '17/11/2026 23:58 UTC' in corpo
    assert '17/11/2026 20:58 Brasília' in corpo
    assert RUN_URL in corpo
    assert 'Rodada com falha.' in resultado.logs


def test_falha_isolada_da_lobo_preserva_o_xml(tmp_path):
    dados = xml_vrsync()
    resultado, memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: xml_invalido()},
    )
    assert resultado.codigo == 1
    assert [conta.motivo for conta in resultado.contas] == [None, orq.MOTIVO_XML]
    assert (tmp_path / 'feed-lobo.xml').read_bytes() == ANTERIOR_LOBO
    assert not (tmp_path / 'feed-lobo.xml.tmp').exists()
    assert b'ListingDataFeed' in (tmp_path / 'feed-hugo.xml').read_bytes()
    assert pubs[0]['arquivos'] == ['feed-hugo.xml']
    corpo = memoria.corpos[1]
    assert 'LOBO: falhou (XML inválido)' in corpo
    assert 'HUGO: ok' in corpo
    assert 'último bom' in corpo
    assert RUN_URL in corpo


def test_falha_dupla_nao_altera_arquivo_nem_abre_duas_issues(tmp_path):
    resultado, memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: b'', URL_LOBO: b'<html>nao e feed</html>'},
    )
    assert resultado.codigo == 1
    assert [conta.motivo for conta in resultado.contas] == [
        orq.MOTIVO_VAZIO,
        orq.MOTIVO_NAO_VRSYNC,
    ]
    assert pubs == []
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert (tmp_path / 'feed-lobo.xml').read_bytes() == ANTERIOR_LOBO
    assert memoria.criadas == [1]
    corpo = memoria.corpos[1]
    assert 'HUGO: falhou (vazio)' in corpo
    assert 'LOBO: falhou (não é VRSync)' in corpo
    assert 'último bom' in corpo


def test_falha_repetida_comenta_a_mesma_issue(tmp_path):
    memoria = MemoriaIssues()
    mapa = {URL_HUGO: b'', URL_LOBO: xml_vrsync()}
    primeira, memoria, _pubs = rodar(tmp_path, mapa, cliente=memoria, agora=AGORA)
    segunda, memoria, _pubs = rodar(tmp_path, mapa, cliente=memoria, agora=AGORA_2)
    assert primeira.acao_issue == 'aberta'
    assert segunda.acao_issue == 'comentada'
    assert segunda.codigo == 1
    assert memoria.criadas == [1]
    assert len(memoria.comentarios) == 1
    assert memoria.comentarios[0][0] == 1
    assert '18/11/2026 12:00 UTC' in memoria.comentarios[0][1]
    assert '18/11/2026 09:00 Brasília' in memoria.comentarios[0][1]
    assert memoria.corpos[1] == memoria.comentarios[0][1]
    assert 'HUGO: falhou (vazio)' in memoria.corpos[1]


def test_curl_nao_deixa_o_marcador_vazar(tmp_path, capsys):
    def baixar(url, destino):
        def executar(cmd, **kwargs):
            assert kwargs.get('shell') is False
            return Proc(22, '', f'curl: (22) The requested URL {url} returned error')

        if url == URL_HUGO:
            orq.baixar_curl(url, destino, executar=executar)
            return
        destino.write_bytes(xml_vrsync())

    resultado, memoria, _pubs = rodar(tmp_path, {}, baixar=baixar)
    assert resultado.contas[0].motivo == orq.MOTIVO_DOWNLOAD
    assert resultado.contas[1].ok
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    capturado = capsys.readouterr()
    assert MARCADOR not in capturado.out
    assert MARCADOR not in capturado.err
    assert MARCADOR not in memoria.corpos[1]
    assert 'falha no download' in memoria.corpos[1]
    assert 'LOBO: ok' in memoria.corpos[1]


def test_excecao_do_download_nao_entra_no_log(tmp_path):
    def baixar(url, destino):
        if url == URL_HUGO:
            raise RuntimeError(f'Could not fetch {url}')
        destino.write_bytes(xml_vrsync())

    resultado, _memoria, pubs = rodar(tmp_path, {}, baixar=baixar)
    assert resultado.contas[0].motivo == orq.MOTIVO_DOWNLOAD
    assert resultado.contas[1].ok
    assert pubs[0]['arquivos'] == ['feed-lobo.xml']
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO


def test_saida_valida_igual_a_do_corrige_feed(tmp_path):
    dados = xml_vrsync()
    env = ambiente()
    entrada = tmp_path / 'entrada.xml'
    direto = tmp_path / 'direto.xml'
    entrada.write_bytes(dados)
    codigo, saida, erro = orq.corrigir_padrao(
        entrada,
        direto,
        RAIZ / 'destaques_lobo.csv',
        env,
    )
    assert codigo == 0, saida + erro
    assert CODIGO_SIGILO not in saida
    assert MARCADOR not in saida
    _resultado, _memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
    )
    assert (tmp_path / 'feed-lobo.xml').read_bytes() == direto.read_bytes()
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == direto.read_bytes()


def test_minimo_e_queda_preservam_o_arquivo(tmp_path):
    anterior = xml_vrsync(12)
    resultado, memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: xml_vrsync(4), URL_LOBO: xml_vrsync(5)},
        hugo=anterior,
        lobo=anterior,
    )
    assert [conta.motivo for conta in resultado.contas] == [
        orq.MOTIVO_MINIMO,
        orq.MOTIVO_QUEDA,
    ]
    assert resultado.codigo == 1
    assert pubs == []
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == anterior
    assert (tmp_path / 'feed-lobo.xml').read_bytes() == anterior
    corpo = memoria.corpos[1]
    assert 'mínimo' in corpo
    assert 'queda' in corpo


def test_lista_ausente_nao_publica_e_nao_copia_o_erro(tmp_path):
    env = ambiente()
    env.pop('PORTAIS_LISTA_SIGILO')
    dados = xml_vrsync()
    resultado, memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        environ=env,
    )
    assert resultado.codigo == 1
    assert pubs == []
    assert [conta.motivo for conta in resultado.contas] == [
        orq.MOTIVO_LISTA,
        orq.MOTIVO_LISTA,
    ]
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert (tmp_path / 'feed-lobo.xml').read_bytes() == ANTERIOR_LOBO
    assert 'lista de sigilo' in memoria.corpos[1]


def test_url_ausente_preserva_o_que_ja_estava(tmp_path):
    preparar(tmp_path)
    env = ambiente()
    vistos = []

    def baixar(url, destino):
        vistos.append(url)
        destino.write_bytes(xml_vrsync())

    memoria = MemoriaIssues()
    resultado = orq.executar_rodada(
        tmp_path,
        {'VISTA_FEED_URL': '', 'VISTA_FEED_URL_LOBO': URL_LOBO},
        baixar=baixar,
        cliente=memoria,
        publicar=lambda *args: True,
        agora=AGORA,
        run_url=RUN_URL,
        proibidos=orq.segredos_do_ambiente(env),
        environ=env,
    )
    assert vistos == [URL_LOBO]
    assert resultado.codigo == 1
    assert resultado.contas[0].motivo == orq.MOTIVO_URL
    assert resultado.contas[1].ok
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert 'URL não configurada' in memoria.corpos[1]
    assert 'LOBO: ok' in memoria.corpos[1]
    assert_limpo(resultado, memoria)


def test_corretor_destrutivo_nao_grava_por_cima(tmp_path):
    def corrigir(entrada, saida, destaques, environ):
        Path(saida).write_bytes(b'ESTRAGADO')
        return 1, 'ERRO: mínimo de segurança: 5', ''

    resultado, _memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: xml_vrsync(), URL_LOBO: xml_vrsync()},
        corrigir=corrigir,
    )
    assert pubs == []
    assert resultado.contas[0].motivo == orq.MOTIVO_MINIMO
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert (tmp_path / 'feed-lobo.xml').read_bytes() == ANTERIOR_LOBO


def test_corretor_que_apaga_o_arquivo_e_desfeito(tmp_path):
    def corrigir(entrada, saida, destaques, environ):
        Path(saida).unlink(missing_ok=True)
        return 1, 'não parece ser um feed VRSync', ''

    resultado, _memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: xml_vrsync(), URL_LOBO: b'nada'},
        corrigir=corrigir,
    )
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert resultado.contas[0].motivo == orq.MOTIVO_NAO_VRSYNC


def test_falha_sem_arquivo_previo_nao_deixa_sobra(tmp_path):
    def corrigir(entrada, saida, destaques, environ):
        Path(saida).write_bytes(b'NAO-DEVIA')
        return 1, 'não parece ser um feed VRSync', ''

    env = ambiente()
    resultado = orq.executar_rodada(
        tmp_path,
        {'VISTA_FEED_URL': URL_HUGO, 'VISTA_FEED_URL_LOBO': ''},
        baixar=baixar_mapa({URL_HUGO: xml_vrsync()}),
        corrigir=corrigir,
        cliente=MemoriaIssues(),
        publicar=lambda *args: True,
        agora=AGORA,
        run_url=RUN_URL,
        proibidos=orq.segredos_do_ambiente(env),
        environ=env,
    )
    assert not (tmp_path / 'feed-hugo.xml').exists()
    assert not (tmp_path / 'feed-lobo.xml').exists()
    assert resultado.contas[0].motivo == orq.MOTIVO_NAO_VRSYNC
    assert resultado.contas[1].motivo == orq.MOTIVO_URL


def test_excecao_do_corretor_nao_trava_a_outra(tmp_path):
    def corrigir(entrada, saida, destaques, environ):
        if Path(saida).name == 'feed-hugo.xml':
            raise RuntimeError(URL_HUGO)
        return orq.corrigir_padrao(entrada, saida, destaques, environ)

    resultado, _memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: xml_vrsync(), URL_LOBO: xml_vrsync()},
        corrigir=corrigir,
    )
    assert resultado.contas[0].motivo == orq.MOTIVO_INTERNO
    assert resultado.contas[1].ok
    assert pubs[0]['arquivos'] == ['feed-lobo.xml']
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO


def test_quebra_fora_da_conta_nao_impede_a_outra(tmp_path, monkeypatch):
    real = orq.processar_conta

    def quebra(*args, **kwargs):
        if args[0].nome == 'HUGO':
            raise RuntimeError(URL_HUGO)
        return real(*args, **kwargs)

    monkeypatch.setattr(orq, 'processar_conta', quebra)
    resultado, _memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: xml_vrsync(), URL_LOBO: xml_vrsync()},
    )
    assert resultado.contas[0].motivo == orq.MOTIVO_INTERNO
    assert resultado.contas[1].ok
    assert pubs[0]['arquivos'] == ['feed-lobo.xml']


def test_temporario_indisponivel_isola_as_contas(tmp_path, monkeypatch):
    def quebra(*args, **kwargs):
        raise OSError('sem espaco')

    monkeypatch.setattr(tempfile, 'mkstemp', quebra)
    resultado, memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: xml_vrsync(), URL_LOBO: xml_vrsync()},
    )
    assert pubs == []
    assert [conta.motivo for conta in resultado.contas] == [
        orq.MOTIVO_INTERNO,
        orq.MOTIVO_INTERNO,
    ]
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO
    assert 'falha interna' in memoria.corpos[1]


@pytest.mark.parametrize(
    ('stdout', 'dados', 'esperado'),
    [
        ('não parece ser um feed VRSync', b'x', orq.MOTIVO_NAO_VRSYNC),
        ('mínimo de segurança: 5', xml_vrsync(1), orq.MOTIVO_MINIMO),
        ('Queda acima de 50%', xml_vrsync(), orq.MOTIVO_QUEDA),
        ('validação falhou (entrada 5 x saída -1', xml_vrsync(), orq.MOTIVO_VALIDACAO),
        ('validação falhou', b'<ListingDataFeed><x>', orq.MOTIVO_XML),
        ('PORTAIS_LISTA_SIGILO está vazio', xml_vrsync(), orq.MOTIVO_LISTA),
        ('algo inesperado', xml_vrsync(), orq.MOTIVO_INTERNO),
    ],
)
def test_classificar_falha(stdout, dados, esperado):
    assert orq.classificar_falha(stdout, '', dados) == esperado
    assert orq.classificar_falha('', stdout, dados) == esperado


def test_xml_bem_formado_aceita_latin1_e_rejeita_lixo():
    assert orq.xml_bem_formado(b'<root>\xff</root>') is True
    assert orq.xml_bem_formado(b'') is False
    assert orq.xml_bem_formado(b'   ') is False
    assert orq.xml_bem_formado(b'<sem-fechar>') is False
    assert orq.xml_bem_formado(xml_vrsync()) is True


def test_redigir_tira_credencial_query_e_texto_vazio():
    bruto = 'fatal: https://x-access-token:TOKEN-FALSO-TESTE@github.com/a/b key=SEGREDO-FALSO'
    limpo = orq.redigir(bruto, ['TOKEN-FALSO-TESTE'])
    assert 'TOKEN-FALSO-TESTE' not in limpo
    assert MARCADOR not in limpo
    assert 'https://[redigido]@github.com/a/b' in limpo
    solto = orq.redigir('veja x-access-token:TOKEN-FALSO-TESTE fim', [])
    assert 'TOKEN-FALSO-TESTE' not in solto
    assert 'x-access-token:[redigido]' in solto
    assert (
        orq.redigir(
            'aaa-segredo-longo e segredo',
            ['segredo', 'aaa-segredo-longo'],
        )
        == '[redigido] e [redigido]'
    )
    assert orq.redigir(None, None) == ''


def test_horario_brasilia_e_o_fuso_de_reserva(monkeypatch):
    utc, brasilia = orq.formatar_instantes(AGORA)
    assert utc == '17/11/2026 23:58'
    assert brasilia == '17/11/2026 20:58'
    utc, brasilia = orq.formatar_instantes(datetime(2026, 11, 18, 1, 30))
    assert utc == '18/11/2026 01:30'
    assert brasilia == '17/11/2026 22:30'

    def quebra(_nome):
        raise orq.ZoneInfoNotFoundError('America/Sao_Paulo')

    monkeypatch.setattr(orq, 'ZoneInfo', quebra)
    _utc, brasilia = orq.formatar_instantes(AGORA)
    assert brasilia == '17/11/2026 20:58'


def test_url_da_execucao_e_segredos_curtos():
    assert orq.url_da_execucao({}) == 'execução local, sem link'
    assert orq.url_da_execucao({'FEED_RUN_URL': '  https://exemplo.invalid/run  '}) == (
        'https://exemplo.invalid/run'
    )
    assert (
        orq.url_da_execucao(
            {
                'GITHUB_SERVER_URL': 'https://github.com/',
                'GITHUB_REPOSITORY': 'guiulobo/feed-olx',
                'GITHUB_RUN_ID': '99',
            }
        )
        == 'https://github.com/guiulobo/feed-olx/actions/runs/99'
    )
    assert (
        orq.url_da_execucao(
            {
                'GITHUB_REPOSITORY': 'guiulobo/feed-olx',
                'GITHUB_RUN_ID': '7',
            }
        )
        == 'https://github.com/guiulobo/feed-olx/actions/runs/7'
    )
    assert orq.segredos_do_ambiente({'VISTA_FEED_URL': 'curta', 'GH_TOKEN': 'token-longo'}) == [
        'token-longo',
    ]
    assert orq.criar_cliente({}) is None
    assert orq.criar_cliente({'GITHUB_TOKEN': 'token-longo'}) is None
    cliente = orq.criar_cliente(
        {
            'GH_TOKEN': 'token-longo',
            'GITHUB_REPOSITORY': 'guiulobo/feed-olx',
        }
    )
    assert isinstance(cliente, orq.ClienteGitHub)


def test_baixar_curl_descarta_stderr_e_nao_chama_sem_url(tmp_path, monkeypatch):
    destino = tmp_path / 'saida.xml'
    with pytest.raises(orq.FalhaDownload):
        orq.baixar_curl('  ', destino, executar=lambda *args, **kwargs: pytest.fail('curl'))

    def falha(cmd, **kwargs):
        raise OSError(cmd[-1])

    with pytest.raises(orq.FalhaDownload) as exc:
        orq.baixar_curl(URL_HUGO, destino, executar=falha)
    assert MARCADOR not in str(exc.value)
    assert exc.value.__cause__ is None

    def http(cmd, **kwargs):
        return Proc(22, '', f'URL {cmd[-1]}')

    with pytest.raises(orq.FalhaDownload) as exc:
        orq.baixar_curl(URL_HUGO, destino, executar=http)
    assert MARCADOR not in str(exc.value)

    def some(cmd, **kwargs):
        assert kwargs['shell'] is False
        return Proc(0, '', URL_HUGO)

    with pytest.raises(orq.FalhaDownload):
        orq.baixar_curl(URL_HUGO, destino, executar=some)

    def ok(cmd, **kwargs):
        Path(cmd[cmd.index('-o') + 1]).write_bytes(b'<ok/>')
        return Proc(0, '', URL_HUGO)

    orq.baixar_curl(URL_HUGO, destino, executar=ok)
    assert destino.read_bytes() == b'<ok/>'

    def padrao(cmd, **kwargs):
        Path(cmd[cmd.index('-o') + 1]).write_bytes(b'<padrao/>')
        return Proc(0, '', '')

    monkeypatch.setattr(subprocess, 'run', padrao)
    outro = tmp_path / 'outro.xml'
    orq.baixar_curl(URL_LOBO, outro)
    assert outro.read_bytes() == b'<padrao/>'


def test_corretor_nao_recebe_segredo_na_linha_de_comando(tmp_path, monkeypatch):
    vistos = {}

    def falso(cmd, **kwargs):
        vistos['cmd'] = cmd
        vistos['env'] = kwargs['env']
        vistos['shell'] = kwargs['shell']
        return Proc(0, 'ok', '')

    monkeypatch.setattr(subprocess, 'run', falso)
    entrada = tmp_path / 'entrada.xml'
    saida = tmp_path / 'saida.xml'
    entrada.write_bytes(b'<x/>')
    env = ambiente()
    orq.corrigir_padrao(entrada, saida, RAIZ / 'destaques_hugo.csv', env)
    comando = ' '.join(vistos['cmd'])
    assert MARCADOR not in comando
    assert CODIGO_SIGILO not in comando
    assert vistos['shell'] is False
    assert vistos['env']['PORTAIS_LISTA_SIGILO'] == env['PORTAIS_LISTA_SIGILO']


def test_publicar_git_so_o_feed_alterado_e_sem_credencial(tmp_path, monkeypatch):
    bare = tmp_path / 'remoto.git'
    repo = tmp_path / 'repo'
    subprocess.run(
        ['git', 'init', '--bare', '-b', 'main', str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    (repo / 'feed-hugo.xml').write_bytes(b'hugo-base')
    (repo / 'feed-lobo.xml').write_bytes(b'lobo-base')
    subprocess.run(['git', 'add', 'feed-hugo.xml', 'feed-lobo.xml'], cwd=repo, check=True)
    subprocess.run(
        [
            'git',
            '-c',
            'user.name=Teste',
            '-c',
            'user.email=teste@example.com',
            'commit',
            '-m',
            'base',
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(['git', 'remote', 'add', 'origin', str(bare)], cwd=repo, check=True)
    subprocess.run(
        ['git', 'push', '-u', 'origin', 'main'],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / 'feed-lobo.xml').write_bytes(b'lobo-novo')
    mensagem = 'Atualiza feeds corrigidos — LOBO (17/11/2026 23:58 UTC)'
    assert orq.publicar_git([repo / 'feed-lobo.xml'], repo, mensagem, []) is True
    copia = tmp_path / 'copia'
    subprocess.run(
        ['git', 'clone', str(bare), str(copia)],
        check=True,
        capture_output=True,
    )
    assert (copia / 'feed-lobo.xml').read_bytes() == b'lobo-novo'
    assert (copia / 'feed-hugo.xml').read_bytes() == b'hugo-base'
    assunto = subprocess.run(
        ['git', 'log', '-1', '--format=%s'],
        cwd=copia,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert assunto == mensagem
    assert orq.publicar_git([repo / 'feed-lobo.xml'], repo, mensagem, []) is False

    with pytest.raises(orq.FalhaPublicacao):
        orq.publicar_git([repo / 'feed_original_hugo.xml'], repo, mensagem, [])

    monkeypatch.setenv('GIT_DIR', '/tmp/nao-usar')

    def executar(args, **kwargs):
        assert 'GIT_DIR' not in kwargs['env']
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert kwargs['shell'] is False
        if args[1] == 'diff':
            return Proc(128, '', 'fatal: https://user:TOKEN-FALSO-TESTE@github.com/a/b')
        if args[1] == 'add':
            return Proc(0, '', '')
        return Proc(1, '', 'fatal: https://x-access-token:TOKEN-FALSO-TESTE@github.com/a/b')

    with pytest.raises(orq.FalhaPublicacao) as exc:
        orq.publicar_git(
            [repo / 'feed-hugo.xml'],
            repo,
            mensagem,
            ['TOKEN-FALSO-TESTE'],
            executar=executar,
        )
    assert 'TOKEN-FALSO-TESTE' not in str(exc.value)
    assert '[redigido]' in str(exc.value)

    def vazio(args, **kwargs):
        if args[1] == 'diff':
            return Proc(3, '', '   ')
        return Proc(0, '', '')

    with pytest.raises(orq.FalhaPublicacao) as exc:
        orq.publicar_git([repo / 'feed-hugo.xml'], repo, mensagem, [], executar=vazio)
    assert 'não foi possível conferir' in str(exc.value)

    def add_vazio(args, **kwargs):
        return Proc(1, '', '   ')

    with pytest.raises(orq.FalhaPublicacao) as exc:
        orq.publicar_git([repo / 'feed-lobo.xml'], repo, mensagem, [], executar=add_vazio)
    assert str(exc.value) == 'falha ao publicar'


def test_publicacao_sem_mudanca_e_falha_nao_vazam(tmp_path):
    dados = xml_vrsync()

    def parado(*_args):
        return False

    resultado, _memoria, pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        publicar=parado,
    )
    assert resultado.codigo == 0
    assert pubs[0]['arquivos'] == ['feed-hugo.xml', 'feed-lobo.xml']
    assert 'Nada a publicar.' in '\n'.join(resultado.logs)

    def quebra(*_args):
        raise orq.FalhaPublicacao('detalhe seguro')

    resultado, _memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        publicar=quebra,
    )
    assert resultado.codigo == 1
    assert any('detalhe seguro' in linha for linha in resultado.logs)
    assert resultado.acao_issue == 'nenhuma'

    def estoura(*_args):
        raise RuntimeError(URL_HUGO)

    resultado, memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        publicar=estoura,
    )
    assert resultado.codigo == 1
    assert any(linha == 'Não foi possível publicar os feeds.' for linha in resultado.logs)
    assert memoria.criadas == []


def test_falha_da_api_de_issues_nao_leva_o_marcador(tmp_path):
    dados = xml_vrsync()

    class Cliente:
        def buscar_aberta(self):
            raise orq.FalhaAviso(f'falhou {URL_HUGO}')

    resultado, _memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        cliente=Cliente(),
        checar=False,
    )
    assert resultado.codigo == 1
    assert resultado.acao_issue == 'erro'
    bruto = '\n'.join(resultado.logs)
    assert MARCADOR not in bruto
    assert '[redigido]' in bruto

    class Generico:
        def buscar_aberta(self):
            raise RuntimeError(URL_LOBO)

    resultado, _memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: b'', URL_LOBO: xml_vrsync()},
        cliente=Generico(),
        checar=False,
    )
    assert resultado.codigo == 1
    assert MARCADOR not in '\n'.join(resultado.logs)
    assert any(linha == 'Não foi possível abrir ou atualizar a issue.' for linha in resultado.logs)


def test_sem_token_sucesso_passa_e_falha_continua_vermelha(tmp_path):
    dados = xml_vrsync()
    resultado, memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: dados, URL_LOBO: dados},
        cliente=None,
    )
    assert resultado.codigo == 0
    assert resultado.acao_issue == 'nenhuma'
    assert memoria is None

    resultado, memoria, _pubs = rodar(
        tmp_path,
        {URL_HUGO: b'', URL_LOBO: dados},
        cliente=None,
    )
    assert resultado.codigo == 1
    assert resultado.acao_issue == 'sem-cliente'
    assert any('Sem token' in linha for linha in resultado.logs)
    assert (tmp_path / 'feed-hugo.xml').read_bytes() == ANTERIOR_HUGO


def test_padroes_da_rodada_nao_acessam_a_rede(tmp_path):
    memoria = MemoriaIssues()
    resultado = orq.executar_rodada(
        tmp_path,
        None,
        cliente=memoria,
        publicar=lambda *args: False,
        environ=ambiente(),
        run_url=RUN_URL,
    )
    assert resultado.codigo == 1
    assert [conta.motivo for conta in resultado.contas] == [
        orq.MOTIVO_URL,
        orq.MOTIVO_URL,
    ]
    assert MARCADOR not in juntar(resultado, memoria)
    assert CODIGO_SIGILO not in juntar(resultado, memoria)
    assert 'Brasília' in memoria.corpos[1]


def test_execucao_direta_mostra_a_ajuda(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['orquestra_feed.py', '--help'])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(RAIZ / 'orquestra_feed.py'), run_name='__main__')
    assert exc.value.code == 0


def test_main_ajuda_argumento_e_falha(tmp_path, monkeypatch, capsys):
    assert orq.main(['--help']) == 0
    assert orq.main(['-h']) == 0
    assert orq.main(['--conta', 'HUGO']) == 2
    monkeypatch.setattr(sys, 'argv', ['orquestra_feed.py', '--help'])
    assert orq.main() == 0
    assert MARCADOR not in capsys.readouterr().out

    def baixar(url, destino):
        destino.write_bytes(b'' if url == URL_HUGO else xml_vrsync())

    env = ambiente()
    env['GITHUB_SERVER_URL'] = 'https://github.com'
    env['GITHUB_REPOSITORY'] = 'guiulobo/feed-olx'
    env['GITHUB_RUN_ID'] = '99'
    memoria = MemoriaIssues()
    codigo = orq.main(
        [],
        diretorio=tmp_path,
        environ=env,
        baixar=baixar,
        cliente=memoria,
        publicar=lambda *args: True,
        agora=AGORA,
    )
    assert codigo == 1
    assert 'actions/runs/99' in memoria.corpos[1]
    assert MARCADOR not in memoria.corpos[1]
    assert not (tmp_path / 'feed-hugo.xml').exists()
    assert b'ListingDataFeed' in (tmp_path / 'feed-lobo.xml').read_bytes()


def test_main_sucesso_sem_cliente_explicito(tmp_path):
    env = ambiente()

    def baixar(url, destino):
        destino.write_bytes(xml_vrsync())

    codigo = orq.main(
        [],
        diretorio=tmp_path,
        environ=env,
        baixar=baixar,
        publicar=lambda *args: True,
        agora=AGORA,
        run_url=RUN_URL,
    )
    assert codigo == 0
    assert b'ListingDataFeed' in (tmp_path / 'feed-hugo.xml').read_bytes()


def test_main_usa_o_baixador_padrao(tmp_path, monkeypatch):
    def falso(url, destino, executar=None):
        destino.write_bytes(b'')

    monkeypatch.setattr(orq, 'baixar_curl', falso)
    monkeypatch.setattr(orq, 'publicar_git', lambda *args, **kwargs: False)
    memoria = MemoriaIssues()
    codigo = orq.main(
        [],
        diretorio=tmp_path,
        environ=ambiente(),
        cliente=memoria,
        agora=AGORA,
        run_url=RUN_URL,
    )
    assert codigo == 1
    assert memoria.criadas == [1]
    assert 'vazio' in memoria.corpos[1]


class _Resp:
    def __init__(self, status, corpo):
        self.status = status
        self._corpo = corpo

    def read(self):
        if isinstance(self._corpo, Exception):
            raise self._corpo
        return self._corpo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_transportar_nao_leva_token_nem_corpo(monkeypatch):
    def ok(pedido, timeout=60):
        assert pedido.get_header('Authorization') == 'Bearer token-de-teste'
        assert 'token-de-teste' not in pedido.full_url
        return _Resp(201, b'{"number": 7}')

    status, dados = orq.transportar(
        'POST',
        'https://api.github.com/repos/a/b/issues',
        'token-de-teste',
        {'title': 'x'},
        abridor=ok,
    )
    assert status == 201
    assert dados['number'] == 7

    def sem_status(pedido, timeout=60):
        class Resp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Resp()

    status, dados = orq.transportar(
        'GET',
        'https://api.github.com/x',
        'token-de-teste',
        abridor=sem_status,
    )
    assert status == 200
    assert dados == {'ok': True}

    def vazio(pedido, timeout=60):
        return _Resp(204, b'')

    status, dados = orq.transportar(
        'GET',
        'https://api.github.com/x',
        'token-de-teste',
        abridor=vazio,
    )
    assert status == 204
    assert dados is None

    def ruim(pedido, timeout=60):
        return _Resp(200, b'nao-json')

    status, dados = orq.transportar(
        'GET',
        'https://api.github.com/x',
        'token-de-teste',
        abridor=ruim,
    )
    assert dados is None

    def http(pedido, timeout=60):
        raise urllib.error.HTTPError(
            pedido.full_url,
            500,
            'erro',
            None,
            io.BytesIO(b'{"message":"token-de-teste"}'),
        )

    status, dados = orq.transportar(
        'GET',
        'https://api.github.com/x',
        'token-de-teste',
        abridor=http,
    )
    assert status == 500
    assert dados['message'] == 'token-de-teste'

    def leitura(pedido, timeout=60):
        class Vazio(io.BytesIO):
            def read(self, *args, **kwargs):
                raise OSError('disco')

        raise urllib.error.HTTPError(pedido.full_url, 404, 'nao', None, Vazio())

    status, dados = orq.transportar(
        'GET',
        'https://api.github.com/x',
        'token-de-teste',
        abridor=leitura,
    )
    assert status == 404
    assert dados is None

    def rede(pedido, timeout=60):
        raise urllib.error.URLError('timed out token-de-teste')

    with pytest.raises(orq.FalhaAviso) as exc:
        orq.transportar('GET', 'https://api.github.com/x', 'token-de-teste', abridor=rede)
    assert 'token-de-teste' not in str(exc.value)
    assert exc.value.__cause__ is None

    def generico(pedido, timeout=60):
        raise RuntimeError('token-de-teste')

    with pytest.raises(orq.FalhaAviso) as exc:
        orq.transportar('GET', 'https://api.github.com/x', 'token-de-teste', abridor=generico)
    assert 'token-de-teste' not in str(exc.value)
    assert exc.value.__cause__ is None

    def ja_aviso(pedido, timeout=60):
        raise orq.FalhaAviso('falha ao falar com a API de issues')

    with pytest.raises(orq.FalhaAviso):
        orq.transportar('GET', 'https://api.github.com/x', 'token-de-teste', abridor=ja_aviso)

    chamado = {'foi': False}

    def nao_chama(*args, **kwargs):
        chamado['foi'] = True

    with pytest.raises(orq.FalhaAviso):
        orq.transportar(
            'GET',
            'https://api.github.com/token-de-teste',
            'token-de-teste',
            abridor=nao_chama,
        )
    with pytest.raises(orq.FalhaAviso):
        orq.transportar('GET', 'https://api.github.com/x', '', abridor=nao_chama)
    assert chamado['foi'] is False

    def abridor(pedido, timeout=60):
        return _Resp(200, b'{"ok": true}')

    monkeypatch.setattr(urllib.request, 'urlopen', abridor)
    status, dados = orq.transportar('GET', 'https://api.github.com/x', 'token-de-teste')
    assert status == 200
    assert dados == {'ok': True}


def test_cliente_github_abre_atualiza_e_fecha():
    estado = {'rotulo': False, 'issues': []}
    chamadas = []

    def transportar(method, url, token, payload=None):
        assert 'token-de-teste' not in url
        chamadas.append((method, url, payload))
        if url.endswith('/labels/alerta-feed') and method == 'GET':
            if not estado['rotulo']:
                return 404, {'message': 'Not Found'}
            return 200, {'name': orq.ROTULO}
        if url.endswith('/labels') and method == 'POST':
            estado['rotulo'] = True
            assert payload['name'] == orq.ROTULO
            assert payload['color'] == orq.COR_ROTULO
            return 201, payload
        if method == 'GET' and '/issues?' in url:
            return 200, list(estado['issues'])
        if method == 'POST' and url.endswith('/issues'):
            assert payload['title'] == orq.TITULO_ISSUE
            assert payload['labels'] == [orq.ROTULO]
            estado['issues'].append(
                {
                    'number': 8,
                    'title': payload['title'],
                    'body': payload['body'],
                }
            )
            estado['issues'].extend(
                [
                    'lixo',
                    {'title': 'outra', 'number': 1},
                    {'title': orq.TITULO_ISSUE, 'number': 9, 'pull_request': {}},
                    {'title': orq.TITULO_ISSUE, 'number': '4'},
                    {'title': orq.TITULO_ISSUE, 'number': 7},
                ]
            )
            return 201, {'number': 8}
        if method == 'POST' and url.endswith('/comments'):
            return 201, {'id': 1}
        if method == 'PATCH':
            return 200, payload or {}
        return 500, None

    cliente = orq.ClienteGitHub('token-de-teste', 'guiulobo/feed-olx', transportar)
    assert cliente._url('issues').endswith('/guiulobo/feed-olx/issues')
    assert cliente._url('/issues').endswith('/guiulobo/feed-olx/issues')
    assert cliente.buscar_aberta() is None
    assert cliente.criar('corpo') == 8
    assert cliente.buscar_aberta() == 7
    cliente.comentar(7, 'de novo')
    cliente.atualizar_corpo(7, 'de novo')
    cliente.fechar(7)
    posts_rotulo = [item for item in chamadas if item[0] == 'POST' and item[1].endswith('/labels')]
    assert len(posts_rotulo) == 1
    fechou = [item for item in chamadas if item[0] == 'PATCH' and item[1].endswith('/issues/7')]
    assert fechou[-1][2]['state'] == 'closed'

    def lista_404(method, url, token, payload=None):
        if '/issues?' in url:
            return 404, None
        if url.endswith('/labels/alerta-feed'):
            return 200, {'name': orq.ROTULO}
        return 200, {}

    outro = orq.ClienteGitHub('token-de-teste', 'guiulobo/feed-olx', lista_404)
    assert outro.buscar_aberta() is None

    def lista_dict(method, url, token, payload=None):
        return 200, {'message': 'ok'}

    with pytest.raises(orq.FalhaAviso):
        orq.ClienteGitHub('token-de-teste', 'a/b', lista_dict).buscar_aberta()

    def sem_numero(method, url, token, payload=None):
        if url.endswith('/labels/alerta-feed'):
            return 200, {'name': orq.ROTULO}
        return 201, {}

    with pytest.raises(orq.FalhaAviso) as exc:
        orq.ClienteGitHub('token-de-teste', 'a/b', sem_numero).criar('x')
    assert 'token-de-teste' not in str(exc.value)

    def erro(method, url, token, payload=None):
        return 422, {'message': 'token-de-teste'}

    with pytest.raises(orq.FalhaAviso) as exc:
        orq.ClienteGitHub('token-de-teste', 'a/b', erro).comentar(1, 'oi')
    assert 'HTTP 422' in str(exc.value)
    assert 'token-de-teste' not in str(exc.value)

    def so_lixo(method, url, token, payload=None):
        return 200, [
            {'title': 'outra', 'number': 1},
            {'title': orq.TITULO_ISSUE, 'number': 3, 'pull_request': {}},
        ]

    assert orq.ClienteGitHub('token-de-teste', 'a/b', so_lixo).buscar_aberta() is None


def test_workflow_e_o_ci():
    workflow = (RAIZ / '.github' / 'workflows' / 'atualiza-feed.yml').read_text(encoding='utf-8')
    assert "cron: '0 8 * * *'" in workflow
    assert "cron: '0 20 * * *'" in workflow
    assert 'workflow_dispatch' in workflow
    assert 'group: atualiza-feed' in workflow
    assert 'cancel-in-progress: false' in workflow
    assert 'contents: write' in workflow
    assert 'issues: write' in workflow
    assert 'python3 orquestra_feed.py' in workflow
    assert workflow.count('PORTAIS_LISTA_SIGILO: ${{ secrets.PORTAIS_LISTA_SIGILO }}') == 1
    assert 'curl ' not in workflow
    assert MARCADOR not in workflow
    ci = (RAIZ / '.github' / 'workflows' / 'testes.yml').read_text(encoding='utf-8')
    assert 'pull_request' in ci
    assert 'pytest' in ci
    assert 'ruff check' in ci
    assert 'secrets.' not in ci
    fonte = (RAIZ / 'orquestra_feed.py').read_text(encoding='utf-8')
    assert MARCADOR not in fonte
    assert 'shell=True' not in fonte
