import hashlib

import pytest


@pytest.mark.parametrize('policy', ['parallel', 'bulk'])
def test_copy_has_identical_bytes_and_resumes_a_verified_prefix(tmp_path, policy):
    from cowmata_tailring.workspace.fast_transfer import copy_verified
    source, partial = tmp_path/'source', tmp_path/'target.partial'
    content = bytes(range(256)) * 20000 + b'last block'
    source.write_bytes(content)
    partial.write_bytes(content[:123456])
    result = copy_verified(source, partial, hashlib.sha256(content).hexdigest(), policy=policy, block_size=65536)
    assert partial.read_bytes() == content and source.read_bytes() == content
    assert result['resumed_bytes'] == 123456
    assert result['written_bytes'] == len(content)-123456


def test_cancelled_transfer_can_resume_without_publishing_partial(tmp_path):
    from cowmata_tailring.workspace.fast_transfer import copy_verified
    source, partial = tmp_path/'source', tmp_path/'target.partial'
    content = b'12345678' * 1000000
    source.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    with pytest.raises(InterruptedError):
        copy_verified(source, partial, expected, block_size=65536,
                      cancelled=lambda: partial.exists() and partial.stat().st_size >= 131072)
    size = partial.stat().st_size
    assert 0 < size < len(content) and source.read_bytes() == content
    result = copy_verified(source, partial, expected, block_size=65536)
    assert result['resumed_bytes'] == size and partial.read_bytes() == content


def test_wrong_source_digest_never_returns_success(tmp_path):
    from cowmata_tailring.workspace.fast_transfer import copy_verified
    source = tmp_path/'source'
    source.write_bytes(b'changed source')
    with pytest.raises(ValueError):
        copy_verified(source, tmp_path/'partial', '0'*64)
    assert source.read_bytes() == b'changed source'


def test_damaged_partial_restarts_safely(tmp_path):
    from cowmata_tailring.workspace.fast_transfer import copy_verified
    source, partial = tmp_path/'source', tmp_path/'partial'
    source.write_bytes(b'correct source')
    partial.write_bytes(b'wrong')
    result = copy_verified(source, partial, hashlib.sha256(source.read_bytes()).hexdigest())
    assert result['resumed_bytes'] == 0 and partial.read_bytes() == source.read_bytes()
