"""Cross-process gate for offline operator procedures, separate from runtime ownership."""
from contextlib import contextmanager
import os
from pathlib import Path

@contextmanager
def operation_gate(database, *, require_existing=False):
    # Persistent coordination inode; never unlink it on release or after a crash.
    path=Path(str(database)+'.wahojobs-maintenance.lock')
    flags=os.O_RDWR|(0 if require_existing else os.O_CREAT)|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags,0o600)
    acquired=False
    try:
        import stat
        metadata=os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError('unsafe_operation_lock')
        if os.name=='nt':
            import msvcrt
            if metadata.st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        acquired=True
        yield
    finally:
        if acquired:
            if os.name=='nt':
                os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)
