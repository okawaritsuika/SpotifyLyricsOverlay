"""OS-enforced budgets for the optional analysis subprocess, never the UI."""
import ctypes
from ctypes import wintypes
import os

CPU_PERCENT=3
MEMORY_MB=1024
_job=None


def request_efficiency_mode():
    """Ask Windows to favor lower clock rates for optional background work."""
    if os.name!='nt':return False
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.SetPriorityClass.argtypes=(wintypes.HANDLE,wintypes.DWORD)
    kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x40)  # IDLE_PRIORITY_CLASS
    setter=getattr(kernel,'SetProcessInformation',None)
    if setter is None:return False
    setter.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD)
    setter.restype=wintypes.BOOL
    state=(wintypes.DWORD*3)(1,1,1)  # Version 1, EXECUTION_SPEED control + enabled.
    return bool(setter(kernel.GetCurrentProcess(),4,ctypes.byref(state),ctypes.sizeof(state)))


def apply_analysis_limits(cpu_percent=CPU_PERCENT,memory_mb=MEMORY_MB,gpu=False):
    global _job
    if _job is not None:return _job
    if os.name!='nt':raise ValueError('음원 분석의 자원 제한은 Windows에서만 지원합니다.')
    if not 1<=cpu_percent<=(20 if gpu else 5) or not 64<=memory_mb<=(4096 if gpu else 1024):raise ValueError('분석 자원 제한값이 올바르지 않습니다.')
    class CpuRate(ctypes.Structure):
        _fields_=[('flags',wintypes.DWORD),('rate',wintypes.DWORD)]
    class Basic(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
                  ('flags',wintypes.DWORD),('minimum_working_set',ctypes.c_size_t),
                  ('maximum_working_set',ctypes.c_size_t),('active_processes',wintypes.DWORD),
                  ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
    class Io(ctypes.Structure):
        _fields_=[(name,ctypes.c_ulonglong) for name in ('reads','writes','other','read_bytes','write_bytes','other_bytes')]
    class Extended(ctypes.Structure):
        _fields_=[('basic',Basic),('io',Io),('process_memory',ctypes.c_size_t),
                  ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=(ctypes.c_void_p,wintypes.LPCWSTR);kernel.CreateJobObjectW.restype=wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD)
    kernel.SetInformationJobObject.restype=wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes=(wintypes.HANDLE,wintypes.HANDLE);kernel.AssignProcessToJobObject.restype=wintypes.BOOL
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    job=kernel.CreateJobObjectW(None,None)
    try:
        if not job:raise OSError(ctypes.get_last_error())
        cpu=CpuRate(0x1|0x4,cpu_percent*100)  # ENABLE | HARD_CAP, per 10,000 cycles.
        memory=Extended();memory.basic.flags=0x100;memory.process_memory=memory_mb*1024*1024
        for info_class,info in ((15,cpu),(9,memory)):
            if not kernel.SetInformationJobObject(job,info_class,ctypes.byref(info),ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job,kernel.GetCurrentProcess()):raise OSError(ctypes.get_last_error())
    except OSError as error:
        if job:kernel.CloseHandle(job)
        raise ValueError('CPU·메모리 제한을 적용하지 못해 음원 분석을 시작하지 않았습니다.') from error
    # Keep the job alive until this worker exits. The owning UI has a separate process.
    _job=job
    if not gpu:request_efficiency_mode()
    for name in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[name]='1'
    return job
