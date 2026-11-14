"""OpenDahua'yı Python 3.10-3.13'te çalıştırma yaması.

OpenDahua, Python 3.14'ün ertelenmiş tip açıklamalarına (PEP 649) dayanır;
3.12'de 'NameError: name ... is not defined' verir. Burada opendahua
modülleri, 'from __future__ import annotations' etkin olarak derlenir.
Bu, kütüphanenin davranışını değiştirmez; yalnızca tip açıklamalarını
çalışma anında değerlendirmeyi erteler.
"""
from __future__ import annotations

import __future__ as _future
import importlib.abc
import importlib.machinery
import sys

_PKG = "opendahua"
_FLAG = _future.annotations.compiler_flag


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # Önbellekteki .pyc yamasız derlenmiş olabilir; her zaman kaynaktan derle.
        source = self.get_data(self.path)
        return compile(source, self.path, "exec", flags=_FLAG, dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != _PKG and not fullname.startswith(_PKG + "."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


if sys.version_info < (3, 14) and not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
