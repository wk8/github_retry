import os


def fixture_path(*subpath):
    return os.path.join(os.path.dirname(__file__), 'test_fixtures', *subpath)


class Generator(object):
    def __init__(self, *values):
        self._values = values
        self._index = -1

    def next(self):
        self._index += 1
        if self._index >= len(self._values):
            raise RuntimeError('No more values')
        return self.last_value

    @property
    def index(self):
        return self._index

    @property
    def last_value(self):
        return self._values[self._index]

    def assert_exhausted(self):
        assert self._index == len(self._values) - 1
