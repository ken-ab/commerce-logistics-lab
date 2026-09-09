"""Recover only API jobs whose owning process has ended; preserve business state."""
from contextlib import closing
import os
from pathlib import Path
import sys

from commerce_lab.state import now


def process_identity(pid):
    if sys.platform!='win32':
        try:
            stat=Path(f'/proc/{pid}/stat').read_text()
            return stat[stat.rfind(')')+2:].split()[19]
        except FileNotFoundError:
            return None
        except (PermissionError,OSError,IndexError):
            return 'unknown'
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:
        return None if ctypes.get_last_error() in (87,1168) else 'unknown'
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
            return 'unknown'
        # A nonzero exit time means this process ended even if a handle remains.
        if times[1].dwHighDateTime or times[1].dwLowDateTime:
            return None
        return str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


class JobManager:
    def __init__(self,store,identity_lookup=process_identity):
        self.store,self.identity_lookup=store,identity_lookup
        self.pid=os.getpid();self.identity=identity_lookup(self.pid)
        with closing(store.connect()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS api_job_owners(run_id TEXT PRIMARY KEY,pid INTEGER,process_identity TEXT,created_at TEXT)')

    def register(self,run_id):
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO api_job_owners VALUES (?,?,?,?)',(run_id,self.pid,self.identity,now()))

    def interrupt(self,run_id,reason):
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT status FROM runs WHERE id=?',(run_id,)).fetchone()
        if row and row['status'] in ('queued','running'):
            result={'run_id':run_id,'error_type':'InterruptedRun','error':reason,
                'state_preserved':True,'scope':'Recorded tool operations, cart and proposals remain available; do not assume an automatic rollback.'}
            self.store.trace(run_id,'host','job_interrupted',result)
            self.store.update_run(run_id,'failed',result)

    def recover(self):
        with closing(self.store.connect()) as db:
            rows=db.execute("SELECT j.* FROM api_job_owners j JOIN runs r ON r.id=j.run_id WHERE r.status IN ('queued','running')").fetchall()
        identities={r['pid']:self.identity_lookup(r['pid']) for r in rows}
        recovered=[]
        for row in rows:
            current=identities[row['pid']]
            if current!='unknown' and (current is None or current!=row['process_identity']):
                self.interrupt(row['run_id'],'The application process ended before this job finished. Review its preserved trace before starting a new request.')
                recovered.append(row['run_id'])
        return recovered
