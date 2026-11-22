import subprocess

import pytest

from annex_library.catalog import split_frontmatter
from annex_library.index import Index
from annex_library.refresh import IndexManager
from annex_library.tools import ToolError, annex_lib_edit, annex_lib_save, annex_lib_search
from tests.test_search import ARXIV_DOI, ARXIV_GLANCE, WILEY_DOI, WILEY_GLANCE
from tests.fixtures import clone_fixture


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def writer(fixture_library, tmp_path):
    root = clone_fixture(fixture_library, tmp_path / 'Library')
    manager = IndexManager(Index(tmp_path / 'index.sqlite3', root), watch=False)
    manager.start()
    yield manager
    manager.stop()


def paper(**updates):
    return dict(doi='10.5555/acceptance', title='Acceptance paper', source='publisher',
                url='https://doi.org/10.5555/acceptance', published='2026-01-01',
                captured='2026-11-21', tags=['acceptance'], authors=['A. Example'],
                version_type='journal-article', body='# Acceptance paper\n', **updates)


def test_scoped_save_and_attachment_equality(writer, tmp_path):
    root = writer.index.library_root
    readme = root / 'README.md'
    readme.write_text('staged\n')
    git(root, 'add', 'README.md')
    readme.write_text('unstaged\n')
    (root / 'untracked').write_text('untouched')
    before = git(root, 'status', '--porcelain')
    staged_blob = git(root, 'ls-files', '-s', 'README.md')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF-1.4 acceptance')
    args = dict(metadata=paper(), staged_files=[str(pdf)])
    saved = annex_lib_save(writer, **args, request_id='doi-1')
    assert saved['status'] == 'committed'
    attachment = saved['bundle'] + '/paper.pdf'
    assert set(git(root, 'show', '--format=', '--name-only', saved['commit_sha']).splitlines()) == {saved['record_path'], attachment}
    assert git(root, 'annex', 'lookupkey', attachment)
    assert git(root, 'status', '--porcelain') == before
    assert git(root, 'ls-files', '-s', 'README.md') == staged_blob
    assert readme.read_text() == 'unstaged\n'
    assert annex_lib_search(writer, doi=paper()['doi'])['total_works'] == 1
    assert annex_lib_save(writer, **args, request_id='doi-1')['commit_sha'] == saved['commit_sha']
    git(root, 'annex', 'drop', '--force', attachment)
    assert annex_lib_save(writer, **args, request_id='doi-2')['status'] == 'unchanged'
    assert git(root, 'rev-parse', 'HEAD') == saved['commit_sha']
    pdf.write_bytes(b'%PDF-1.4 changed')
    for request_id in ('doi-1', 'doi-3'):
        with pytest.raises(ToolError) as failure:
            annex_lib_save(writer, **args, request_id=request_id)
        assert failure.value.code == 'conflict'


def test_save_rollback_and_committed_replay(writer, monkeypatch, tmp_path):
    root = writer.index.library_root
    old_head = git(root, 'rev-parse', 'HEAD')
    (root / 'README.md').write_text('staged before failure\n')
    git(root, 'add', 'README.md')
    before = git(root, 'status', '--porcelain')
    git(root, 'config', 'user.name', '')
    pdf = tmp_path / 'test.pdf'
    pdf.write_bytes(b'%PDF-1.4 rollback')
    with pytest.raises(ToolError) as failure:
        annex_lib_save(writer, metadata=paper(), staged_files=[str(pdf)], request_id='reject')
    assert failure.value.code == 'git_failed'
    assert git(root, 'rev-parse', 'HEAD') == old_head
    assert git(root, 'status', '--porcelain') == before
    assert not (root / 'DOI/DOI-10.5555_acceptance').exists()
    git(root, 'config', 'user.name', 'Fixture')

    misc = dict(title='Journal digest', source='feed', url='https://example.org/digest',
                published='unknown', captured='2026-11-21', tags=['digest'], body='# Digest\n')

    def unavailable(*args, **kwargs):
        raise OSError('index failure')

    with monkeypatch.context() as patch:
        patch.setattr(writer, 'refresh_now', unavailable)
        result = annex_lib_save(writer, metadata=misc, request_id='misc-1')
    assert result['status'] == 'committed_index_pending'
    assert result['bundle'] == 'Misc/' + result['id']
    path = root / result['record_path']
    path.write_text(path.read_text().replace('Journal digest', 'Revised journal digest'))
    restarted = IndexManager(Index(tmp_path / 'rebuilt.sqlite3', root), watch=False)
    try:
        restarted.start()
        replay = annex_lib_save(restarted, metadata=misc, request_id='misc-1')
        assert replay['status'] == 'committed' and replay['replayed']
        assert replay['commit_sha'] == result['commit_sha'] == git(root, 'rev-parse', 'HEAD')
        assert replay['id'] == result['id']
        assert annex_lib_search(restarted, title='Revised journal digest')['total_works'] == 1
        with pytest.raises(ToolError) as failure:
            annex_lib_save(restarted, metadata={**misc, 'title': 'Another title'}, request_id='misc-1')
        assert failure.value.code == 'conflict'
    finally:
        restarted.stop()


def test_edit_one_version_and_preserve_body(writer):
    root = writer.index.library_root
    preprint = root / ARXIV_GLANCE
    preprint.write_bytes(preprint.read_bytes().replace(b'\n', b'\r\n'))
    git(root, 'add', ARXIV_GLANCE)
    git(root, 'commit', '-m', 'Use CRLF in the preprint')
    writer.refresh_now()
    notes = preprint.with_name('arxiv-notes.md')
    wiley = root / WILEY_GLANCE
    original_notes, original_wiley = notes.read_bytes(), wiley.read_bytes()
    _, original_body, _ = split_frontmatter(preprint.read_bytes().decode())
    for doi, path in ((ARXIV_DOI, ARXIV_GLANCE), (WILEY_DOI, WILEY_GLANCE)):
        work = annex_lib_search(writer, doi=doi)['works'][0]
        assert work['matched_version'] == path
        assert {version['record_path'] for version in work['versions']} == {ARXIV_GLANCE, WILEY_GLANCE}
    work = annex_lib_search(writer, doi=ARXIV_DOI)['works'][0]
    version = next(version for version in work['versions'] if version['record_path'] == ARXIV_GLANCE)
    args = dict(path=ARXIV_GLANCE, revision=version['revision'], field_updates={'tags': [*version['tags'], 'verified']})
    result = annex_lib_edit(writer, **args, request_id='edit-1')
    assert result['status'] == 'committed'
    assert git(root, 'show', '--format=', '--name-only', result['commit_sha']) == ARXIV_GLANCE
    assert split_frontmatter(preprint.read_bytes().decode())[1] == original_body
    assert notes.read_bytes() == original_notes
    assert wiley.read_bytes() == original_wiley
    work = annex_lib_search(writer, tags=['verified'])['works'][0]
    assert work['matched_version'] == ARXIV_GLANCE
    current = next(version for version in work['versions'] if version['record_path'] == ARXIV_GLANCE)
    assert annex_lib_edit(writer, **{**args, 'revision': current['revision']}, request_id='edit-noop')['status'] == 'unchanged'
    assert annex_lib_edit(writer, **args, request_id='edit-1')['commit_sha'] == result['commit_sha']
    with pytest.raises(ToolError) as failure:
        annex_lib_edit(writer, **args, request_id='edit-stale')
    assert failure.value.code == 'conflict'
    preprint.rename(preprint.with_name('renamed-glance.md'))
    with pytest.raises(ToolError) as failure:
        annex_lib_edit(writer, **{**args, 'revision': current['revision']}, request_id='edit-moved')
    assert failure.value.code == 'conflict'
