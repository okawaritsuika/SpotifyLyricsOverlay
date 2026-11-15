from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import audio_runtime


class AudioRuntimeTests(unittest.TestCase):
    def runtime(self,request,device_count=1,factory=None,warmup_error=None,final=False):
        gpu=Mock();gpu.feature_extractor.return_value='features'
        gpu.encode.side_effect=warmup_error
        cpu=Mock()
        if factory is None:factory=Mock(side_effect=[gpu,cpu])
        cuda_count=Mock(return_value=device_count)
        samples=object();zeros=Mock(return_value=samples)
        modules={'faster_whisper':SimpleNamespace(WhisperModel=factory),
                 'stable_whisper':SimpleNamespace(load_faster_whisper=factory),
                 'ctranslate2':SimpleNamespace(get_cuda_device_count=cuda_count),
                 'numpy':SimpleNamespace(zeros=zeros,float32='float32')}
        events=Mock()
        with patch.dict('sys.modules',modules),patch.object(audio_runtime,'prepare_cuda_dlls') as prepare:
            result=(audio_runtime.load_final_model if final else audio_runtime.load_realtime_model)(request,events)
        return result,factory,gpu,cuda_count,prepare,events,zeros,samples

    def test_legacy_request_keeps_cpu_without_probing_cuda(self):
        result,factory,_,cuda_count,prepare,events,_,_=self.runtime({'model':'models/whisper-small'})
        factory.assert_called_once_with('models/whisper-small',device='cpu',compute_type='int8',
                                      cpu_threads=1,num_workers=1,local_files_only=True)
        self.assertEqual(result[1],{'device':'cpu','model':'whisper-small','compute_type':'int8','fallback':False})
        cuda_count.assert_not_called();prepare.assert_not_called();events.assert_not_called()

    def test_explicit_cpu_ignores_the_larger_gpu_model(self):
        result,factory,*_=self.runtime({'model':'models/whisper-small',
            'realtime_model':'models/whisper-turbo','realtime_device':'cpu'})
        self.assertEqual(factory.call_args.args,('models/whisper-small',))
        self.assertFalse(result[1]['fallback'])

    def test_final_gpu_uses_small_stable_factory_even_when_realtime_uses_medium(self):
        result,factory,gpu,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'realtime_model':'models/whisper-medium','realtime_device':'auto'},final=True)
        self.assertIs(result[0],gpu)
        factory.assert_called_once_with('models/whisper-small',device='cuda',compute_type='int8_float16',
                                      cpu_threads=2,num_workers=1,local_files_only=True)
        self.assertEqual(events.call_args.args,('progress',))
        self.assertEqual(events.call_args.kwargs['runtime']['device'],'cuda')
        self.assertEqual(events.call_args.kwargs['state'],'analyzing')

    def test_final_gpu_fallback_is_an_observable_analysis_event(self):
        result,_,_,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'final_device':'auto'},device_count=0,final=True)
        self.assertEqual(result[1]['device'],'cpu')
        self.assertTrue(result[1]['fallback'])
        self.assertTrue(all(call.args==('progress',) and call.kwargs['state']=='analyzing'
                            for call in events.call_args_list))
        self.assertEqual(events.call_args.kwargs['runtime'],result[1])

    def test_final_gpu_uses_explicit_final_model_separately_from_realtime(self):
        result,factory,gpu,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'realtime_model':'models/whisper-medium','final_model':'models/whisper-turbo',
            'final_device':'auto'},final=True)
        self.assertIs(result[0],gpu)
        factory.assert_called_once_with('models/whisper-turbo',device='cuda',compute_type='int8_float16',
                                      cpu_threads=2,num_workers=1,local_files_only=True)
        self.assertEqual(result[1]['model'],'whisper-turbo')
        self.assertEqual(events.call_args.kwargs['runtime'],result[1])

    def test_final_model_cache_reuses_only_the_same_runtime_request(self):
        cache={}
        request={'model':'models/whisper-small','final_model_cache':cache}
        first,factory,*_=self.runtime(request,final=True)
        factory.assert_called_once()
        second,factory,*_=self.runtime(request,final=True)
        self.assertIs(second[0],first[0])
        factory.assert_not_called()
        cuda,factory,*_=self.runtime({**request,'final_device':'auto'},final=True)
        factory.assert_called_once()
        self.assertEqual(cuda[1]['device'],'cuda')

    def test_final_cpu_ignores_explicit_final_gpu_model(self):
        result,factory,_,cuda_count,prepare,_,_,_=self.runtime({'model':'models/whisper-small',
            'final_model':'models/whisper-turbo','final_device':'cpu'},final=True)
        factory.assert_called_once_with('models/whisper-small',device='cpu',compute_type='int8',
                                      cpu_threads=1,num_workers=1,local_files_only=True)
        self.assertEqual(result[1]['model'],'whisper-small')
        cuda_count.assert_not_called();prepare.assert_not_called()

    def test_final_turbo_cuda_missing_falls_back_to_small(self):
        result,factory,_,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'final_model':'models/whisper-turbo','final_device':'auto'},device_count=0,final=True)
        factory.assert_called_once_with('models/whisper-small',device='cpu',compute_type='int8',
                                      cpu_threads=1,num_workers=1,local_files_only=True)
        self.assertEqual(result[1]['fallback_reason'],'cuda_unavailable')
        self.assertEqual(events.call_args.kwargs['runtime']['model'],'whisper-small')

    def test_final_turbo_warmup_failure_falls_back_to_small(self):
        result,factory,gpu,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'final_model':'models/whisper-turbo','final_device':'cuda'},
            warmup_error=RuntimeError('private runtime failure'),final=True)
        self.assertEqual([call.args[0] for call in factory.call_args_list],
                         ['models/whisper-turbo','models/whisper-small'])
        self.assertEqual(result[1]['model'],'whisper-small')
        self.assertEqual(result[1]['fallback_reason'],'cuda_load_failed')
        self.assertNotIn('private',str(events.call_args_list))

    def test_explicit_final_cpu_overrides_realtime_gpu_preference(self):
        result,_,_,cuda_count,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'final_device':'cpu','realtime_device':'auto'},final=True)
        self.assertEqual(result[1]['device'],'cpu');cuda_count.assert_not_called()
        self.assertEqual(events.call_args.kwargs['runtime']['device'],'cpu')

    def test_auto_loads_local_gpu_model_and_warms_encoder(self):
        result,factory,gpu,cuda_count,prepare,events,zeros,samples=self.runtime({
            'model':'models/whisper-small','realtime_model':'models/whisper-turbo','realtime_device':'auto'})
        self.assertIs(result[0],gpu)
        self.assertEqual(result[1],{'device':'cuda','model':'whisper-turbo',
                                 'compute_type':'int8_float16','fallback':False})
        factory.assert_called_once_with('models/whisper-turbo',device='cuda',compute_type='int8_float16',
                                      cpu_threads=2,num_workers=1,local_files_only=True)
        cuda_count.assert_called_once_with();prepare.assert_called_once_with()
        zeros.assert_called_once_with(16000,dtype='float32')
        gpu.feature_extractor.assert_called_once_with(samples,padding=0)
        gpu.encode.assert_called_once_with('features');events.assert_not_called()

    def test_cuda_without_realtime_model_uses_legacy_local_model(self):
        result,factory,*_=self.runtime({'model':'models/whisper-small','realtime_device':'cuda'})
        self.assertEqual(factory.call_args.args,('models/whisper-small',))
        self.assertEqual(result[1]['device'],'cuda')

    def test_unavailable_gpu_falls_back_to_small_with_safe_status(self):
        result,factory,_,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'realtime_model':'models/whisper-turbo','realtime_device':'auto'},device_count=0)
        self.assertEqual(result[1]['fallback_reason'],'cuda_unavailable')
        factory.assert_called_once_with('models/whisper-small',device='cpu',compute_type='int8',
                                      cpu_threads=1,num_workers=1,local_files_only=True)
        self.assertEqual(events.call_args.kwargs['state'],'fallback')

    def test_gpu_load_error_falls_back_without_exposing_error_contents(self):
        factory=Mock(side_effect=[RuntimeError('private model path or lyric text'),object()])
        result,factory,_,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'realtime_model':'models/whisper-turbo','realtime_device':'cuda'},factory=factory)
        self.assertEqual(result[1]['fallback_reason'],'cuda_load_failed')
        self.assertEqual(factory.call_count,2)
        self.assertEqual(factory.call_args.kwargs['device'],'cpu')
        self.assertEqual(factory.call_args.args,('models/whisper-small',))
        self.assertNotIn('private',str(events.call_args_list))

    def test_deferred_cuda_encoder_error_also_falls_back(self):
        result,factory,gpu,_,_,events,_,_=self.runtime({'model':'models/whisper-small',
            'realtime_model':'models/whisper-turbo','realtime_device':'auto'},
            warmup_error=RuntimeError('Library private.dll not found'))
        gpu.encode.assert_called_once_with('features')
        self.assertEqual(factory.call_count,2)
        self.assertEqual(result[1]['device'],'cpu')
        self.assertTrue(result[1]['fallback'])
        self.assertNotIn('private',str(events.call_args_list))

    def test_dll_discovery_retains_handles_and_sets_only_worker_path_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cublas=root/'packages'/'nvidia'/'cublas'/'bin';cublas.mkdir(parents=True)
            cudnn=root/'packages'/'nvidia'/'cudnn'/'bin';cudnn.mkdir(parents=True)
            toolkit=root/'cuda'/'bin';toolkit.mkdir(parents=True)
            add=Mock(side_effect=lambda path:object())
            handles={}
            with patch.object(audio_runtime,'_dll_directories',handles), \
                 patch.object(audio_runtime.sys,'path',[str(root/'packages')]), \
                 patch.object(audio_runtime.sysconfig,'get_path',return_value=str(root/'packages')), \
                 patch.object(audio_runtime.os,'add_dll_directory',add,create=True), \
                 patch.dict(audio_runtime.os.environ,{'CUDA_PATH':str(root/'cuda'),'PATH':'unchanged'},clear=True):
                audio_runtime.prepare_cuda_dlls()
                first_path=audio_runtime.os.environ['PATH']
                audio_runtime.prepare_cuda_dlls()
                self.assertEqual(audio_runtime.os.environ['PATH'],first_path)
                self.assertEqual(first_path.split(audio_runtime.os.pathsep),
                                 [str(path.resolve()) for path in (cublas,cudnn,toolkit)]+['unchanged'])
                self.assertEqual(set(handles),{str(path.resolve()) for path in (cublas,cudnn,toolkit)})
                self.assertEqual(add.call_count,3)


if __name__=='__main__':unittest.main()
