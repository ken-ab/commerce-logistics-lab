"""A nonblocking execution lock for one local job directory, held for the whole run."""
import errno
import sys


class DirectoryRunLock:
    def __init__(self, directory):
        self.path = directory / '.execution.lock'
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise RuntimeError('Execution lock already held by this handle')
        handle = self.path.open('a+b')
        try:
            handle.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                # Windows permits locking a byte beyond EOF; no lock-file writes are needed.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        return True

    def release(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
