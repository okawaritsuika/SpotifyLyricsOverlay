"""Local-only realtime Whisper runtime with an explicit, observable CPU fallback."""
import os
from pathlib import Path
import sys
import sysconfig


_dll_directories={}


def prepare_cuda_dlls():
    """Expose installed CUDA libraries to this worker, never the system PATH."""
    add_directory=getattr(os,'add_dll_directory',None)
    if add_directory is None:return
    roots=[Path(value) for value in sys.path if value]
    purelib=sysconfig.get_path('purelib')
    if purelib:roots.append(Path(purelib))
    candidates=[root/'nvidia'/package/'bin' for root in roots
                for package in ('cublas','cudnn','cuda_runtime')]
    candidates.extend(Path(value)/'bin' for name,value in os.environ.items()
                      if value and (name=='CUDA_PATH' or name.startswith('CUDA_PATH_V')))
    directories=[]
    for candidate in candidates:
        if not candidate.is_dir():continue
        key=str(candidate.resolve())
        if key not in directories:directories.append(key)
        if key in _dll_directories:continue
        try:_dll_directories[key]=add_directory(key)
        except OSError:continue
    # CT2 also loads CUDA components with legacy LoadLibrary, which ignores
    # add_dll_directory. Its search needs the worker's own PATH as well.
    current=os.environ.get('PATH','')
    known={os.path.normcase(value) for value in current.split(os.pathsep)}
    missing=[value for value in directories if os.path.normcase(value) not in known]
    if missing:os.environ['PATH']=os.pathsep.join(missing+([current] if current else []))


def _load_cuda_model(factory,path):
    import numpy as np
    model=factory(str(path),device='cuda',compute_type='int8_float16',
                  cpu_threads=2,num_workers=1,local_files_only=True)
    # CTranslate2 can defer loading cuBLAS/cuDNN until the first encoder call.
    # Exercise that path before reporting CUDA as ready for live playback.
    model.encode(model.feature_extractor(np.zeros(16000,dtype=np.float32),padding=0))
    return model


def load_realtime_model(request,emit=None,*,_model_factory=None):
    """Return (model, metadata); legacy requests retain their small CPU runtime.

    ``model`` is the local small-model path used for CPU and fallback. The
    optional ``realtime_model`` is only used for an explicitly requested CUDA
    or auto runtime. No model downloads or process limits are applied here.
    """
    device=request.get('realtime_device','cpu')
    if device not in ('cpu','cuda','auto'):
        raise ValueError('실시간 분석 장치 설정이 올바르지 않습니다.')
    fallback_reason=None
    if device!='cpu':
        prepare_cuda_dlls()
        try:
            import ctranslate2
            available=ctranslate2.get_cuda_device_count()>0
        except (ImportError,OSError,RuntimeError):
            available=False
        if not available:fallback_reason='cuda_unavailable'
    if _model_factory is None:
        from faster_whisper import WhisperModel
    else:WhisperModel=_model_factory
    if device!='cpu' and not fallback_reason:
        path=request.get('realtime_model') or request['model']
        try:
            model=_load_cuda_model(WhisperModel,path)
        except (OSError,RuntimeError,ValueError,ImportError):
            fallback_reason='cuda_load_failed'
        else:
            return model,{'device':'cuda','model':Path(path).name,
                          'compute_type':'int8_float16','fallback':False}
    if fallback_reason and emit:
        emit('realtime_status',state='fallback',device='cpu',
             text='GPU를 사용할 수 없어 CPU 소형 모델로 실시간 음원 대조를 계속합니다.')
    path=request['model']
    model=WhisperModel(str(path),device='cpu',compute_type='int8',
                       cpu_threads=1,num_workers=1,local_files_only=True)
    metadata={'device':'cpu','model':Path(path).name,'compute_type':'int8',
              'fallback':bool(fallback_reason)}
    if fallback_reason:metadata['fallback_reason']=fallback_reason
    return model,metadata


def load_final_model(request,emit=None):
    """Use the selected local GPU model, retaining small as the CPU fallback."""
    import stable_whisper
    def fallback(kind,**event):
        if emit:emit('progress',state='analyzing',
                     text='전체 분석 GPU를 사용할 수 없어 CPU 소형 모델로 전환합니다.')
    runtime_request={'model':request['model'],
        'realtime_device':request.get('final_device',request.get('realtime_device','cpu'))}
    if request.get('final_model'):runtime_request['realtime_model']=request['final_model']
    model,runtime=load_realtime_model(runtime_request,
        fallback,_model_factory=stable_whisper.load_faster_whisper)
    if emit:
        emit('progress',state='analyzing',runtime=runtime,
             text=f"전체 음원 정밀 분석 · {'GPU' if runtime['device']=='cuda' else 'CPU'} · {runtime['model']}")
    return model,runtime
