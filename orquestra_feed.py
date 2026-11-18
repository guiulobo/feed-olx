#!/usr/bin/env python3
"""Orquestra as contas HUGO e LOBO do feed OLX.

Cada conta baixa, valida e corrige sozinha. A que falha não impede a outra,
não apaga o feed já publicado e não entra no commit. Se alguma falhar, a
rodada publica quem passou, reaproveita uma única issue e termina com erro.

O download, a correção, a publicação e o cliente de issues são injetáveis,
para a rodada inteira ser testada sem rede e sem segredo real.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RAIZ = Path(__file__).resolve().parent

TITULO_ISSUE = 'Robô feed-olx: falha no download/validação'
ROTULO = 'alerta-feed'
COR_ROTULO = 'B60205'
AGENTE = 'Mozilla/5.0 (compatible; LoboFeedBot/1.0)'
ARQUIVOS_PUBLICAVEIS = ('feed-hugo.xml', 'feed-lobo.xml')
NOME_ROBO = 'robo-feed'
EMAIL_ROBO = 'actions@users.noreply.github.com'

MOTIVO_VAZIO = 'vazio'
MOTIVO_NAO_VRSYNC = 'não é VRSync'
MOTIVO_XML = 'XML inválido'
MOTIVO_MINIMO = 'mínimo'
MOTIVO_QUEDA = 'queda'
MOTIVO_VALIDACAO = 'validação'
MOTIVO_DOWNLOAD = 'falha no download'
MOTIVO_URL = 'URL não configurada'
MOTIVO_LISTA = 'lista de sigilo'
MOTIVO_INTERNO = 'falha interna'

_NOMES_SECRETOS = (
    'VISTA_FEED_URL',
    'VISTA_FEED_URL_LOBO',
    'PORTAIS_LISTA_SIGILO',
    'GITHUB_TOKEN',
    'GH_TOKEN',
)
_RE_USERINFO = re.compile(r'(https?://)[^/\s@]+@')
_RE_QUERY_KEY = re.compile(r'(key=)[^&\s]+')
_RE_X_ACCESS = re.compile(r'x-access-token:[^\s@]+')
_GIT_ENV_IGNORAR = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_PREFIX')

USO = (
    'Uso: python3 orquestra_feed.py\n'
    'Baixa as contas HUGO e LOBO, publica as que passarem e,\n'
    'se alguma falhar, atualiza uma única issue e sai com erro.\n'
)


class FalhaDownload(Exception):
    """Falha de download. A mensagem é fixa e não leva o endereço."""

    def __init__(self, *args, **kwargs):
        super().__init__(MOTIVO_DOWNLOAD)


class FalhaPublicacao(Exception):
    """Falha ao gravar o commit. O texto já chega sem credencial."""


class FalhaAviso(Exception):
    """Falha na API de issues. O texto não leva token nem corpo da resposta."""


@dataclass(frozen=True)
class Conta:
    nome: str
    arquivo: str
    destaques: str
    env_url: str


CONTAS = (
    Conta('HUGO', 'feed-hugo.xml', 'destaques_hugo.csv', 'VISTA_FEED_URL'),
    Conta('LOBO', 'feed-lobo.xml', 'destaques_lobo.csv', 'VISTA_FEED_URL_LOBO'),
)


@dataclass
class ResultadoConta:
    nome: str
    arquivo: str
    ok: bool
    motivo: str | None = None


@dataclass
class ResultadoRodada:
    contas: list[ResultadoConta]
    codigo: int
    acao_issue: str
    logs: list[str]


def log_padrao(mensagem: str) -> None:
    print(mensagem, flush=True)


def redigir(texto: str | None, proibidos: list[str] | None) -> str:
    """Tira URL, token e o valor dos secrets de um texto que vai para fora."""
    saida = '' if texto is None else str(texto)
    for segredo in sorted((p for p in (proibidos or []) if p), key=len, reverse=True):
        saida = saida.replace(segredo, '[redigido]')
    saida = _RE_USERINFO.sub(r'\1[redigido]@', saida)
    saida = _RE_X_ACCESS.sub('x-access-token:[redigido]', saida)
    saida = _RE_QUERY_KEY.sub(r'\1[redigido]', saida)
    return saida


def segredos_do_ambiente(environ: dict) -> list[str]:
    achados = []
    for nome in _NOMES_SECRETOS:
        valor = environ.get(nome) or ''
        if len(valor) >= 8:
            achados.append(valor)
    return achados


def formatar_instantes(agora: datetime) -> tuple[str, str]:
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=UTC)
    try:
        zona = ZoneInfo('America/Sao_Paulo')
    except ZoneInfoNotFoundError:
        zona = timezone(timedelta(hours=-3))
    utc = agora.astimezone(UTC)
    brasilia = agora.astimezone(zona)
    return utc.strftime('%d/%m/%Y %H:%M'), brasilia.strftime('%d/%m/%Y %H:%M')


def url_da_execucao(environ: dict) -> str:
    explicita = (environ.get('FEED_RUN_URL') or '').strip()
    if explicita:
        return explicita
    servidor = (environ.get('GITHUB_SERVER_URL') or 'https://github.com').rstrip('/')
    repositorio = (environ.get('GITHUB_REPOSITORY') or '').strip()
    run_id = (environ.get('GITHUB_RUN_ID') or '').strip()
    if repositorio and run_id:
        return f'{servidor}/{repositorio}/actions/runs/{run_id}'
    return 'execução local, sem link'


def xml_bem_formado(dados: bytes) -> bool:
    if not dados or not dados.strip():
        return False
    try:
        texto = dados.decode('utf-8')
    except UnicodeDecodeError:
        texto = dados.decode('latin-1')
    try:
        ET.fromstring(texto.lstrip('\ufeff \t\r\n'))
    except ET.ParseError:
        return False
    return True


def classificar_falha(stdout: str, stderr: str, dados: bytes) -> str:
    """Traduz a saída do corretor para um motivo curto, sem colar o erro bruto."""
    texto = f'{stdout or ""}\n{stderr or ""}'
    if 'não parece ser um feed VRSync' in texto:
        return MOTIVO_NAO_VRSYNC
    if 'mínimo de segurança' in texto:
        return MOTIVO_MINIMO
    if 'Queda acima' in texto:
        return MOTIVO_QUEDA
    if 'validação falhou' in texto:
        if xml_bem_formado(dados):
            return MOTIVO_VALIDACAO
        return MOTIVO_XML
    if 'PORTAIS_LISTA_SIGILO' in texto:
        return MOTIVO_LISTA
    return MOTIVO_INTERNO


def restaurar(caminho: Path, antes: bytes | None) -> None:
    """Devolve o feed publicado ao byte anterior se a conta não passou."""
    if antes is None:
        if caminho.is_file():
            caminho.unlink()
        return
    if not caminho.is_file() or caminho.read_bytes() != antes:
        caminho.write_bytes(antes)


def _mensagem_falha(nome: str, motivo: str) -> str:
    return f'{nome}: falhou ({motivo}). O arquivo publicado continua sendo o último bom.'


def baixar_curl(url: str, destino: Path, executar=None) -> None:
    """Baixa com curl. O erro do curl pode ecoar a URL: ele não sai daqui."""
    if not str(url or '').strip():
        raise FalhaDownload()
    executar = executar or subprocess.run
    comando = [
        'curl',
        '-sS',
        '-f',
        '-L',
        '--retry',
        '3',
        '--retry-delay',
        '30',
        '--max-time',
        '1200',
        '-A',
        AGENTE,
        '-o',
        str(destino),
        '--',
        str(url),
    ]
    try:
        proc = executar(
            comando,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            errors='replace',
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - o erro do curl pode trazer a URL
        raise FalhaDownload() from None
    if getattr(proc, 'returncode', 1) != 0 or not Path(destino).is_file():
        raise FalhaDownload()


def corrigir_padrao(entrada: Path, saida: Path, destaques: Path, environ: dict):
    """Roda o corretor num processo novo. A saída bruta volta só para classificar."""
    comando = [
        sys.executable,
        str(RAIZ / 'corrige_feed.py'),
        str(entrada),
        str(saida),
        str(destaques),
    ]
    proc = subprocess.run(
        comando,
        cwd=str(RAIZ),
        env=dict(environ),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        errors='replace',
    )
    return proc.returncode, proc.stdout or '', proc.stderr or ''


def _limpar_temporarios(destino: Path | None, publicado: Path) -> None:
    if destino is not None:
        destino.unlink(missing_ok=True)
    Path(str(publicado) + '.tmp').unlink(missing_ok=True)


def processar_conta(conta, diretorio, url, baixar, corrigir, emitir, environ):
    publicado = diretorio / conta.arquivo
    antes = publicado.read_bytes() if publicado.is_file() else None
    destino = None

    def falha(motivo: str) -> ResultadoConta:
        restaurar(publicado, antes)
        emitir(_mensagem_falha(conta.nome, motivo))
        return ResultadoConta(conta.nome, conta.arquivo, False, motivo)

    try:
        if not str(url or '').strip():
            return falha(MOTIVO_URL)
        emitir(f'{conta.nome}: baixando.')
        descritor, nome_tmp = tempfile.mkstemp(prefix='feed-original-', suffix='.xml')
        destino = Path(nome_tmp)
        os.close(descritor)
        try:
            baixar(url, destino)
        except Exception:  # noqa: BLE001 - a exceção pode trazer a URL
            return falha(MOTIVO_DOWNLOAD)
        dados = destino.read_bytes() if destino.is_file() else b''
        if not dados.strip():
            return falha(MOTIVO_VAZIO)
        emitir(f'{conta.nome}: corrigindo.')
        try:
            codigo, saida, erro = corrigir(
                destino,
                publicado,
                RAIZ / conta.destaques,
                environ,
            )
        except Exception:  # noqa: BLE001 - não repassar a saída do corretor
            return falha(MOTIVO_INTERNO)
        if codigo == 0:
            emitir(f'{conta.nome}: validada e pronta para publicação.')
            return ResultadoConta(conta.nome, conta.arquivo, True, None)
        return falha(classificar_falha(saida, erro, dados))
    except Exception:  # noqa: BLE001 - a conta segue isolada mesmo com erro inesperado
        return falha(MOTIVO_INTERNO)
    finally:
        _limpar_temporarios(destino, publicado)


def mensagem_commit(sucessos: list[ResultadoConta], agora: datetime) -> str:
    quando_utc, _quando_brasilia = formatar_instantes(agora)
    nomes = ', '.join(item.nome for item in sucessos)
    return f'Atualiza feeds corrigidos — {nomes} ({quando_utc} UTC)'


def _ambiente_git() -> dict:
    ambiente = os.environ.copy()
    for chave in _GIT_ENV_IGNORAR:
        ambiente.pop(chave, None)
    ambiente['GIT_TERMINAL_PROMPT'] = '0'
    return ambiente


def _git(args: list[str], diretorio: Path, proibidos: list[str], executar):
    proc = executar(
        args,
        cwd=str(diretorio),
        env=_ambiente_git(),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        errors='replace',
    )
    if proc.returncode != 0:
        detalhe = redigir(((proc.stderr or '') + '\n' + (proc.stdout or '')).strip(), proibidos)
        raise FalhaPublicacao(detalhe or 'falha ao publicar')
    return proc


def publicar_git(arquivos, diretorio: Path, mensagem: str, proibidos: list[str], executar=None):
    """Faz commit só dos feeds permitidos. O push não devolve a URL do remoto."""
    executar = executar or subprocess.run
    nomes = []
    for arquivo in arquivos:
        nome = Path(arquivo).name
        if nome not in ARQUIVOS_PUBLICAVEIS:
            raise FalhaPublicacao('arquivo de publicação não permitido')
        nomes.append(nome)
    _git(['git', 'add', '--', *nomes], diretorio, proibidos, executar)
    diff = executar(
        ['git', 'diff', '--cached', '--quiet', '--', *nomes],
        cwd=str(diretorio),
        env=_ambiente_git(),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        errors='replace',
    )
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        detalhe = redigir(((diff.stderr or '') + '\n' + (diff.stdout or '')).strip(), proibidos)
        raise FalhaPublicacao(detalhe or 'não foi possível conferir as mudanças')
    _git(['git', 'config', 'user.name', NOME_ROBO], diretorio, proibidos, executar)
    _git(['git', 'config', 'user.email', EMAIL_ROBO], diretorio, proibidos, executar)
    _git(['git', 'commit', '-m', mensagem], diretorio, proibidos, executar)
    _git(['git', 'push'], diretorio, proibidos, executar)
    return True


def montar_texto_falha(resultados, quando_utc: str, quando_brasilia: str, run_url: str) -> str:
    linhas = [
        'Pelo menos uma conta do feed OLX falhou no download ou na validação.',
        '',
        'Contas:',
    ]
    for resultado in resultados:
        if resultado.ok:
            linhas.append(f'- {resultado.nome}: ok. O feed foi validado e segue para publicação.')
        else:
            linhas.append(
                f'- {resultado.nome}: falhou ({resultado.motivo}). '
                'O arquivo publicado continua sendo o último bom.'
            )
    linhas.extend(
        [
            '',
            f'Data e hora: {quando_utc} UTC ({quando_brasilia} Brasília).',
            f'Execução: {run_url}',
        ]
    )
    return '\n'.join(linhas) + '\n'


def montar_texto_sucesso(quando_utc: str, quando_brasilia: str, run_url: str) -> str:
    return (
        'As contas HUGO e LOBO passaram no download e na validação. '
        'Esta issue está sendo fechada.\n'
        '\n'
        f'Data e hora: {quando_utc} UTC ({quando_brasilia} Brasília).\n'
        f'Execução: {run_url}\n'
    )


def transportar(method: str, url: str, token: str, payload=None, abridor=None):
    """Chama a API. Token só no cabeçalho. Erro de rede vira mensagem fixa."""
    if not token or token in url:
        raise FalhaAviso('falha ao falar com a API de issues')
    abridor = abridor or urllib.request.urlopen
    dados = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    pedido = urllib.request.Request(url, data=dados, method=method)
    pedido.add_header('Authorization', f'Bearer {token}')
    pedido.add_header('Accept', 'application/vnd.github+json')
    pedido.add_header('X-GitHub-Api-Version', '2022-11-28')
    pedido.add_header('User-Agent', 'robo-feed-olx')
    if payload is not None:
        pedido.add_header('Content-Type', 'application/json; charset=utf-8')
    try:
        with abridor(pedido, timeout=60) as resp:
            status = getattr(resp, 'status', 200)
            bruto = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            bruto = exc.read()
        except OSError:
            bruto = b''
    except urllib.error.URLError:
        raise FalhaAviso('falha ao falar com a API de issues') from None
    except FalhaAviso:
        raise
    except Exception:  # noqa: BLE001 - a resposta pode ecoar o pedido
        raise FalhaAviso('falha ao falar com a API de issues') from None
    if not bruto:
        return status, None
    try:
        return status, json.loads(bruto.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


class ClienteGitHub:
    """Abre, comenta e fecha a issue de alerta com o GITHUB_TOKEN do workflow."""

    def __init__(self, token: str, repositorio: str, transportar=transportar):
        self.token = token
        self.repositorio = repositorio
        self.transportar = transportar

    def _url(self, caminho: str) -> str:
        repo = urllib.parse.quote(self.repositorio, safe='/')
        if not caminho.startswith('/'):
            caminho = '/' + caminho
        return f'https://api.github.com/repos/{repo}{caminho}'

    def _chamar(self, method: str, caminho: str, payload=None, aceita_404: bool = False):
        status, dados = self.transportar(method, self._url(caminho), self.token, payload)
        if status == 404 and aceita_404:
            return None
        if status < 200 or status >= 300:
            raise FalhaAviso(f'falha ao falar com a API de issues (HTTP {status})')
        return dados

    def garantir_rotulo(self) -> None:
        achado = self._chamar(
            'GET',
            f'/labels/{urllib.parse.quote(ROTULO)}',
            aceita_404=True,
        )
        if achado is not None:
            return
        self._chamar(
            'POST',
            '/labels',
            {
                'name': ROTULO,
                'color': COR_ROTULO,
                'description': 'Falha no download ou na validação do feed OLX',
            },
        )

    def buscar_aberta(self):
        dados = self._chamar(
            'GET',
            f'/issues?state=open&labels={urllib.parse.quote(ROTULO)}&per_page=100',
            aceita_404=True,
        )
        if not dados:
            return None
        if not isinstance(dados, list):
            raise FalhaAviso('falha ao falar com a API de issues')
        candidatas = []
        for item in dados:
            if not isinstance(item, dict):
                continue
            if item.get('title') != TITULO_ISSUE or 'pull_request' in item:
                continue
            numero = item.get('number')
            if isinstance(numero, int):
                candidatas.append(numero)
        if not candidatas:
            return None
        return min(candidatas)

    def criar(self, corpo: str) -> int:
        self.garantir_rotulo()
        dados = self._chamar(
            'POST',
            '/issues',
            {
                'title': TITULO_ISSUE,
                'body': corpo,
                'labels': [ROTULO],
            },
        )
        numero = dados.get('number') if isinstance(dados, dict) else None
        if not isinstance(numero, int):
            raise FalhaAviso('falha ao falar com a API de issues')
        return numero

    def comentar(self, numero: int, corpo: str) -> None:
        self._chamar('POST', f'/issues/{numero}/comments', {'body': corpo})

    def atualizar_corpo(self, numero: int, corpo: str) -> None:
        self._chamar('PATCH', f'/issues/{numero}', {'body': corpo})

    def fechar(self, numero: int) -> None:
        self._chamar(
            'PATCH',
            f'/issues/{numero}',
            {
                'state': 'closed',
                'state_reason': 'completed',
            },
        )


def criar_cliente(environ: dict):
    token = environ.get('GITHUB_TOKEN') or environ.get('GH_TOKEN') or ''
    repositorio = (environ.get('GITHUB_REPOSITORY') or '').strip()
    if not token or not repositorio:
        return None
    return ClienteGitHub(token=token, repositorio=repositorio)


def avisar(cliente, texto_falha: str, texto_ok: str, houve_falha: bool, emitir) -> str:
    if cliente is None:
        if houve_falha:
            emitir('Sem token para abrir a issue de alerta.')
            return 'sem-cliente'
        return 'nenhuma'
    numero = cliente.buscar_aberta()
    if houve_falha:
        if numero is None:
            novo = cliente.criar(texto_falha)
            emitir(f'Issue #{novo} aberta.')
            return 'aberta'
        cliente.comentar(numero, texto_falha)
        cliente.atualizar_corpo(numero, texto_falha)
        emitir(f'Issue #{numero} atualizada.')
        return 'comentada'
    if numero is None:
        return 'nenhuma'
    cliente.comentar(numero, texto_ok)
    cliente.fechar(numero)
    emitir(f'Issue #{numero} fechada.')
    return 'fechada'


def executar_rodada(
    diretorio: Path,
    urls: dict | None = None,
    *,
    baixar=None,
    corrigir=None,
    cliente=None,
    publicar=None,
    agora: datetime | None = None,
    run_url: str = 'execução local, sem link',
    proibidos: list[str] | None = None,
    log=None,
    environ: dict | None = None,
) -> ResultadoRodada:
    """Roda as duas contas, publica quem passou e avisa. Devolve o código final."""
    baixar = baixar_curl if baixar is None else baixar
    corrigir = corrigir_padrao if corrigir is None else corrigir
    publicar = publicar_git if publicar is None else publicar
    log = log_padrao if log is None else log
    environ = os.environ if environ is None else environ
    proibidos = [] if proibidos is None else list(proibidos)
    urls = {} if urls is None else urls
    agora = datetime.now(UTC) if agora is None else agora
    logs: list[str] = []

    def emitir(mensagem: str) -> None:
        limpo = redigir(mensagem, proibidos)
        logs.append(limpo)
        log(limpo)

    emitir('Iniciando a rodada das contas HUGO e LOBO.')
    resultados: list[ResultadoConta] = []
    for conta in CONTAS:
        try:
            resultados.append(
                processar_conta(
                    conta,
                    diretorio,
                    urls.get(conta.env_url, ''),
                    baixar,
                    corrigir,
                    emitir,
                    environ,
                )
            )
        except Exception:  # noqa: BLE001 - uma conta quebrada não pode travar a outra
            emitir(_mensagem_falha(conta.nome, MOTIVO_INTERNO))
            resultados.append(ResultadoConta(conta.nome, conta.arquivo, False, MOTIVO_INTERNO))

    sucessos = [item for item in resultados if item.ok]
    houve_falha = any(not item.ok for item in resultados)
    erro_publicacao = False
    if sucessos:
        mensagem = mensagem_commit(sucessos, agora)
        emitir(f'Publicando: {mensagem}')
        try:
            mudou = publicar(
                [diretorio / item.arquivo for item in sucessos],
                diretorio,
                mensagem,
                proibidos,
            )
        except FalhaPublicacao as exc:
            erro_publicacao = True
            emitir(f'Não foi possível publicar: {exc}')
        except Exception:  # noqa: BLE001 - o erro do git pode trazer credencial
            erro_publicacao = True
            emitir('Não foi possível publicar os feeds.')
        else:
            if mudou:
                nomes = ', '.join(item.nome for item in sucessos)
                emitir(f'Feeds publicados ({nomes}). GitHub Pages atualiza em 1-2 min.')
            else:
                emitir('Feeds sem mudanças. Nada a publicar.')

    quando_utc, quando_brasilia = formatar_instantes(agora)
    texto_falha = redigir(
        montar_texto_falha(resultados, quando_utc, quando_brasilia, run_url),
        proibidos,
    )
    texto_ok = redigir(
        montar_texto_sucesso(quando_utc, quando_brasilia, run_url),
        proibidos,
    )
    resumo = ', '.join(f'{item.nome}={"ok" if item.ok else item.motivo}' for item in resultados)
    emitir(f'Resultado: {resumo}.')
    try:
        acao = avisar(cliente, texto_falha, texto_ok, houve_falha, emitir)
    except FalhaAviso as exc:
        emitir(str(exc))
        acao = 'erro'
    except Exception:  # noqa: BLE001 - o erro da API pode trazer o token
        emitir('Não foi possível abrir ou atualizar a issue.')
        acao = 'erro'

    if houve_falha or erro_publicacao or acao == 'erro':
        emitir('Rodada com falha.')
        codigo = 1
    else:
        emitir('Rodada concluída com sucesso.')
        codigo = 0
    return ResultadoRodada(resultados, codigo, acao, logs)


def main(argv=None, **opcoes) -> int:
    argumentos = list(sys.argv[1:] if argv is None else argv)
    if argumentos in (['-h'], ['--help']):
        print(USO, end='')
        return 0
    if argumentos:
        print(USO, end='')
        return 2
    environ = opcoes.get('environ', os.environ)
    urls = {
        'VISTA_FEED_URL': environ.get('VISTA_FEED_URL', ''),
        'VISTA_FEED_URL_LOBO': environ.get('VISTA_FEED_URL_LOBO', ''),
    }
    cliente = opcoes['cliente'] if 'cliente' in opcoes else criar_cliente(environ)
    resultado = executar_rodada(
        Path(opcoes.get('diretorio', RAIZ)),
        urls,
        baixar=opcoes.get('baixar'),
        corrigir=opcoes.get('corrigir'),
        cliente=cliente,
        publicar=opcoes.get('publicar'),
        agora=opcoes.get('agora'),
        run_url=opcoes.get('run_url', url_da_execucao(environ)),
        proibidos=opcoes.get('proibidos', segredos_do_ambiente(environ)),
        log=opcoes.get('log'),
        environ=environ,
    )
    return resultado.codigo


if __name__ == '__main__':
    sys.exit(main())
