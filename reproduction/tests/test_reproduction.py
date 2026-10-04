import csv
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import reproduce

REPO=ROOT.parent

class ReproductionTests(unittest.TestCase):
    def test_upper_cutoff_removed_but_negative_and_unmatched_not_accepted(self):
        path=REPO/'tools/realsense_steady_bench/analyze_full_receive_path.py'
        source=path.read_text()
        if 'latency_s > 0.250' in source: source=reproduce.patched_analyzer(source)
        module=types.ModuleType('patched_latency')
        exec(compile(source,str(path),'exec'),module.__dict__)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); events=root/'events.csv'
            with events.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['camera_index','serial','delivery','stream','stream_index','host_boottime_ns','backend_to_return_ms','arrival_to_return_ms'])
                w.writeheader()
                for delivery,age in [(1,300),(2,-100),(3,900)]:
                    w.writerow(dict(camera_index=0,serial='A',delivery=delivery,stream='Depth',stream_index=0,
                                    host_boottime_ns=10_000_000_000,backend_to_return_ms=age,arrival_to_return_ms=age))
            buffers=[dict(backend_s=9.7,complete_s=9.7,xhci_activation_s=9.7,xhci_irq=132),
                     dict(backend_s=10.1,complete_s=10.1,xhci_activation_s=10.1,xhci_irq=132)]
            result=module.host_receive_latency(events,buffers,'threaded_irq',root/'latency.csv',{0:132})
            self.assertEqual(result['total_deliveries'],3)
            self.assertEqual(result['matched_deliveries'],1)
            self.assertAlmostEqual(result['max_us'],300000)

    def test_all_adapters_parse_and_resolve_serials_without_boot_state_machine(self):
        cfg={'d435_serials':['111','222'],'d455_serial':'333',
             'e3_irqs':{'d435':{'number':55,'label':'xhci-hcd:usb8'},'d455':{'number':66,'label':'xhci-hcd:usb10'}}}
        with tempfile.TemporaryDirectory(prefix='repro test ') as temp:
            root=Path(temp)
            reproduce.prepare(REPO,root,cfg)
            for stage in reproduce.STAGES:
                path=root/'entrypoints'/(stage+'.sh')
                self.assertTrue(path.is_file())
                subprocess.run(['sh','-n',str(path)],check=True)
                text=path.read_text()
                for serial in ['948122073863','327122075717','311322302503']:
                    self.assertNotIn(serial,text)
                self.assertNotIn('case "$state" in',text)
            e4=(root/'entrypoints/e4-rt-threaded.sh').read_text()
            self.assertNotIn('result_root=${RS_P1_RESULT_ROOT',e4)
            self.assertIn('p1_trace_mode=host-latency',e4)
            self.assertIn('run_phase rt-threaded',e4)

    def test_adapters_fail_on_changed_source_boundary(self):
        with self.assertRaises(ValueError): reproduce.before('new implementation','old marker')
        with self.assertRaises(ValueError): reproduce.replace_once('x x','x','y')

    def test_swapped_camera_serials_and_irq_assignments(self):
        cfg={'d435_serials':['948122073863','327122075717'],'d455_serial':'311322304911',
             'e3_irqs':{'d435':{'number':137,'label':'xhci-hcd:usb4'},
                        'd455':{'number':132,'label':'xhci-hcd:usb2'}}}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            reproduce.prepare(REPO,root,cfg)
            import json
            cases=json.loads((root/'entrypoints/p1_v2_cases_20260820.json').read_text())
            self.assertEqual(cases['cases'][0]['probe']['serials'],cfg['d435_serials'])
            one=(root/'entrypoints/e3-one.sh').read_text()
            d435=one.split('    d435)')[1].split('    d455)')[0]
            d455=one.split('    d455)')[1].split('    *)')[0]
            self.assertIn('xhci_irq=137',d435)
            self.assertIn('xhci_irq=132',d455)

class LayoutTests(unittest.TestCase):
    def test_boot_selection_preserves_root_and_other_arguments(self):
        spec=importlib.util.spec_from_file_location('kernel_setup',ROOT/'setup/kernel.py')
        kernel=importlib.util.module_from_spec(spec); spec.loader.exec_module(kernel)
        original='[all]\nkernel=vmlinuz\nos_prefix=old/\n'
        cmd='console=tty1 root=LABEL=writable rootwait threadirqs quiet\n'
        config,line=kernel.boot_selection(original,cmd,'standard-6.12-btf',False)
        self.assertEqual(line,'console=tty1 root=LABEL=writable rootwait quiet\n')
        self.assertIn('kernel=vmlinuz',config)
        self.assertIn('os_prefix=standard-6.12-btf/',config)
        _,line=kernel.boot_selection(config,line,'rt-6.12-btf-uvc16',True)
        self.assertEqual(line.split().count('threadirqs'),1)
        fresh, _ = kernel.boot_selection('[all]\nkernel=vmlinuz\n',cmd,'rt-6.12-btf-uvc16',False)
        self.assertTrue(fresh.endswith('[all]\nos_prefix=rt-6.12-btf-uvc16/\n'))
        self.assertEqual(kernel.config_settings('CONFIG_CC_VERSION_TEXT=\"native\"\nCONFIG_PREEMPT_RT=y'),
                         kernel.config_settings('CONFIG_CC_VERSION_TEXT=\"cross\"\nCONFIG_PREEMPT_RT=y'))
        self.assertNotEqual(kernel.config_settings('CONFIG_PREEMPT_RT=y'),
                            kernel.config_settings('CONFIG_PREEMPT=y'))
        with self.assertRaises(ValueError): kernel.boot_selection(config,'quiet','x',False)
        with self.assertRaises(ValueError): kernel.boot_selection(config+'os_prefix=other/\n',cmd,'x',False)

    def test_experiment_stages_partition_capture_plan(self):
        stages=[stage for capture,_ in reproduce.EXPERIMENTS.values() for stage in capture]
        self.assertCountEqual(stages,reproduce.STAGES)
        self.assertEqual(len(stages),len(set(stages)))
        for directory in reproduce.EXPERIMENTS:
            subprocess.run([sys.executable,str(ROOT/directory/'run.py'),'plan'],
                           cwd='/tmp',check=True,stdout=subprocess.PIPE)

    def test_e3_zero_is_labelled_without_logarithm_failure(self):
        from analysis_common import module
        plotter=module('test_e3_plot',ROOT/'e3_kernel_path/plot.py')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with (root/'e3_summary.csv').open('w') as stream:
                stream.write('camera,treatment,nonfresh_percent\n')
                for camera in ('d435','d455'):
                    for treatment in ('xhci-other_uvc-other','xhci-fifo90_uvc-other','xhci-fifo90_uvc-fifo89'):
                        stream.write(f'{camera},{treatment},0\n')
            plotter.plot(root,REPO)
            text=(root/'e3-kernel-path.tex').read_text()
            self.assertIn('(3,0.01) [0]',text)
            self.assertNotIn('nan',text)

if __name__=='__main__': unittest.main()
