"""Receipt storage for authorized POP product migrations."""
import stat
import sys
from pathlib import Path

MACOS=sys.platform == "darwin"

class CloudFileUnavailable(RuntimeError):
    def __init__(self, path):
        self.path = Path(path).absolute()
        super().__init__(f'文件尚未下载到本机：{self.path}')

    def receipt(self):
        return {'status':'blocked', 'error':'macos-cloud-file-not-local', 'path':str(self.path),
                'nextAction':'在访达中按住 Control 键点按该项目，选择“现在下载”。下载完成后从原任务继续。'}


def check_local_metadata(path, metadata):
    # Darwin sys/stat.h defines SF_DATALESS; older Python stat modules omit the name.
    if MACOS and getattr(metadata, 'st_flags', 0) & getattr(stat, 'SF_DATALESS', 0x40000000):
        raise CloudFileUnavailable(path)


def require_local(path, *, parents=False):
    """Inspect only the requested path. A missing optional file keeps its old behaviour."""
    if not isinstance(path,Path):
        path = Path(path)
    if MACOS:
        chain = (*reversed(path.absolute().parents), path) if parents else (path,)
        for item in chain:
            try:
                metadata = item.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    metadata = item.stat()
            except FileNotFoundError:
                break
            check_local_metadata(item,metadata)
    return path
