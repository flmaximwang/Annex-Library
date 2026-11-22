"""Run public MCP acceptance and end-to-end latency on disposable checkouts."""

import argparse
import asyncio
import json
import math
import platform
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fixtures import build_fixture
from tests.test_search import ARXIV_DOI, ARXIV_GLANCE, WILEY_DOI, WILEY_GLANCE, WILEY_PDF
from tests.test_writes import paper


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@asynccontextmanager
async def connect(root, index, fault=None):
    args = ['--library', str(root), '--index', str(index)]
    command = [sys.executable, '-m', 'annex_library.server', *args]
    if fault is not None:
        # Inject a single post-commit refresh failure inside the server process.
        code = '''
import sys
from pathlib import Path
from annex_library.index import Index
from annex_library.server import main
fault = Path(sys.argv[1])
refresh = Index.refresh
def fail_once(self, *args, **kwargs):
    if fault.exists():
        fault.unlink()
        raise OSError('injected index failure')
    return refresh(self, *args, **kwargs)
Index.refresh = fail_once
raise SystemExit(main(sys.argv[2:]))
'''
        command = [sys.executable, '-c', code, str(fault), *args]
    started = time.perf_counter()
    async with stdio_client(StdioServerParameters(command=command[0], args=command[1:])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            print(f'Cold server startup and MCP initialize: {(time.perf_counter() - started) * 1000:.1f} ms')
            yield session


async def call(session, tool, **args):
    response = await session.call_tool(f'annex-lib-{tool}', args)
    assert not response.is_error, response.content
    return response.structured_content or json.loads(response.content[0].text)


async def verify(root, temp):
    for path, authors in (
        (WILEY_GLANCE, ['Lorenzo Travaglini', 'Nga T. Lam', 'Artur Sawicki', 'Hee-Jeong Cha', 'Dominic J. Glover']),
        (ARXIV_GLANCE, ['Lorenzo Travaglini', 'Artur Sawicki', 'Dominic J. Glover']),
    ):
        record = root / path
        _, frontmatter, body = record.read_text().split('---\n', 2)
        fields = yaml.safe_load(frontmatter)
        fields['authors'] = authors
        record.write_text('---\n' + yaml.safe_dump(fields, sort_keys=False) + '---\n' + body)
    git(root, 'add', WILEY_GLANCE, ARXIV_GLANCE)
    names = ('ISBN-978-0-08-087776-1', 'ISBN-978-0-08-102256-6',
             'ISBN-978-0-12-227410-7-C5XKA6VZ', 'ISBN-978-0-12-227410-7-YI73UQM9')
    for name in names:
        path = root / 'ISBN' / name / 'example.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(f'%PDF-1.4 {name}'.encode())
    git(root, 'add', 'ISBN')
    git(root, 'commit', '-m', 'Seed ISBN acceptance cases')
    print(f'Workflow fixture starting revision: {git(root, "rev-parse", "HEAD")}')
    pdf = temp / 'paper.pdf'
    pdf.write_bytes(b'%PDF-1.4 MCP acceptance')
    fault = temp / 'fail-refresh'
    archive = dict(metadata=paper(), staged_files=[str(pdf)], request_id='public-paper')
    misc = dict(metadata=dict(title='Public digest', source='feed', url='https://example.org/digest',
                              published='unknown', captured='2026-11-21', tags=['digest'], body='# Digest\n'),
                request_id='public-misc')
    async with connect(root, temp / 'workflow.sqlite3', fault) as session:
        for name in names[:2]:
            assert (await call(session, 'check', path=f'ISBN/{name}'))['ok']
        for name in names[2:]:
            diagnostics = (await call(session, 'check', path=f'ISBN/{name}'))['diagnostics']
            assert {d['rule'] for d in diagnostics} == {'isbn.name', 'isbn.collision'}
        for doi, path in ((WILEY_DOI, WILEY_GLANCE), (ARXIV_DOI, ARXIV_GLANCE)):
            work = (await call(session, 'search', doi=doi))['works'][0]
            assert work['matched_version'] == path
            assert {v['record_path'] for v in work['versions']} == {WILEY_GLANCE, ARXIV_GLANCE}
        notes_path = (root / ARXIV_GLANCE).with_name('arxiv-notes.md')
        notes = notes_path.read_bytes()
        whole = await call(session, 'check')
        assert not any(d['path'].endswith('-notes.md') for d in whole['diagnostics'])
        git(root, 'annex', 'drop', '--force', WILEY_PDF)
        work = (await call(session, 'search', doi=WILEY_DOI, include_local_status=True))['works'][0]
        assert any(a['path'] == WILEY_PDF and a['local_content'] == 'missing'
                   for v in work['versions'] for a in v['attachments'])
        readme = root / 'README.md'
        readme.write_text('staged\n')
        git(root, 'add', 'README.md')
        readme.write_text('unstaged\n')
        status = git(root, 'status', '--porcelain')
        head = git(root, 'rev-parse', 'HEAD')
        git(root, 'config', 'user.name', '')
        seen = set()
        failed = await call(session, 'save', **archive)
        assert failed['error']['code'] == 'git_failed'
        assert git(root, 'rev-parse', 'HEAD') == head
        assert git(root, 'status', '--porcelain') == status
        git(root, 'config', 'user.name', 'Acceptance fixture')
        saved = await call(session, 'save', **archive)
        assert saved['status'] == 'committed'
        attachment = f'{saved["bundle"]}/paper.pdf'
        assert set(git(root, 'show', '--format=', '--name-only', saved['commit_sha']).splitlines()) == {saved['record_path'], attachment}
        assert git(root, 'annex', 'lookupkey', attachment)
        assert git(root, 'status', '--porcelain') == status
        direct = (await call(session, 'search', doi=archive['metadata']['doi']))['works'][0]
        assert direct['matched_version'] == saved['record_path'] and len(direct['versions']) == 1
        arxiv = (await call(session, 'search', doi=ARXIV_DOI))['works'][0]
        version = next(v for v in arxiv['versions'] if v['record_path'] == ARXIV_GLANCE)
        edit = await call(session, 'edit', path=ARXIV_GLANCE, revision=version['revision'],
                          field_updates={'tags': [*version['tags'], 'stdio-verified']}, request_id='public-edit')
        assert edit['status'] == 'committed'
        assert git(root, 'show', '--format=', '--name-only', edit['commit_sha']) == ARXIV_GLANCE
        assert (await call(session, 'search', tags=['stdio-verified']))['works'][0]['matched_version'] == ARXIV_GLANCE
        assert notes_path.read_bytes() == notes
        fault.touch()
        pending = await call(session, 'save', **misc)
        # The caller advances state only when both required archives have succeeded.
        if saved['status'] == pending['status'] == 'committed':
            seen.add('example-item')
        assert pending['status'] == 'committed_index_pending' and not seen
        digest = root / pending['record_path']
        digest.write_text(digest.read_text().replace('Public digest', 'Externally revised digest'))
        started = time.perf_counter()
        while not (await call(session, 'search', title='Externally revised digest'))['total_works']:
            assert time.perf_counter() - started < 5
            await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - started
        assert elapsed < 5
        print(f'External edit visible over MCP after {elapsed:.2f} s')
    async with connect(root, temp / 'rebuilt.sqlite3') as session:
        replay = await call(session, 'save', **misc)
        assert replay['status'] == 'committed' and replay['replayed']
        assert replay['commit_sha'] == pending['commit_sha'] == git(root, 'rev-parse', 'HEAD')
        assert replay['id'] == pending['id']
        assert (await call(session, 'search', title='Externally revised digest'))['total_works'] == 1
        if saved['status'] == replay['status'] == 'committed':
            seen.add('example-item')
        assert seen == {'example-item'}
    print('Public MCP workflow, recovery and caller state checks passed')


async def benchmark(root, index):
    print(f'Benchmark revision: {git(root, "rev-parse", "HEAD")}')
    command = [sys.executable, '-m', 'annex_library.server', '--library', str(root)]
    started = time.perf_counter()
    built = subprocess.check_output([*command, '--index', str(index), '--rebuild'], text=True)
    print(f'First build: {json.loads(built)["build_ms"]:.1f} ms in index, {(time.perf_counter() - started) * 1000:.1f} ms CLI')
    queries = [dict(doi=WILEY_DOI), dict(title='conductive protein heme'), dict(author='Sawicki'),
               dict(captured_from='2026-11-01', captured_to='2026-11-21', tags=['gamma-prefoldin', 'nanowire'])]
    queries.append({**queries[0], 'include_local_status': True})
    async with connect(root, index.with_name('benchmark-stdio.sqlite3')) as session:
        for query in queries:
            samples = []
            for iteration in range(55):
                started = time.perf_counter()
                result = await call(session, 'search', limit=20, **query)
                if iteration >= 5:
                    samples.append((time.perf_counter() - started) * 1000)
                assert 0 < result['returned'] <= 20
            p95 = sorted(samples)[math.ceil(.95 * len(samples)) - 1]
            print(f'{query} => {result["returned"]} work(s), n={len(samples)}, P95={p95:.2f} ms')
            if 'include_local_status' not in query:
                assert p95 <= 100


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark-from', type=Path, help='Clone this checkout locally for realistic read-only timings')
    args = parser.parse_args()
    print(f'Machine: {platform.platform()}, {platform.machine()}, Python {platform.python_version()}')
    with tempfile.TemporaryDirectory(prefix='annex-library-v1-') as directory:
        temp = Path(directory)
        fixture = build_fixture(temp / 'Library')
        await verify(fixture, temp)
        root = fixture
        if args.benchmark_from:
            root = temp / 'Benchmark'
            subprocess.run(['git', 'clone', '--shared', '--quiet', str(args.benchmark_from.resolve()), str(root)], check=True)
            git(root, 'annex', 'init', '-q')
        await benchmark(root, temp / 'benchmark-cli.sqlite3')


if __name__ == '__main__':
    asyncio.run(main())
