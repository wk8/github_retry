import yaml


class Config(object):
    def __init__(self, file='config.yml'):
        with open(file) as f:
            self._data = yaml.load(f, Loader=yaml.FullLoader)

    def get(self, *path):
        current = self._data
        for item in path:
            if not isinstance(current, dict):
                raise RuntimeError('Invalid config path %s' % (path, ))
            current = current.get(item)
            if current is None:
                return None
        return current
