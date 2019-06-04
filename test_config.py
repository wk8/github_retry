import pytest

from config import Config
from test_helpers import fixture_path


def test_config():
    config = Config(fixture_path('config.yml'))

    assert config.get('repositories', 'moby/moby', 'checks', 'codecov/patch') == {'ignore': True}
    assert config.get('repositories', 'moby/moby', 'checks', 'codecov/patch', 'ignore') is True
    assert config.get('repositories', 'moby/moby', 'checks', 'codecov/patch', 'i_dont_exist') is None
    assert config.get('top_level') == 'value'
    assert config.get('i', 'dont', 'exist') is None

    with pytest.raises(RuntimeError):
        assert config.get('top_level', 'nested')
