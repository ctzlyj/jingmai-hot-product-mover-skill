"""Cross-process locks and an independent state namespace."""
import os,time,threading
from pathlib import Path
from . import require_local
from .safety import is_linked,SafetyError

class FileLock:
 def __init__(self,path:Path,timeout=5,*,shared=False):self.path=Path(path);self.timeout=timeout;self.shared=shared;self.stream=None;self.acquired=False;self.owner_thread=None
 def _windows_lock(self,unlock=False):
  import ctypes,msvcrt
  from ctypes import wintypes
  if not unlock:
   class Overlapped(ctypes.Structure):
    _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
   self.overlapped=Overlapped();self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
   self.kernel.LockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
   self.kernel.LockFileEx.restype=wintypes.BOOL
   self.kernel.UnlockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
   self.kernel.UnlockFileEx.restype=wintypes.BOOL
  handle=msvcrt.get_osfhandle(self.stream.fileno())
  success=self.kernel.UnlockFileEx(handle,0,1,0,ctypes.byref(self.overlapped)) if unlock else self.kernel.LockFileEx(handle,1|(0 if self.shared else 2),0,1,0,ctypes.byref(self.overlapped))
  if not success:raise ctypes.WinError(ctypes.get_last_error())
 def require_owned(self,path):
  if not self.acquired or self.stream is None or self.stream.closed or self.path.resolve()!=Path(path).resolve() or self.owner_thread!=threading.get_ident():raise SafetyError('existing-same-owner-install-guard-required')
 def __enter__(self):
  require_local(self.path,parents=True)
  self.path.parent.mkdir(parents=True,exist_ok=True)
  if is_linked(self.path):raise SafetyError('linked-lock')
  self.stream=self.path.open('a+b')
  if self.path.stat().st_size==0:self.stream.write(b'0');self.stream.flush()
  deadline=time.monotonic()+self.timeout
  while True:
   try:
    self.stream.seek(0)
    if os.name=='nt':
     self._windows_lock()
    else:
     import fcntl
     fcntl.flock(self.stream.fileno(),(fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
    self.acquired=True;self.owner_thread=threading.get_ident();return self
   except OSError:
    if time.monotonic()>=deadline:self.stream.close();raise SafetyError('resource-busy') from None
    time.sleep(.025)
 def __exit__(self,*args):
  self.stream.seek(0)
  if os.name=='nt':
   self._windows_lock(unlock=True)
  else:
   import fcntl
   fcntl.flock(self.stream.fileno(),fcntl.LOCK_UN)
  self.stream.close();self.acquired=False;self.owner_thread=None

def merchant_home():
    """Internal POP receipt root, independent of merchant installations."""
    return Path(os.environ.get("HEIDENG_POP_HOME") or Path.home() / ".heideng-pop").resolve()
