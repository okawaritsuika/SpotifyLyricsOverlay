import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name=='nt','Windows Job Object budgets')
class AnalysisLimitsTests(unittest.TestCase):
    def test_os_enforces_cpu_and_memory_limits_without_loading_a_model(self):
        script='''
import ctypes,os
from ctypes import wintypes
from analysis_limits import apply_analysis_limits
handle=apply_analysis_limits(memory_mb=64)
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.QueryInformationJobObject.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p)
cpu=(wintypes.DWORD*2)()
assert kernel.QueryInformationJobObject(handle,15,ctypes.byref(cpu),ctypes.sizeof(cpu),None)
assert list(cpu)==[5,300],list(cpu)
assert os.environ['OMP_NUM_THREADS']=='1'
try:
    memory=bytearray(80*1024*1024)
except MemoryError:
    print('CPU_CAP_AND_MEMORY_LIMIT_VERIFIED')
else:
    raise AssertionError('allocation exceeded the committed-memory limit')
'''
        result=subprocess.run([sys.executable,'-c',script],cwd=Path(__file__).resolve().parents[1],
                              text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('CPU_CAP_AND_MEMORY_LIMIT_VERIFIED',result.stdout)
